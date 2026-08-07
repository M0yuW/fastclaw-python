from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError
from typer.testing import CliRunner

from fastclaw.cli import _BACKFILL_TEAM_MEMBERS, app
from fastclaw.storage import (
    AgentRecord,
    AgentTeamMemberRecord,
    AgentTeamRecord,
    Database,
    UnitOfWork,
    UserRecord,
)
from fastclaw.teams import TeamRole, TeamService, TeamValidationError


@pytest.mark.anyio
async def test_team_creation_is_atomic_and_idempotent(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'teams.db'}")
    await database.create_schema()
    try:
        async with UnitOfWork(database) as unit:
            await unit.require_store().save_user(
                UserRecord(id="usr_1", username="one", email="one@example.test", password_hash="x")
            )
        service = TeamService(database)
        first, members = await service.create(
            user_id="usr_1",
            name="Markets",
            description="",
            template_key="finance-market-research",
            client_request_id="request-1",
        )
        second, retried_members = await service.create(
            user_id="usr_1",
            name="ignored",
            description="",
            template_key="finance-market-research",
            client_request_id="request-1",
        )
        assert first.status == "active"
        assert second.id == first.id
        assert len(members) == len(retried_members) == 5
        assert [member.member_type for member in members].count("coordinator") == 1
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            assert len(await store.list_agents("usr_1")) == 5
            assert len(await store.list_team_members(first.id)) == 5
            created = [await store.get_agent(member.agent_id) for member in members]
            assert all(agent is not None and "model" not in agent.config for agent in created)
    finally:
        await database.close()


@pytest.mark.anyio
async def test_team_creation_concurrent_retries_share_one_team(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'concurrent-teams.db'}")
    await database.create_schema()
    try:
        async with UnitOfWork(database) as unit:
            await unit.require_store().save_user(
                UserRecord(id="usr_1", username="one", email="one@example.test", password_hash="x")
            )
        service = TeamService(database)
        results = await asyncio.gather(
            *(
                service.create(
                    user_id="usr_1",
                    name="Markets",
                    description="",
                    template_key="finance-market-research",
                    client_request_id="concurrent-request",
                )
                for _ in range(2)
            )
        )
        assert results[0][0].id == results[1][0].id
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            assert len(await store.list_teams("usr_1")) == 1
            assert len(await store.list_agents("usr_1")) == 5
    finally:
        await database.close()


def test_custom_team_requires_one_coordinator_and_specialist() -> None:
    with pytest.raises(TeamValidationError, match="exactly one coordinator"):
        from fastclaw.teams import validate_roles

        validate_roles((TeamRole("worker", "Worker", "specialist"),))


@pytest.mark.anyio
async def test_agent_can_belong_to_only_one_team(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'exclusive-membership.db'}")
    await database.create_schema()
    try:
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_user(
                UserRecord(id="user-1", username="one", email="one@example.test", password_hash="x")
            )
            await store.save_agent(AgentRecord(id="agent-1", user_id="user-1", name="Agent"))
            await store.save_team(
                AgentTeamRecord(
                    id="team-1",
                    user_id="user-1",
                    name="One",
                    client_request_id="request-1",
                )
            )
            await store.save_team(
                AgentTeamRecord(
                    id="team-2",
                    user_id="user-1",
                    name="Two",
                    client_request_id="request-2",
                )
            )
            await store.save_team_member(
                AgentTeamMemberRecord(
                    team_id="team-1",
                    agent_id="agent-1",
                    role_key="coordinator",
                    member_type="coordinator",
                )
            )
        with pytest.raises(IntegrityError):
            async with UnitOfWork(database) as unit:
                await unit.require_store().save_team_member(
                    AgentTeamMemberRecord(
                        team_id="team-2",
                        agent_id="agent-1",
                        role_key="coordinator",
                        member_type="coordinator",
                    )
                )
    finally:
        await database.close()


def test_backfill_teams_is_idempotent_and_reports_partial_matches(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}"
    finance_names = _BACKFILL_TEAM_MEMBERS["finance-market-research"]

    def decode_report(output: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(output[output.index("{") :]))

    async def seed() -> None:
        database = Database(database_url)
        await database.create_schema()
        try:
            async with UnitOfWork(database) as unit:
                store = unit.require_store()
                await store.save_user(
                    UserRecord(
                        id="user-1",
                        username="one",
                        email="one@example.test",
                        password_hash="x",
                    )
                )
                for index, name in enumerate((*finance_names, "coordinator-wc")):
                    await store.save_agent(
                        AgentRecord(
                            id=f"agent-{index}",
                            user_id="user-1",
                            name=name,
                            config={"marker": name},
                        )
                    )
        finally:
            await database.close()

    async def inspect() -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        database = Database(database_url)
        try:
            async with UnitOfWork(database) as unit:
                store = unit.require_store()
                teams = await store.list_teams("user-1")
                agents = await store.list_agents("user-1")
                members = await store.list_team_members(teams[0].id) if teams else ()
            return (
                len(agents),
                tuple(agent.id for agent in agents),
                tuple(member.agent_id for member in members),
            )
        finally:
            await database.close()

    asyncio.run(seed())
    runner = CliRunner()
    dry_run = runner.invoke(
        app,
        ["migrate", "backfill-teams", "--database-url", database_url, "--dry-run"],
    )
    assert dry_run.exit_code == 0, dry_run.output
    dry_report = decode_report(dry_run.output)
    statuses = {entry["template"]: entry["status"] for entry in dry_report["manifest"]}
    assert statuses["finance-market-research"] == "candidate"
    assert statuses["world-cup-analysis"] == "conflict"
    assert statuses["benchmark-finance"] == "skipped"
    assert asyncio.run(inspect())[0] == len(finance_names) + 1

    manifest_path = tmp_path / "audit" / "teams.json"
    created = runner.invoke(
        app,
        [
            "migrate",
            "backfill-teams",
            "--database-url",
            database_url,
            "--manifest-path",
            str(manifest_path),
        ],
    )
    assert created.exit_code == 0, created.output
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == decode_report(created.output)
    agent_count, agent_ids, member_ids = asyncio.run(inspect())
    assert agent_count == len(finance_names) + 1
    assert set(member_ids) == set(agent_ids[:-1])

    repeated = runner.invoke(app, ["migrate", "backfill-teams", "--database-url", database_url])
    assert repeated.exit_code == 0, repeated.output
    repeated_report = decode_report(repeated.output)
    statuses = {entry["template"]: entry["status"] for entry in repeated_report["manifest"]}
    assert statuses["finance-market-research"] == "existing"

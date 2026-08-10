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
from fastclaw.teams import (
    TeamRole,
    TeamService,
    TeamValidationError,
    public_templates,
    resolve_template,
)


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
            model="deepseek-v4-flash",
            provider_name="deepseek",
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
            assert all(
                agent is not None
                and agent.config["model"] == "deepseek-v4-flash"
                and agent.config["provider"] == "deepseek"
                for agent in created
            )
            created_by_role = {
                member.role_key: agent
                for member, agent in zip(members, created, strict=True)
                if agent is not None
            }
            china_news = created_by_role["news-analyst"]
            assert china_news.config["skills"] == {"alwaysLoad": ["findata-toolkit-cn"]}
            assert china_news.config["allowedTools"] == [
                "exec",
                "web_fetch",
                "finance-tools.market_events",
            ]
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


@pytest.mark.anyio
async def test_general_football_team_creation_persists_role_prompts(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'football-team.db'}")
    await database.create_schema()
    try:
        async with UnitOfWork(database) as unit:
            await unit.require_store().save_user(
                UserRecord(id="usr_1", username="one", email="one@example.test", password_hash="x")
            )
        team, members = await TeamService(database).create(
            user_id="usr_1",
            name="European competition review",
            description="Competition-scoped football analysis",
            template_key="football-competition-analysis",
            client_request_id="football-team-request",
            model="deepseek-v4-flash",
            provider_name="deepseek",
        )

        assert team.status == "active"
        assert len(members) == 7
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            agents = {member.role_key: await store.get_agent(member.agent_id) for member in members}
        coordinator = agents["coordinator"]
        data_analyst = agents["data-analyst"]
        assert coordinator is not None
        assert coordinator.config["allowedTools"] == ["spawn_subagent", "football_ledger"]
        assert "season or edition" in coordinator.config["soul"]
        assert data_analyst is not None
        assert data_analyst.config["allowedTools"] == ["web_fetch"]
        assert "skills" not in data_analyst.config
    finally:
        await database.close()


def test_custom_team_requires_one_coordinator_and_specialist() -> None:
    with pytest.raises(TeamValidationError, match="exactly one coordinator"):
        from fastclaw.teams import validate_roles

        validate_roles((TeamRole("worker", "Worker", "specialist"),))


def test_custom_team_rejects_unsupported_member_types() -> None:
    from fastclaw.teams import validate_roles

    with pytest.raises(TeamValidationError, match="unsupported team member type: observer"):
        validate_roles(
            (
                TeamRole("coordinator", "Coordinator", "coordinator"),
                TeamRole("worker", "Worker", "specialist"),
                TeamRole("observer", "Observer", "observer"),
            )
        )


def test_benchmark_finance_template_contains_all_persisted_specialists() -> None:
    template = resolve_template("benchmark-finance")

    assert [role.name for role in template.roles] == [
        "Finance Research Coordinator",
        "Finance Accounting Analyst",
        "Finance Governance Specialist",
        "Finance Methodology Specialist",
        "Finance Retrieval Specialist",
        "Finance Risk Analyst",
        "Finance Source Specialist",
        "Finance Trend Analyst",
    ]


def test_general_football_template_is_public_and_competition_scoped() -> None:
    template = resolve_template("football-competition-analysis")

    assert template in public_templates()
    assert len(template.roles) == 7
    assert template.roles[0].allowed_tools == ("spawn_subagent", "football_ledger")
    assert all(role.allowed_tools == ("web_fetch",) for role in template.roles[1:])
    assert all(not role.skills for role in template.roles)
    combined_prompt = "\n".join(role.soul for role in template.roles)
    assert "competition" in combined_prompt.lower()
    assert "World Cup" not in combined_prompt
    assert "世界杯" not in combined_prompt


def test_benchmark_backfill_repairs_existing_team_without_creating_agents(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'benchmark-backfill.db'}"
    names = _BACKFILL_TEAM_MEMBERS["benchmark-finance"]
    template = resolve_template("benchmark-finance")

    async def inspect() -> tuple[int, int, tuple[str, ...], tuple[str, ...]]:
        database = Database(database_url)
        try:
            async with UnitOfWork(database) as unit:
                store = unit.require_store()
                agents = await store.list_agents("user-1")
                teams = await store.list_teams("user-1")
                members = await store.list_team_members("team-benchmark")
                team = teams[0]
            return (
                len(agents),
                team.revision,
                tuple(member.agent_id for member in members),
                tuple(member.role_key for member in members),
            )
        finally:
            await database.close()

    async def seed() -> None:
        database = Database(database_url)
        await database.create_schema()
        try:
            async with UnitOfWork(database) as unit:
                store = unit.require_store()
                await store.save_user(
                    UserRecord(
                        id="user-1",
                        username="benchmark",
                        email="benchmark@example.test",
                        password_hash="x",
                    )
                )
                for index, name in enumerate(names):
                    await store.save_agent(
                        AgentRecord(id=f"agent-{index}", user_id="user-1", name=name)
                    )
                await store.save_team(
                    AgentTeamRecord(
                        id="team-benchmark",
                        user_id="user-1",
                        name="Benchmark finance",
                        template_key="benchmark-finance",
                        template_version="v1",
                        status="active",
                        revision=4,
                        client_request_id="backfill:benchmark-finance:user-1",
                    )
                )
                for order, (role, _name) in enumerate(
                    zip(template.roles[:6], names[:6], strict=True)
                ):
                    await store.save_team_member(
                        AgentTeamMemberRecord(
                            team_id="team-benchmark",
                            agent_id=f"agent-{order}",
                            role_key=role.key,
                            member_type=role.member_type,
                            display_order=order,
                        )
                    )
        finally:
            await database.close()

    def decode_report(output: str) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads(output[output.index("{") :]))

    asyncio.run(seed())
    runner = CliRunner()
    dry_run = runner.invoke(
        app,
        ["migrate", "backfill-teams", "--database-url", database_url, "--dry-run"],
    )
    assert dry_run.exit_code == 0, dry_run.output
    dry_entry = next(
        entry
        for entry in decode_report(dry_run.output)["manifest"]
        if entry["template"] == "benchmark-finance"
    )
    assert dry_entry["status"] == "existing"
    assert dry_entry["missingAgentIds"] == ["agent-6", "agent-7"]
    assert asyncio.run(inspect()) == (
        8,
        4,
        tuple(f"agent-{index}" for index in range(6)),
        tuple(role.key for role in template.roles[:6]),
    )

    repaired = runner.invoke(app, ["migrate", "backfill-teams", "--database-url", database_url])
    assert repaired.exit_code == 0, repaired.output
    repaired_entry = next(
        entry
        for entry in decode_report(repaired.output)["manifest"]
        if entry["template"] == "benchmark-finance"
    )
    assert repaired_entry["status"] == "updated"
    assert repaired_entry["addedAgentIds"] == ["agent-6", "agent-7"]
    assert asyncio.run(inspect()) == (
        8,
        5,
        tuple(f"agent-{index}" for index in range(8)),
        tuple(role.key for role in template.roles),
    )

    repeated = runner.invoke(app, ["migrate", "backfill-teams", "--database-url", database_url])
    assert repeated.exit_code == 0, repeated.output
    repeated_entry = next(
        entry
        for entry in decode_report(repeated.output)["manifest"]
        if entry["template"] == "benchmark-finance"
    )
    assert repeated_entry["status"] == "existing"
    assert asyncio.run(inspect()) == (
        8,
        5,
        tuple(f"agent-{index}" for index in range(8)),
        tuple(role.key for role in template.roles),
    )


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

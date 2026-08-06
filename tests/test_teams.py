from __future__ import annotations

from pathlib import Path

import pytest

from fastclaw.storage import Database, UnitOfWork, UserRecord
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


def test_custom_team_requires_one_coordinator_and_specialist() -> None:
    with pytest.raises(TeamValidationError, match="exactly one coordinator"):
        from fastclaw.teams import validate_roles

        validate_roles((TeamRole("worker", "Worker", "specialist"),))

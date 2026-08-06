from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

from test_agent_manager import CoordinatingProvider

from fastclaw.agent.manager import AgentRuntimeConfig, AgentRuntimeManager
from fastclaw.cutover import CutoverExpectations, audit_cutover
from fastclaw.runtime import Runtime
from fastclaw.storage import (
    AgentFileRecord,
    AgentRecord,
    Database,
    SessionRecord,
    UnitOfWork,
    UserRecord,
)


async def test_cutover_audit_reports_profile_contracts_and_blockers(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    await database.create_schema()
    user = UserRecord(
        id="user-1",
        username="fixture",
        email="fixture@example.test",
        password_hash="unused",
        created_at=now,
        updated_at=now,
    )
    coordinator = AgentRecord(
        id="coordinator",
        user_id=user.id,
        name="Fixture Coordinator",
        config={"model": "fixture/coordinator", "policy": "delegate-only"},
        created_at=now,
        updated_at=now,
    )
    specialist = AgentRecord(
        id="specialist",
        user_id=user.id,
        name="Fixture Specialist",
        config={"model": "fixture/specialist", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(user)
        for agent in (coordinator, specialist):
            await store.save_agent(agent)
            await store.save_agent_file(
                AgentFileRecord(
                    agent_id=agent.id,
                    user_id=user.id,
                    filename="SOUL.md",
                    data=f"You are {agent.name}.".encode(),
                )
            )
        await store.save_session(
            SessionRecord(
                user_id=user.id,
                agent_id=coordinator.id,
                key="session-1",
                messages=[],
                created_at=now,
                updated_at=now,
            )
        )

    runtime = Runtime((CoordinatingProvider(specialist.id),))
    manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=tmp_path))
    await runtime.start()
    await manager.start()
    try:
        report = await audit_cutover(
            database,
            manager,
            expectations=CutoverExpectations(
                agents_by_username=MappingProxyType(
                    {"fixture": frozenset({coordinator.name, specialist.name})}
                ),
                required_role_profile_count=2,
                require_odds_when_declared=False,
            ),
        )
    finally:
        await manager.stop()
        await runtime.stop()
        await database.close()

    assert report.ready is True
    assert report.counts["users"] == 1
    assert report.counts["agents"] == 2
    assert report.counts["sessions"] == 1
    assert report.agents_by_username == {"fixture": 2}
    assert [item.policy for item in report.agents] == ["delegate-only", "no-tools"]
    assert all(item.role_files == ("SOUL.md",) for item in report.agents)
    assert report.providers == {"fixture": True}
    assert report.plugins[0].running is True


async def test_cutover_audit_fails_closed_for_missing_provider_and_role(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'blocked.db'}")
    await database.create_schema()
    user = UserRecord(
        id="user-1",
        username="fixture",
        email="fixture@example.test",
        password_hash="unused",
        created_at=now,
        updated_at=now,
    )
    agent = AgentRecord(
        id="agent-1",
        user_id=user.id,
        name="Agent",
        config={"model": "missing/model", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(user)
        await store.save_agent(agent)
    runtime = Runtime()
    manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=tmp_path))
    await runtime.start()
    await manager.start()
    try:
        report = await audit_cutover(
            database,
            manager,
            expectations=CutoverExpectations(
                agents_by_username=MappingProxyType({"fixture": frozenset({"Agent"})}),
                required_role_profile_count=1,
                require_sessions=False,
                require_plugins=False,
            ),
        )
    finally:
        await manager.stop()
        await runtime.stop()
        await database.close()

    assert report.ready is False
    assert "one or more required Providers are not configured" in report.blockers
    assert "role-file profile count is 0, expected 1" in report.blockers

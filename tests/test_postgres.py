from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from fastclaw.storage import (
    AgentRecord,
    ConfigRecord,
    Database,
    SessionRecord,
    UnitOfWork,
    UserRecord,
)

POSTGRES_URL = os.environ.get("FASTCLAW_POSTGRES_TEST_URL", "")


@pytest.mark.postgres
@pytest.mark.skipif(not POSTGRES_URL, reason="PostgreSQL integration URL is not configured")
async def test_postgres_alembic_repository_acl_and_json_round_trip() -> None:
    database = Database(POSTGRES_URL)
    await database.create_schema()
    now = datetime.now(UTC)
    try:
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            existing = await store.get_user("postgres-user")
            if existing is not None:
                await store.delete_user(existing.id)
            await store.save_user(
                UserRecord(
                    id="postgres-user",
                    username="postgres-fixture",
                    email="postgres@example.test",
                    password_hash="fixture",
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_agent(
                AgentRecord(
                    id="postgres-agent",
                    user_id="postgres-user",
                    name="PostgreSQL Agent",
                    config={"model": "fixture/model", "nested": {"enabled": True}},
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_session(
                SessionRecord(
                    user_id="postgres-user",
                    agent_id="postgres-agent",
                    key="postgres-session",
                    messages=[{"role": "user", "content": "hello"}],
                    message_count=1,
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_config(
                ConfigRecord(
                    id="postgres-config",
                    kind="provider",
                    scope="agent",
                    scope_id="postgres-agent",
                    user_id="postgres-user",
                    agent_id="postgres-agent",
                    name="fixture",
                    data={"models": [{"id": "fixture/model"}]},
                    created_at=now,
                    updated_at=now,
                )
            )

        async with database.session() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260805_01"
            )
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            agent = await store.get_agent("postgres-agent")
            session_record = await store.get_session(
                "postgres-user", "postgres-agent", "postgres-session"
            )
            configs = await store.list_configs(
                kind="provider", user_id="postgres-user", agent_id="postgres-agent"
            )
            assert agent is not None
            assert agent.config["nested"] == {"enabled": True}
            assert session_record is not None
            assert session_record.messages[0]["content"] == "hello"
            assert configs[0].data["models"][0]["id"] == "fixture/model"
            await store.delete_user("postgres-user")

        async with UnitOfWork(database) as unit:
            assert await unit.require_store().get_agent("postgres-agent") is None
    finally:
        await database.close()

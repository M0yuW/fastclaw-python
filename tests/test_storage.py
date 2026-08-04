from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from fastclaw.identity import (
    Identity,
    current_identity,
    hash_api_key,
    hash_password,
    require_identity,
    use_identity,
    verify_password,
)
from fastclaw.storage import (
    AgentRecord,
    APIKeyRecord,
    ConfigRecord,
    Database,
    SessionRecord,
    UnitOfWork,
    UserRecord,
)


def database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path}"


@pytest.mark.asyncio
async def test_sqlite_defaults_and_repository_round_trip(tmp_path: Path) -> None:
    database = Database(database_url(tmp_path / "fastclaw.db"))
    await database.create_schema()
    password_hash = hash_password("correct horse battery staple")
    api_hash = hash_api_key("fc_secret")

    try:
        async with database.session() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "20260805_01"
            )
            assert await session.scalar(text("PRAGMA journal_mode")) == "wal"
            assert await session.scalar(text("PRAGMA foreign_keys")) == 1
            assert await session.scalar(text("PRAGMA busy_timeout")) == 5000

        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_user(
                UserRecord(
                    id="user-1",
                    username="alice",
                    email="alice@example.test",
                    password_hash=password_hash,
                )
            )
            await store.save_agent(
                AgentRecord(
                    id="agent-1",
                    user_id="user-1",
                    name="Analyst",
                    is_public=True,
                )
            )
            await store.save_api_key(
                APIKeyRecord(
                    id="key-1",
                    user_id="user-1",
                    name="agent key",
                    key_hash=api_hash,
                    key_prefix="fc_secre",
                )
            )
            await store.set_api_key_agents("key-1", ["agent-1", "agent-1"])
            await store.save_session(
                SessionRecord(
                    user_id="user-1",
                    agent_id="agent-1",
                    key="session-1",
                    channel="web",
                    chat_id="chat-1",
                    messages=[{"role": "user", "content": "hello"}],
                    message_count=1,
                    chatter_user_id="user-1",
                )
            )
            await store.save_config(
                ConfigRecord(
                    id="config-1",
                    kind="provider",
                    scope="agent",
                    scope_id="agent-1",
                    user_id="user-1",
                    agent_id="agent-1",
                    name="primary",
                    data={"model": "test-model"},
                )
            )

        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            user = await store.get_user_by_login("alice@example.test")
            assert user is not None
            assert verify_password("correct horse battery staple", user.password_hash)
            assert not verify_password("wrong", user.password_hash)
            assert (await store.get_agent("agent-1")).is_public  # type: ignore[union-attr]
            assert await store.api_key_can_access_agent("key-1", "agent-1")
            session_record = await store.get_session("user-1", "agent-1", "session-1")
            assert session_record is not None
            assert session_record.channel == "web"
            configs = await store.list_configs(
                kind="provider", user_id="user-1", agent_id="agent-1"
            )
            assert configs[0].data == {"model": "test-model"}
    finally:
        await database.close()


def test_trusted_identity_context_is_scoped_and_act_as_is_read_only() -> None:
    identity = Identity(
        user_id="admin",
        role="super_admin",
        auth_method="cookie",
        act_as_user_id="alice",
    )

    assert current_identity() is None
    with use_identity(identity):
        assert require_identity().effective_user_id == "alice"
        assert require_identity().read_only
        assert require_identity().can_access_agent("any-agent")
    assert current_identity() is None


def test_api_key_identity_is_limited_to_explicit_agent_acl() -> None:
    identity = Identity(
        user_id="alice",
        role="user",
        auth_method="apikey",
        api_key_id="key-1",
        api_key_agents=("allowed",),
    )

    assert identity.can_access_agent("allowed")
    assert not identity.can_access_agent("other")

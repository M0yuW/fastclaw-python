from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from fastclaw.agent.manager import (
    AgentManagerShutdownError,
    AgentRuntimeConfig,
    AgentRuntimeManager,
)
from fastclaw.providers import (
    ChatRequest,
    ChatResponse,
    MessageRole,
    ProviderEvent,
    ProviderEventType,
    ProviderStream,
)
from fastclaw.runtime import Runtime
from fastclaw.storage import (
    AgentFileRecord,
    AgentRecord,
    ConfigRecord,
    Database,
    UnitOfWork,
    UserRecord,
)


class CoordinatingProvider:
    name = "fixture"

    def __init__(self, specialist_id: str) -> None:
        self.specialist_id = specialist_id
        self.requests: list[ChatRequest] = []

    async def start(self, client: httpx.AsyncClient) -> None:
        del client

    async def stop(self) -> None:
        pass

    async def ready(self) -> bool:
        return True

    async def chat(self, request: ChatRequest) -> ChatResponse:
        stream = self.stream(request)
        async for _ in stream:
            pass
        return stream.result()

    def stream(self, request: ChatRequest) -> ProviderStream:
        self.requests.append(request)

        async def events() -> AsyncIterator[ProviderEvent]:
            if request.model == "fixture/specialist":
                yield ProviderEvent(
                    type=ProviderEventType.CONTENT_DELTA,
                    content="specialist answer",
                )
                yield ProviderEvent(type=ProviderEventType.DONE, finish_reason="stop")
                return
            tool_result = next(
                (
                    message
                    for message in reversed(request.messages)
                    if message.role is MessageRole.TOOL
                ),
                None,
            )
            if tool_result is not None:
                yield ProviderEvent(
                    type=ProviderEventType.CONTENT_DELTA,
                    content=f"coordinator: {tool_result.content}",
                )
                yield ProviderEvent(type=ProviderEventType.DONE, finish_reason="stop")
                return
            yield ProviderEvent(
                type=ProviderEventType.TOOL_CALL_DELTA,
                tool_index=0,
                tool_name="spawn_subagent",
                tool_arguments=(f'{{"agent_id":"{self.specialist_id}","task":"investigate"}}'),
            )
            yield ProviderEvent(type=ProviderEventType.DONE, finish_reason="tool_calls")

        return ProviderStream(events())


class BlockingProvider(CoordinatingProvider):
    def __init__(self) -> None:
        super().__init__("")
        self.closed = asyncio.Event()

    def stream(self, request: ChatRequest) -> ProviderStream:
        self.requests.append(request)

        async def events() -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="partial")
                await asyncio.Event().wait()
            finally:
                self.closed.set()

        return ProviderStream(events())


async def build_manager(
    path: Path,
    provider: CoordinatingProvider,
    agents: tuple[AgentRecord, ...],
) -> tuple[AgentRuntimeManager, Runtime, Database]:
    database = Database(f"sqlite+aiosqlite:///{path}")
    await database.create_schema()
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        now = datetime.now(UTC)
        await store.save_user(
            UserRecord(
                id="user-1",
                username="fixture",
                email="fixture@example.test",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        for agent in agents:
            await store.save_agent(agent)
    runtime = Runtime((provider,))
    await runtime.start()
    manager = AgentRuntimeManager(
        database,
        runtime,
        AgentRuntimeConfig(data_root=path.parent, max_concurrent=1),
    )
    await manager.start()
    return manager, runtime, database


async def close_manager(manager: AgentRuntimeManager, runtime: Runtime, database: Database) -> None:
    await manager.stop()
    await runtime.stop()
    await database.close()


async def test_manager_shutdown_attempts_plugin_cleanup_after_bus_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}")
    runtime = Runtime()
    manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=tmp_path))
    plugin_stopped = False

    async def fail_bus_shutdown() -> None:
        raise OSError("fixture bus shutdown failed")

    async def track_plugin_stop() -> None:
        nonlocal plugin_stopped
        plugin_stopped = True

    monkeypatch.setattr(manager.bus, "shutdown", fail_bus_shutdown)
    monkeypatch.setattr(manager.plugin_manager, "stop", track_plugin_stop)

    with pytest.raises(AgentManagerShutdownError) as error:
        await manager.stop()

    assert plugin_stopped
    assert len(error.value.errors) == 1
    assert isinstance(error.value.errors[0], OSError)


async def test_manager_can_load_profiles_without_starting_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'read-only-check.db'}")
    await database.create_schema()
    runtime = Runtime()
    await runtime.start()
    manager = AgentRuntimeManager(
        database,
        runtime,
        AgentRuntimeConfig(data_root=tmp_path, enable_plugins=False),
    )

    async def unexpected_plugin_start() -> None:
        raise AssertionError("read-only profile inspection started plugins")

    monkeypatch.setattr(manager.plugin_manager, "start", unexpected_plugin_start)
    try:
        await manager.start()

        assert manager.started
        assert manager.plugin_manager.enabled == set()
        assert all(not instance.process.running for instance in manager.plugin_manager.instances)
    finally:
        await manager.stop()
        await runtime.stop()
        await database.close()


async def test_root_and_nested_runs_share_queue_without_deadlock(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    coordinator = AgentRecord(
        id="coordinator",
        user_id="user-1",
        name="Coordinator",
        config={
            "model": "fixture/coordinator",
            "policy": "delegate-only",
            "maxToolIterations": 3,
            "maxTokens": 8192,
            "temperature": 0.1,
        },
        created_at=now,
        updated_at=now,
    )
    specialist = AgentRecord(
        id="specialist",
        user_id="user-1",
        name="Specialist",
        config={"model": "fixture/specialist", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider(specialist.id)
    manager, runtime, database = await build_manager(
        tmp_path / "manager.db", provider, (coordinator, specialist)
    )
    try:
        result = await asyncio.wait_for(
            manager.chat(
                user_id="user-1",
                agent_id=coordinator.id,
                session_id="shared-session",
                message="coordinate",
            ),
            timeout=2,
        )

        assert result.content == "coordinator: specialist answer"
        coordinator_requests = [
            request for request in provider.requests if request.model == "fixture/coordinator"
        ]
        specialist_requests = [
            request for request in provider.requests if request.model == "fixture/specialist"
        ]
        assert len(coordinator_requests) == 2
        assert len(specialist_requests) == 1
        assert coordinator_requests[0].max_tokens == 8192
        assert coordinator_requests[0].temperature == 0.1
        assert [tool.function.name for tool in coordinator_requests[0].tools] == ["spawn_subagent"]
        assert specialist_requests[0].tools == ()
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            root_session = await store.get_session("user-1", coordinator.id, "shared-session")
            child_session = await store.get_session("user-1", specialist.id, "shared-session")
        assert root_session is not None
        assert child_session is not None
        assert root_session.agent_id != child_session.agent_id
    finally:
        await close_manager(manager, runtime, database)


async def test_closing_root_stream_cancels_provider_and_does_not_persist_partial(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="blocking",
        user_id="user-1",
        name="Blocking",
        config={"model": "fixture/blocking", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = BlockingProvider()
    manager, runtime, database = await build_manager(tmp_path / "cancel.db", provider, (agent,))
    try:
        stream = await manager.stream(
            user_id="user-1",
            agent_id=agent.id,
            session_id="cancelled",
            message="start",
        )
        assert (await anext(stream)).content == "partial"
        await stream.aclose()
        await asyncio.wait_for(provider.closed.wait(), timeout=1)
        async with UnitOfWork(database) as unit:
            stored = await unit.require_store().get_session("user-1", agent.id, "cancelled")
        assert stored is None
    finally:
        await close_manager(manager, runtime, database)


async def test_named_provider_credential_is_resolved_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTCLAW_PROVIDER_DEEPSEEK_API_KEY", "central-secret")
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="deepseek-agent",
        user_id="user-1",
        name="DeepSeek",
        config={"model": "deepseek/deepseek-v4-flash", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider.db'}")
    await database.create_schema()
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(
            UserRecord(
                id="user-1",
                username="fixture",
                email="fixture@example.test",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await store.save_agent(agent)
        await store.save_config(
            ConfigRecord(
                id="deepseek-provider",
                kind="provider",
                scope="system",
                name="deepseek",
                data={
                    "apiBase": "https://deepseek.example/v1",
                    "apiType": "openai-compatible",
                },
                created_at=now,
                updated_at=now,
            )
        )
    runtime = Runtime()
    await runtime.start()
    manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=tmp_path))
    await manager.start()
    try:
        profile = await manager.profile(agent.id, agent.user_id)
        selected = await manager.provider_selection(profile)
        readiness = await manager.readiness()

        assert selected.api_key == "central-secret"
        assert selected.api_base == "https://deepseek.example/v1"
        assert selected.model == "deepseek/deepseek-v4-flash"
        assert readiness["providers"] is True
    finally:
        await close_manager(manager, runtime, database)


async def test_profile_loads_skill_by_frontmatter_name_and_requires_preparation(
    tmp_path: Path,
) -> None:
    skill_root = tmp_path / "skills" / "directory-name-differs"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        """---
name: imported-skill-name
description: Fixture
---

Read /legacy/.fastclaw/data before answering.
""",
        encoding="utf-8",
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'skill-profile.db'}")
    await database.create_schema()
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="skill-agent",
        user_id="user-1",
        name="Skill Agent",
        config={"model": "fixture/skill"},
        created_at=now,
        updated_at=now,
    )
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(
            UserRecord(
                id="user-1",
                username="fixture",
                email="fixture@example.test",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await store.save_agent(agent)
        await store.save_agent_file(
            AgentFileRecord(
                agent_id=agent.id,
                user_id=agent.user_id,
                filename="agent.json",
                data=b'{"skills":{"alwaysLoad":["imported-skill-name"]}}',
            )
        )
    provider = CoordinatingProvider("")
    runtime = Runtime((provider,))
    await runtime.start()
    manager = AgentRuntimeManager(
        database,
        runtime,
        AgentRuntimeConfig(
            data_root=tmp_path,
            legacy_data_root=Path("/legacy/.fastclaw"),
        ),
    )
    await manager.start()
    try:
        profile = await manager.profile(agent.id, agent.user_id)

        assert [skill.name for skill in profile.skills] == ["imported-skill-name"]
        assert profile.skills[0].root.name == "directory-name-differs"
        assert "/legacy/.fastclaw" not in profile.system_prompt
        assert str(tmp_path) in profile.system_prompt
        assert (await manager.readiness())["skills"] is False

        await manager.skill_catalog.prepare(profile.skills[0])
        assert (await manager.readiness())["skills"] is True
    finally:
        await close_manager(manager, runtime, database)


async def test_openrouter_standard_endpoint_requires_only_the_central_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FASTCLAW_PROVIDER_OPENROUTER_API_KEY", "central-secret")
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="openrouter-agent",
        user_id="user-1",
        name="OpenRouter",
        config={"model": "openrouter/google/gemini-fixture", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider("")
    manager, runtime, database = await build_manager(tmp_path / "openrouter.db", provider, (agent,))
    try:
        # The fixture Runtime provider has a different name, so resolution uses
        # the official OpenRouter compatibility endpoint.
        selected = await manager.provider_selection(await manager.profile(agent.id, agent.user_id))

        assert selected.api_base == "https://openrouter.ai/api/v1"
        assert selected.api_key == "central-secret"
        assert selected.source == "environment-default"
    finally:
        await close_manager(manager, runtime, database)


async def test_agent_without_agent_json_inherits_system_defaults(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="leo",
        user_id="user-1",
        name="LEO",
        config={},
        created_at=now,
        updated_at=now,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'defaults.db'}")
    await database.create_schema()
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(
            UserRecord(
                id="user-1",
                username="fixture",
                email="fixture@example.test",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await store.save_agent(agent)
        await store.save_config(
            ConfigRecord(
                id="system-defaults",
                kind="setting",
                scope="system",
                name="agents.defaults",
                data={"model": "deepseek/deepseek-v4-pro", "maxToolIterations": 20},
                created_at=now,
                updated_at=now,
            )
        )
    runtime = Runtime()
    await runtime.start()
    manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=tmp_path))
    await manager.start()
    try:
        profile = await manager.profile(agent.id, agent.user_id)

        assert profile.agent.config["model"] == "deepseek/deepseek-v4-pro"
        assert profile.agent.config["maxToolIterations"] == 20
    finally:
        await close_manager(manager, runtime, database)

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
    AgentRuntimeProfile,
    _explicit_ev_request,
    _explicit_sporttery_request,
)
from fastclaw.agent.models import AgentRunError
from fastclaw.execution import ExecutionContext
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
    AgentTeamMemberRecord,
    AgentTeamRecord,
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


def test_ev_dispatch_requires_explicit_ev_intent() -> None:
    assert not _explicit_ev_request("分析这两场比赛的基本面和战术")
    assert not _explicit_ev_request("请补充赔率和 Sporttery 价格")
    assert not _explicit_ev_request("分析盘口、市场价格和去水概率")
    assert _explicit_ev_request("请补充赔率、EV 和 Sporttery 价格")
    assert _explicit_ev_request("请让EV专家做期望值分析")
    assert _explicit_ev_request("获取ev建议")
    assert _explicit_ev_request("获取EV建议")
    assert not _explicit_ev_request("event data")
    assert not _explicit_sporttery_request("请给普通独立赔率")
    assert _explicit_sporttery_request("请补充 Sporttery 官方SP")
    assert _explicit_sporttery_request("请读取竞彩赔率")


@pytest.mark.asyncio
async def test_ev_delegation_is_skipped_without_provider_call(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    ev_agent = AgentRecord(
        id="ev-analyst",
        user_id="user-1",
        name="Football EV analyst",
        config={"model": "fixture/specialist", "teamRole": "ev-analyst"},
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider(ev_agent.id)
    manager, runtime, database = await build_manager(
        tmp_path / "ev-skip.db", provider, (ev_agent,)
    )
    try:
        result = await manager._delegated_chat(
            ev_agent.id,
            "analyze odds",
            ExecutionContext(
                user_id="user-1",
                agent_id="coordinator",
                session_id="session-1",
                root_execution_id="run-1",
            ),
        )

        assert "EV analysis skipped" in result
        assert provider.requests == []
    finally:
        await close_manager(manager, runtime, database)


@pytest.mark.asyncio
async def test_existing_football_coordinator_gets_prediction_contract(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    coordinator = AgentRecord(
        id="football-coordinator",
        user_id="user-1",
        name="Football analysis coordinator",
        config={
            "model": "fixture/coordinator",
            "teamRole": "coordinator",
            "scopeGuard": "football",
            "allowedTools": ["spawn_subagent", "football_ledger"],
        },
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider("")
    manager, runtime, database = await build_manager(
        tmp_path / "football-coordinator.db", provider, (coordinator,)
    )
    try:
        profile = await manager.profile(coordinator.id, "user-1")

        assert "Football prediction output contract" in profile.system_prompt
        assert "one row for every requested match" in profile.system_prompt
        assert "1X2 is exactly home win, draw, or away win" in profile.system_prompt
        assert "two required phases" in profile.system_prompt
        assert "independent data-only 1X2 lean" in profile.system_prompt
        assert "never tell the data analyst 'do not predict'" in profile.system_prompt
        assert "never describe the whole batch as missing" in profile.system_prompt
        assert "one exact full-time score prediction" in profile.system_prompt
        assert "ht_score_pred, ft_score_pred" in profile.system_prompt
    finally:
        await close_manager(manager, runtime, database)


@pytest.mark.asyncio
async def test_prediction_data_delegation_requires_evidence_then_data_lean(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    data_agent = AgentRecord(
        id="data-analyst",
        user_id="user-1",
        name="Competition data analyst",
        config={
            "model": "fixture/specialist",
            "teamRole": "data-analyst",
            "allowedTools": ["football_data", "football_context"],
        },
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider(data_agent.id)
    manager, runtime, database = await build_manager(
        tmp_path / "data-two-phase.db", provider, (data_agent,)
    )
    execution = ExecutionContext(
        user_id="user-1",
        agent_id=data_agent.id,
        session_id="session-1",
        root_execution_id="run-1",
    )
    try:
        result = await manager._delegated_chat(
            data_agent.id,
            "Analyze Home FC vs Away FC for a pre-match prediction",
            execution,
        )

        assert result == "specialist answer"
        request = provider.requests[-1]
        user_message = next(
            message for message in reversed(request.messages) if message.role is MessageRole.USER
        )
        assert isinstance(user_message.content, str)
        assert "action=base_evidence" in user_message.content
        assert "complete the second phase" in user_message.content
        assert "independent data-only 1X2 judgment" in user_message.content
        assert "report per-fixture status" in user_message.content
        assert "Do not call football_odds or Sporttery" in user_message.content
    finally:
        await close_manager(manager, runtime, database)


@pytest.mark.asyncio
async def test_settlement_data_delegation_exposes_results_tool_only(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    data_agent = AgentRecord(
        id="data-analyst",
        user_id="user-1",
        name="Competition data analyst",
        config={
            "model": "fixture/specialist",
            "teamRole": "data-analyst",
            "allowedTools": ["football_data", "football_context"],
        },
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider(data_agent.id)
    manager, runtime, database = await build_manager(
        tmp_path / "settlement-tools.db", provider, (data_agent,)
    )
    execution = ExecutionContext(
        user_id="user-1",
        agent_id=data_agent.id,
        session_id="session-1",
        root_execution_id="run-1",
    )
    try:
        result = await manager._delegated_chat(
            data_agent.id,
            "复盘账本并核对已结束比赛赛果",
            execution,
        )

        assert result == "specialist answer"
        request = provider.requests[-1]
        assert {tool.function.name for tool in request.tools} == {"football_data"}
        assert execution.shared_state.football_settlement_review is True
    finally:
        await close_manager(manager, runtime, database)


def test_existing_football_specialists_fail_closed_when_config_lacks_policy() -> None:
    now = datetime.now(UTC)
    profile = AgentRuntimeProfile(
        agent=AgentRecord(
            id="football-specialist",
            user_id="user-1",
            name="Competition data analyst",
            config={"teamMemberType": "specialist"},
            created_at=now,
            updated_at=now,
        ),
        system_prompt="",
        allowed_tools=frozenset({"football_data", "web_fetch"}),
    )

    request = AgentRuntimeManager._request(profile, "fixture/specialist", "check match")

    assert request.max_failed_tool_rounds == 1


def test_explicit_specialist_retry_policy_is_preserved() -> None:
    now = datetime.now(UTC)
    profile = AgentRuntimeProfile(
        agent=AgentRecord(
            id="football-specialist",
            user_id="user-1",
            name="Competition data analyst",
            config={"teamMemberType": "specialist", "maxFailedToolRounds": 0},
            created_at=now,
            updated_at=now,
        ),
        system_prompt="",
        allowed_tools=frozenset({"football_data", "web_fetch"}),
    )

    request = AgentRuntimeManager._request(profile, "fixture/specialist", "check match")

    assert request.max_failed_tool_rounds == 0


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


class ResumableProvider(CoordinatingProvider):
    """Provider fixture that can finish after its HTTP consumer detaches."""

    def __init__(self) -> None:
        super().__init__("")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = asyncio.Event()

    def stream(self, request: ChatRequest) -> ProviderStream:
        self.requests.append(request)

        async def events() -> AsyncIterator[ProviderEvent]:
            try:
                self.started.set()
                yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="partial")
                await self.release.wait()
                yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="completed")
                yield ProviderEvent(type=ProviderEventType.DONE, finish_reason="stop")
            finally:
                self.finished.set()

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
        assert "emit several ordinary `spawn_subagent` tool calls" in str(
            coordinator_requests[0].messages[0].content
        )
        assert "Do not use legacy `delegations`, `sharedContext`, or `agentId`" in str(
            coordinator_requests[0].messages[0].content
        )
        assert specialist_requests[0].tools == ()
        assert all(
            "Runtime delegation contract" not in str(message.content)
            for message in specialist_requests[0].messages
        )
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            root_session = await store.get_session("user-1", coordinator.id, "shared-session")
            child_session = await store.get_session("user-1", specialist.id, "shared-session")
        assert root_session is not None
        assert child_session is not None
        assert root_session.agent_id != child_session.agent_id
    finally:
        await close_manager(manager, runtime, database)


async def test_team_coordinator_has_a_roster_enum_and_rejects_non_team_delegation(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    coordinator = AgentRecord(
        id="coordinator",
        user_id="user-1",
        name="Coordinator",
        config={"model": "fixture/coordinator", "policy": "delegate-only"},
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
    outsider = AgentRecord(
        id="outsider",
        user_id="user-1",
        name="Outsider",
        config={"model": "fixture/specialist", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = CoordinatingProvider(specialist.id)
    manager, runtime, database = await build_manager(
        tmp_path / "team-manager.db", provider, (coordinator, specialist, outsider)
    )
    try:
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            await store.save_team(
                AgentTeamRecord(
                    id="team-1",
                    user_id="user-1",
                    name="Team",
                    status="active",
                    client_request_id="team-request",
                    created_at=now,
                    updated_at=now,
                )
            )
            await store.save_team_member(
                AgentTeamMemberRecord(
                    team_id="team-1",
                    agent_id=coordinator.id,
                    role_key="coordinator",
                    member_type="coordinator",
                )
            )
            await store.save_team_member(
                AgentTeamMemberRecord(
                    team_id="team-1",
                    agent_id=specialist.id,
                    role_key="research",
                    member_type="specialist",
                    display_order=1,
                )
            )
        await manager.reload_profile(coordinator)
        await manager.reload_profile(specialist)

        result = await manager.chat(
            user_id="user-1",
            agent_id=coordinator.id,
            session_id="team-session",
            message="coordinate",
        )

        assert result.content == "coordinator: specialist answer"
        first_request = next(
            request for request in provider.requests if request.model == "fixture/coordinator"
        )
        properties = first_request.tools[0].function.parameters["properties"]
        assert isinstance(properties, dict)
        agent_id_schema = properties["agent_id"]
        assert isinstance(agent_id_schema, dict)
        assert agent_id_schema == {
            "type": "string",
            "enum": [specialist.id],
        }
        profile = await manager.profile(coordinator.id, "user-1")
        assert specialist.id in profile.system_prompt
        assert "research" in profile.system_prompt

        with pytest.raises(AgentRunError, match="team delegation"):
            await manager._delegated_chat(
                specialist.id,
                "bypass",
                ExecutionContext(
                    user_id="user-1",
                    agent_id=specialist.id,
                    session_id="team-session",
                    root_execution_id="team-root",
                    call_path=(outsider.id, specialist.id),
                ),
            )
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
        assert (
            await manager.cancel_session_roots(
                user_id="user-1", agent_id=agent.id, session_id="cancelled"
            )
            == 1
        )
        await stream.aclose()
        await asyncio.wait_for(provider.closed.wait(), timeout=1)
        assert not manager._session_roots
        async with UnitOfWork(database) as unit:
            stored = await unit.require_store().get_session("user-1", agent.id, "cancelled")
        assert stored is None
    finally:
        await close_manager(manager, runtime, database)


async def test_detaching_root_stream_keeps_agent_running_and_persists_final_session(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="resumable",
        user_id="user-1",
        name="Resumable",
        config={"model": "fixture/resumable", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = ResumableProvider()
    manager, runtime, database = await build_manager(tmp_path / "detach.db", provider, (agent,))
    try:
        stream = await manager.stream(
            user_id="user-1",
            agent_id=agent.id,
            session_id="detached",
            message="continue after refresh",
        )
        assert (await anext(stream)).content == "partial"

        stream.detach()
        provider.release.set()
        await asyncio.wait_for(provider.finished.wait(), timeout=1)

        stored = None
        for _ in range(100):
            async with UnitOfWork(database) as unit:
                stored = await unit.require_store().get_session("user-1", agent.id, "detached")
            if stored is not None:
                break
            await asyncio.sleep(0.01)

        assert stored is not None
        assert stored.messages[-1]["role"] == "assistant"
        assert stored.messages[-1]["content"] == "partialcompleted"
    finally:
        await close_manager(manager, runtime, database)


async def test_cancelled_non_stream_chat_detaches_instead_of_cancelling_agent(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="resumable-chat",
        user_id="user-1",
        name="Resumable chat",
        config={"model": "fixture/resumable", "policy": "no-tools"},
        created_at=now,
        updated_at=now,
    )
    provider = ResumableProvider()
    manager, runtime, database = await build_manager(
        tmp_path / "detach-chat.db", provider, (agent,)
    )
    try:
        request = asyncio.create_task(
            manager.chat(
                user_id="user-1",
                agent_id=agent.id,
                session_id="detached-chat",
                message="continue after request cancellation",
            )
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

        provider.release.set()
        await asyncio.wait_for(provider.finished.wait(), timeout=1)

        stored = None
        for _ in range(100):
            async with UnitOfWork(database) as unit:
                stored = await unit.require_store().get_session("user-1", agent.id, "detached-chat")
            if stored is not None:
                break
            await asyncio.sleep(0.01)

        assert stored is not None
        assert stored.messages[-1]["content"] == "partialcompleted"
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


async def test_agent_database_scope_overrides_stale_agent_json(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    agent = AgentRecord(
        id="production-agent",
        user_id="user-1",
        name="Production Agent",
        config={"description": "explicit agents.config value"},
        created_at=now,
        updated_at=now,
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'precedence.db'}")
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
        await store.save_agent_file(
            AgentFileRecord(
                agent_id=agent.id,
                user_id=agent.user_id,
                filename="agent.json",
                data=b'{"model":"openrouter/legacy-model","temperature":0.4}',
                updated_at=now,
            )
        )
        await store.save_config(
            ConfigRecord(
                id="system-defaults",
                kind="setting",
                scope="system",
                name="agents.defaults",
                data={"model": "deepseek/system-default", "maxToolIterations": 10},
                created_at=now,
                updated_at=now,
            )
        )
        await store.save_config(
            ConfigRecord(
                id="agent-defaults",
                kind="setting",
                scope="agent",
                scope_id=agent.id,
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
        assert profile.agent.config["temperature"] == 0.4
        assert profile.agent.config["description"] == "explicit agents.config value"
    finally:
        await close_manager(manager, runtime, database)

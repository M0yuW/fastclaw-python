from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest
from pydantic import ValidationError

from fastclaw.agent import (
    AgentEventType,
    AgentRunner,
    AgentRunRequest,
    DatabaseSessionPersistence,
)
from fastclaw.execution import ExecutionContext
from fastclaw.migration import import_go_database
from fastclaw.providers import (
    ChatRequest,
    ChatResponse,
    ProviderEvent,
    ProviderEventType,
    ProviderStream,
    ToolDefinition,
    ToolFunction,
)
from fastclaw.storage import Database, SessionRecord
from fastclaw.tools import ToolRegistry, ToolResult


class StubPersistence:
    def __init__(self, stored: SessionRecord | None = None) -> None:
        self.stored = stored
        self.saved: list[SessionRecord] = []

    async def load(self, user_id: str, agent_id: str, session_id: str) -> SessionRecord | None:
        del user_id, agent_id, session_id
        return self.stored

    async def save(self, session: SessionRecord) -> None:
        self.saved.append(session)


class EchoTool:
    definition = ToolDefinition(
        function=ToolFunction(
            name="echo",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
    )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        return ToolResult(content=f"{context.user_id}:{arguments['text']}")


def run_context() -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="run-1",
        call_path=("agent-1",),
    )


class ScriptedProvider:
    name = "scripted"

    def __init__(self, scripts: list[tuple[ProviderEvent, ...]]) -> None:
        self.scripts = scripts
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
        script = self.scripts[len(self.requests) - 1]

        async def source() -> AsyncIterator[ProviderEvent]:
            for event in script:
                yield event

        return ProviderStream(source())


def tool_round() -> tuple[ProviderEvent, ...]:
    return (
        ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_name="echo",
            tool_arguments='{"text":"hello"}',
        ),
        ProviderEvent(type=ProviderEventType.DONE, finish_reason="tool_calls"),
    )


def delegation_round() -> tuple[ProviderEvent, ...]:
    return (
        ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_name="spawn_subagent",
            tool_arguments='{"agent_id":"specialist","task":"research"}',
        ),
        ProviderEvent(type=ProviderEventType.DONE, finish_reason="tool_calls"),
    )


def final_round() -> tuple[ProviderEvent, ...]:
    return (
        ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="finished"),
        ProviderEvent(type=ProviderEventType.DONE, finish_reason="stop"),
    )


def batch_tool_round() -> tuple[ProviderEvent, ...]:
    return (
        ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_name="batch_echo",
            tool_arguments='{"text":"first"}',
        ),
        ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=1,
            tool_name="batch_echo",
            tool_arguments='{"text":"second"}',
        ),
        ProviderEvent(type=ProviderEventType.DONE, finish_reason="tool_calls"),
    )


class BatchEchoTool:
    definition = ToolDefinition(
        function=ToolFunction(
            name="batch_echo",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        )
    )

    def __init__(self) -> None:
        self.batches: list[tuple[str, ...]] = []

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del arguments, context
        raise AssertionError("batch-capable tool was executed serially")

    async def execute_many(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
    ) -> tuple[ToolResult, ...]:
        del context
        texts = tuple(str(item["text"]) for item in arguments)
        self.batches.append(texts)
        return tuple(ToolResult(content=text.upper()) for text in texts)


def test_chat_request_cannot_carry_tenant_identity() -> None:
    with pytest.raises(ValidationError, match="userId"):
        AgentRunRequest.model_validate({"model": "fixture", "message": "hello", "userId": "forged"})


@pytest.mark.asyncio
async def test_react_loop_calls_provider_once_per_round_and_persists_final_history() -> None:
    provider = ScriptedProvider([tool_round(), final_round()])
    persistence = StubPersistence()
    runner = AgentRunner(provider, ToolRegistry([EchoTool()]), persistence)

    stream = runner.stream(
        AgentRunRequest(
            model="fixture",
            message="start",
        ),
        run_context(),
    )
    events = [event async for event in stream]

    assert stream.result().content == "finished"
    assert len(provider.requests) == 2
    assert [event.seq for event in events] == list(range(len(events)))
    assert [event.type for event in events] == [
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.CONTENT_DELTA,
        AgentEventType.CONTENT,
        AgentEventType.DONE,
    ]
    assert events[0].tool_call is not None
    assert events[0].tool_call.id == "tool-call-0"
    assert events[1].tool_result == "user-1:hello"
    assert len(persistence.saved) == 1
    saved = persistence.saved[0]
    assert [message["role"] for message in saved.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert saved.messages[1]["_raw"]["tool_calls"][0]["function"]["name"] == "echo"


@pytest.mark.asyncio
async def test_agent_run_logs_provider_and_tool_stages(caplog: pytest.LogCaptureFixture) -> None:
    provider = ScriptedProvider([tool_round(), final_round()])
    runner = AgentRunner(provider, ToolRegistry([EchoTool()]), StubPersistence())
    context = run_context()
    context = ExecutionContext(
        user_id=context.user_id,
        agent_id=context.agent_id,
        session_id=context.session_id,
        root_execution_id=context.root_execution_id,
        call_path=context.call_path,
        task_id="delegation-1",
    )

    with caplog.at_level(logging.INFO):
        stream = runner.stream(AgentRunRequest(model="deepseek/model", message="start"), context)
        _ = [event async for event in stream]

    stages = [getattr(record, "stage", "") for record in caplog.records if hasattr(record, "stage")]
    assert stages == [
        "provider_started",
        "provider_finished",
        "tool_started",
        "tool_finished",
        "provider_started",
        "provider_finished",
    ]
    assert all(
        getattr(record, "task_id", "") == "delegation-1"
        for record in caplog.records
        if hasattr(record, "stage")
    )


@pytest.mark.asyncio
async def test_react_loop_uses_ordered_batch_protocol_for_one_model_round() -> None:
    provider = ScriptedProvider([batch_tool_round(), final_round()])
    persistence = StubPersistence()
    tool = BatchEchoTool()
    stream = AgentRunner(provider, ToolRegistry([tool]), persistence).stream(
        AgentRunRequest(model="fixture", message="batch"),
        run_context(),
    )

    events = [event async for event in stream]

    assert stream.result().content == "finished"
    assert tool.batches == [("first", "second")]
    assert [event.type for event in events] == [
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.TOOL_RESULT,
        AgentEventType.CONTENT_DELTA,
        AgentEventType.CONTENT,
        AgentEventType.DONE,
    ]
    assert [
        message.content for message in provider.requests[1].messages if message.role.value == "tool"
    ] == ["FIRST", "SECOND"]


class BlockingProvider(ScriptedProvider):
    def __init__(self) -> None:
        super().__init__([])
        self.closed = False

    def stream(self, request: ChatRequest) -> ProviderStream:
        self.requests.append(request)

        async def source() -> AsyncIterator[ProviderEvent]:
            try:
                yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="partial")
                await anyio.sleep_forever()
            finally:
                self.closed = True

        return ProviderStream(source())


@pytest.mark.asyncio
async def test_stop_closes_provider_stream_and_does_not_persist_partial_assistant() -> None:
    provider = BlockingProvider()
    persistence = StubPersistence()
    stream = AgentRunner(provider, ToolRegistry(), persistence).stream(
        AgentRunRequest(
            model="fixture",
            message="start",
        ),
        run_context(),
    )

    assert (await anext(stream)).content == "partial"
    await stream.aclose()

    assert provider.closed
    assert persistence.saved == []


class FailingTool(EchoTool):
    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del arguments, context
        raise OSError("fixture failure")


class SlowTool(EchoTool):
    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del arguments, context
        await anyio.sleep_forever()
        raise AssertionError("sleep_forever returned")


class SlowDelegationTool:
    definition = ToolDefinition(
        function=ToolFunction(
            name="spawn_subagent",
            parameters={"type": "object"},
        )
    )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del arguments, context
        await anyio.sleep(0.03)
        return ToolResult(content="specialist evidence")


class DirectReturnTool(EchoTool):
    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del arguments, context
        return ToolResult(content="authoritative report", direct_return=True)


@pytest.mark.asyncio
async def test_direct_return_finishes_without_a_second_model_request() -> None:
    provider = ScriptedProvider([tool_round()])
    persistence = StubPersistence()
    stream = AgentRunner(provider, ToolRegistry([DirectReturnTool()]), persistence).stream(
        AgentRunRequest(model="fixture", message="report"),
        run_context(),
    )

    events = [event async for event in stream]

    assert stream.result().content == "authoritative report"
    assert len(provider.requests) == 1
    assert [event.type for event in events] == [
        AgentEventType.TOOL_CALL,
        AgentEventType.TOOL_RESULT,
        AgentEventType.CONTENT,
        AgentEventType.DONE,
    ]
    assert persistence.saved[0].messages[-1]["content"] == "authoritative report"


@pytest.mark.asyncio
async def test_tool_exceptions_are_visible_to_model_and_event_stream() -> None:
    provider = ScriptedProvider([tool_round(), final_round()])
    persistence = StubPersistence()
    runner = AgentRunner(provider, ToolRegistry([FailingTool()]), persistence)

    stream = runner.stream(
        AgentRunRequest(
            model="fixture",
            message="start",
        ),
        run_context(),
    )
    events = [event async for event in stream]

    tool_result = next(event for event in events if event.type is AgentEventType.TOOL_RESULT)
    assert tool_result.is_error
    assert "tool 'echo' failed (reference " in tool_result.tool_result
    second_request_tool = provider.requests[1].messages[-1]
    assert "tool 'echo' failed (reference " in str(second_request_tool.content)
    assert "fixture failure" not in str(second_request_tool.content)
    saved_tool = next(
        message for message in persistence.saved[-1].messages if message["role"] == "tool"
    )
    assert saved_tool["metadata"]["isError"] is True


@pytest.mark.asyncio
async def test_tool_timeout_is_visible_and_does_not_hang_the_turn() -> None:
    provider = ScriptedProvider([tool_round(), final_round()])
    runner = AgentRunner(provider, ToolRegistry([SlowTool()]), StubPersistence())

    stream = runner.stream(
        AgentRunRequest(
            model="fixture",
            message="start",
            tool_timeout=0.01,
        ),
        run_context(),
    )
    events = [event async for event in stream]

    timeout_event = next(event for event in events if event.type is AgentEventType.TOOL_RESULT)
    assert timeout_event.is_error
    assert "timed out" in timeout_event.tool_result


@pytest.mark.asyncio
async def test_delegation_uses_its_independent_timeout_budget() -> None:
    provider = ScriptedProvider([delegation_round(), final_round()])
    runner = AgentRunner(
        provider,
        ToolRegistry([SlowDelegationTool()]),
        StubPersistence(),
    )
    stream = runner.stream(
        AgentRunRequest(
            model="fixture",
            message="start",
            tool_timeout=0.01,
            delegation_timeout=0.2,
        ),
        run_context(),
    )

    events = [event async for event in stream]

    result = next(event for event in events if event.type is AgentEventType.TOOL_RESULT)
    assert result.is_error is False
    assert result.tool_result == "specialist evidence"


@pytest.mark.asyncio
async def test_all_failed_tool_round_can_fail_closed_without_model_retry() -> None:
    provider = ScriptedProvider([tool_round()])
    persistence = StubPersistence()
    stream = AgentRunner(provider, ToolRegistry([FailingTool()]), persistence).stream(
        AgentRunRequest(
            model="fixture",
            message="# Match prediction",
            max_failed_tool_rounds=1,
        ),
        run_context(),
    )

    _ = [event async for event in stream]

    assert len(provider.requests) == 1
    assert stream.result().metadata["evidenceGate"] == "all_tools_failed"
    assert "不会生成推测性结论" in str(stream.result().content)
    assert persistence.saved[0].title == "Match prediction"
    assert persistence.saved[0].messages[-1]["timestamp"] != "1970-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_football_scope_guard_clarifies_before_provider_or_tools() -> None:
    provider = ScriptedProvider([])
    persistence = StubPersistence()
    stream = AgentRunner(provider, ToolRegistry(), persistence).stream(
        AgentRunRequest(
            model="fixture",
            message="天狼星 vs 布洛马波卡纳",
            scope_guard="football",
        ),
        run_context(),
    )

    _ = [event async for event in stream]

    assert provider.requests == []
    assert "赛事名称" in str(stream.result().content)
    assert "比赛日期" in str(stream.result().content)
    assert stream.result().metadata == {"scopeGate": "football"}
    assert persistence.saved[0].title == "天狼星 vs 布洛马波卡纳"


@pytest.mark.asyncio
async def test_locked_go_session_can_continue_and_preserve_raw_thinking(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "go792" / "fastclaw-go.db"
    source = tmp_path / "go.db"
    source.write_bytes(fixture.read_bytes())
    target_url = f"sqlite+aiosqlite:///{tmp_path / 'python.db'}"
    await import_go_database(source=source, target_url=target_url)
    database = Database(target_url)
    provider = ScriptedProvider([final_round()])
    runner = AgentRunner(provider, ToolRegistry(), DatabaseSessionPersistence(database))
    context = ExecutionContext(
        user_id="u_go_fixture",
        agent_id="agt_go_fixture",
        session_id="web_go_fixture",
        root_execution_id="run-go-fixture",
        call_path=("agt_go_fixture",),
    )
    try:
        response = await runner.chat(
            AgentRunRequest(model="fixture", message="continue"),
            context,
        )
    finally:
        await database.close()

    assert response.content == "finished"
    imported_assistant = provider.requests[0].messages[1]
    assert imported_assistant.raw_assistant is not None
    raw_content = cast(list[dict[str, Any]], imported_assistant.raw_assistant["content"])
    assert raw_content[0]["signature"] == "fixture-signature"

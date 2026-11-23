from dataclasses import replace
from typing import Any

import pytest
from test_agent_runner import EchoTool, ScriptedProvider, StubPersistence, final_round, run_context

from fastclaw.agent import AgentEventType, AgentRunner, AgentRunRequest
from fastclaw.execution import ExecutionContext
from fastclaw.providers import ProviderEvent, ProviderEventType
from fastclaw.tools import ToolRegistry, ToolResult


class ForbiddenEcho(EchoTool):
    calls = 0

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        self.calls += 1
        raise AssertionError("side effect must not run while clarification is requested")


def clarification_round(arguments: str, mixed: bool = False) -> tuple[ProviderEvent, ...]:
    calls = []
    if mixed:
        calls.append(
            ProviderEvent(
                type=ProviderEventType.TOOL_CALL_DELTA,
                tool_index=0,
                tool_name="echo",
                tool_arguments='{"text":"side effect"}',
            )
        )
    calls.append(
        ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=len(calls),
            tool_name="request_clarification",
            tool_arguments=arguments,
        )
    )
    return (*calls, ProviderEvent(type=ProviderEventType.DONE, finish_reason="tool_calls"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    ['{"question":"Which reporting period?"}', "{}", '{"question":" "}', "[]", "{broken"],
)
async def test_clarification_pauses_before_sibling_tools(arguments: str) -> None:
    provider = ScriptedProvider([clarification_round(arguments, mixed=True)])
    persistence = StubPersistence()
    tool = ForbiddenEcho()
    runner = AgentRunner(provider, ToolRegistry([tool]), persistence)
    stream = runner.stream(AgentRunRequest(model="fixture", message="Analyze this"), run_context())
    events = [event async for event in stream]
    assert stream.result().metadata["status"] == "awaiting_clarification"
    assert tool.calls == 0
    assert len(provider.requests) == 1
    assert len(persistence.saved) == 1
    results = [event for event in events if event.type == AgentEventType.TOOL_RESULT]
    assert len(results) == 2
    assert all(event.tool_metadata["executed"] is False for event in results)
    assert len([m for m in persistence.saved[0].messages if m["role"] == "tool"]) == 2


@pytest.mark.asyncio
async def test_reply_resumes_with_persisted_question_and_original_task() -> None:
    provider = ScriptedProvider(
        [
            clarification_round('{"question":"Which reporting period?"}'),
            final_round(),
        ]
    )
    persistence = StubPersistence()
    runner = AgentRunner(provider, ToolRegistry(), persistence)
    first = await runner.chat(
        AgentRunRequest(model="fixture", message="Analyze Nike"), run_context()
    )
    assert first.metadata["status"] == "awaiting_clarification"
    persistence.stored = persistence.saved[-1]
    # A new runner models a reload; no in-memory clarification state is required.
    runner = AgentRunner(provider, ToolRegistry(), persistence)
    final = await runner.chat(AgentRunRequest(model="fixture", message="FY2025"), run_context())
    assert final.content == "finished"
    assert "status" not in final.metadata
    contents = [m.content for m in provider.requests[-1].messages]
    assert "Analyze Nike" in contents and "Which reporting period?" in contents
    assert contents[-1] == "FY2025"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "child,enabled,expected", [(False, True, True), (True, True, False), (False, False, False)]
)
async def test_control_tool_is_root_only(child: bool, enabled: bool, expected: bool) -> None:
    provider = ScriptedProvider([final_round()])
    context = run_context()
    if child:
        context = replace(context, call_path=("parent", "child"))
    runner = AgentRunner(provider, ToolRegistry(), StubPersistence())
    await runner.chat(
        AgentRunRequest(
            model="fixture",
            message="hello",
            clarification_enabled=enabled,
            allowed_tools=frozenset(),
        ),
        context,
    )
    names = [tool.function.name for tool in provider.requests[0].tools]
    assert ("request_clarification" in names) is expected

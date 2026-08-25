from __future__ import annotations

from typing import cast

from fastclaw.agent.context_compaction import (
    ContextCompactionSettings,
    ContextPlanner,
    ensure_context_message_ids,
)
from fastclaw.providers import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    FunctionCall,
    MessageRole,
    ProviderStream,
    ToolCall,
)
from fastclaw.storage import SessionContextSnapshotRecord


class SummaryProvider:
    name = "fixture"

    def __init__(self) -> None:
        self.chat_calls = 0

    async def start(self, client: object) -> None:
        del client

    async def stop(self) -> None:
        return None

    async def ready(self) -> bool:
        return True

    async def chat(self, request: ChatRequest) -> ChatResponse:
        del request
        self.chat_calls += 1
        return ChatResponse(content="Earlier requests were completed; continue the current goal.")

    def stream(self, request: ChatRequest) -> ProviderStream:
        del request
        raise AssertionError("stream is not used by the context planner")


class SnapshotPersistence:
    def __init__(self) -> None:
        self.snapshots: list[SessionContextSnapshotRecord] = []

    async def latest_context_snapshot(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionContextSnapshotRecord | None:
        del user_id, agent_id, session_id
        return self.snapshots[-1] if self.snapshots else None

    async def save_context_snapshot(self, snapshot: SessionContextSnapshotRecord) -> None:
        self.snapshots.append(snapshot)


def long_history() -> list[ChatMessage]:
    messages = [ChatMessage(role=MessageRole.SYSTEM, content="system contract")]
    for index in range(10):
        messages.extend(
            (
                ChatMessage(role=MessageRole.USER, content=f"request {index} " + "x" * 800),
                ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=f"working {index}",
                    thinking="private " + "y" * 1000,
                    raw_assistant={"role": "assistant", "content": f"working {index}"},
                ),
            )
        )
    return messages


async def test_shadow_compaction_preserves_audit_view_and_records_candidate() -> None:
    persistence = SnapshotPersistence()
    history = long_history()
    planner = ContextPlanner(SummaryProvider(), persistence)

    plan = await planner.prepare(
        history,
        user_id="u1",
        agent_id="a1",
        session_id="s1",
        model="fixture",
        settings=ContextCompactionSettings(
            mode="shadow", triggerTokens=1, triggerBytes=1, recentUserTurns=2
        ),
    )

    assert not plan.compacted
    assert len(plan.messages) == len(history)
    assert plan.snapshot is not None
    assert plan.snapshot.status == "ready"
    assert plan.snapshot.metrics["afterBytes"] < plan.snapshot.metrics["beforeBytes"]
    assert all(
        message.metadata.get("contextMessageId")
        for message in plan.messages
        if message.role is not MessageRole.SYSTEM
    )


async def test_active_compaction_keeps_current_tool_transaction_and_football_state() -> None:
    persistence = SnapshotPersistence()
    history = long_history()
    history.extend(
        (
            ChatMessage(
                role=MessageRole.ASSISTANT,
                content="checking fixture",
                tool_calls=(
                    ToolCall(
                        id="call-1",
                        function=FunctionCall(name="spawn_subagent", arguments="{}"),
                    ),
                ),
            ),
            ChatMessage(
                role=MessageRole.TOOL,
                name="spawn_subagent",
                tool_call_id="call-1",
                content=(
                    '{"status":"confirmed","confirmed":[{"fixture_key":'
                    '"spanishlaliga|2026-2027|2026-08-23|getafe|racingsantander",'
                    '"status":"confirmed","fixture":{"home":"Getafe",'
                    '"away":"Racing de Santander"}}]}'
                ),
            ),
        )
    )
    planner = ContextPlanner(SummaryProvider(), persistence)

    plan = await planner.prepare(
        history,
        user_id="u1",
        agent_id="football",
        session_id="s1",
        model="fixture",
        scope_guard="football",
        settings=ContextCompactionSettings(
            mode="active", triggerTokens=1, triggerBytes=1, recentUserTurns=2
        ),
    )

    assert plan.compacted
    assert plan.snapshot is not None
    fixtures = cast(dict[str, object], plan.snapshot.checkpoint["fixtures"])
    key = "spanishlaliga|2026-2027|2026-08-23|getafe|racingsantander"
    assert cast(dict[str, object], fixtures[key])["status"] == "confirmed"
    assert any(
        message.role is MessageRole.TOOL and message.tool_call_id == "call-1"
        for message in plan.messages
    )


def test_context_ids_do_not_modify_provider_raw_payload() -> None:
    raw = {"role": "assistant", "content": "signed", "thinking_signature": "sig"}
    messages = ensure_context_message_ids(
        [ChatMessage(role=MessageRole.ASSISTANT, content="signed", raw_assistant=raw)],
        session_id="s1",
    )

    assert messages[0].raw_assistant == raw
    assert messages[0].metadata["contextMessageId"].startswith("ctx_")


async def test_active_snapshot_is_reused_without_resummarizing() -> None:
    persistence = SnapshotPersistence()
    provider = SummaryProvider()
    planner = ContextPlanner(provider, persistence)
    settings = ContextCompactionSettings(
        mode="active", triggerTokens=5_000, triggerBytes=10_000, recentUserTurns=2
    )
    history = long_history()

    first = await planner.prepare(
        history,
        user_id="u1",
        agent_id="a1",
        session_id="s1",
        model="fixture",
        settings=settings,
    )
    calls = provider.chat_calls
    continued = [*history, ChatMessage(role=MessageRole.USER, content="continue")]
    second = await planner.prepare(
        continued,
        user_id="u1",
        agent_id="a1",
        session_id="s1",
        model="fixture",
        settings=settings,
    )

    assert first.compacted and second.compacted
    assert provider.chat_calls == calls
    assert second.snapshot is first.snapshot

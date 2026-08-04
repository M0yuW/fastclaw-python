from fastclaw.agent import normalize_messages
from fastclaw.providers import ChatMessage, FunctionCall, MessageRole, ToolCall


def call(call_id: str, name: str = "echo") -> ToolCall:
    return ToolCall(id=call_id, function=FunctionCall(name=name))


def test_normalizer_repairs_missing_ids_and_results_and_drops_orphans() -> None:
    messages = (
        ChatMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=(call("dup"), call("dup", "second"), call("")),
        ),
        ChatMessage(role=MessageRole.TOOL, tool_call_id="orphan", content="ignore"),
        ChatMessage(role=MessageRole.TOOL, tool_call_id="dup", content="first result"),
        ChatMessage(role=MessageRole.TOOL, tool_call_id="dup", content="duplicate result"),
        ChatMessage(role=MessageRole.USER, content="continue"),
        ChatMessage(role=MessageRole.TOOL, tool_call_id="late", content="late result"),
    )

    normalized = normalize_messages(messages)

    assistant = normalized[0]
    assert [item.id for item in assistant.tool_calls] == ["dup", "tool-0-1", "tool-0-2"]
    tool_messages = [item for item in normalized if item.role is MessageRole.TOOL]
    assert [item.tool_call_id for item in tool_messages] == ["dup", "tool-0-1", "tool-0-2"]
    assert tool_messages[0].content == "first result"
    assert "missing tool result" in str(tool_messages[1].content)
    assert "missing tool result" in str(tool_messages[2].content)
    assert normalized[-1].role is MessageRole.USER


def test_go_session_message_fields_are_accepted_without_loss() -> None:
    message = ChatMessage.model_validate(
        {
            "role": "user",
            "content": "",
            "content_parts": [{"type": "text", "text": "fixture"}],
            "timestamp": 1_754_265_600_000,
            "metadata": {"sandbox": True},
            "origin": "goal_context",
            "provider": "openai",
            "model": "fixture",
        }
    )

    assert message.content_parts[0].text == "fixture"
    assert message.timestamp == 1_754_265_600_000
    assert message.metadata == {"sandbox": True}
    assert message.origin == "goal_context"

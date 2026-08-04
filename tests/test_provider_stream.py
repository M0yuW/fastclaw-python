from collections.abc import AsyncIterator
from typing import Any, cast

import pytest

from fastclaw.providers import (
    ChatMessage,
    ProviderEvent,
    ProviderEventType,
    ProviderStream,
    ProviderStreamError,
    Usage,
)


def test_chat_message_accepts_go_database_aliases_and_rfc3339() -> None:
    message = ChatMessage.model_validate(
        {
            "role": "assistant",
            "content": "",
            "timestamp": "2026-01-02T03:04:05.123Z",
            "rawAssistant": {"role": "assistant", "content": "cached"},
        }
    )

    assert message.timestamp.isoformat() == "2026-01-02T03:04:05.123000+00:00"
    assert message.raw_assistant == {"role": "assistant", "content": "cached"}
    dumped = message.model_dump(by_alias=True, mode="json")
    assert dumped["_raw"] == {"role": "assistant", "content": "cached"}
    assert "rawAssistant" not in dumped


def test_chat_message_interprets_numeric_timestamp_as_unix_milliseconds() -> None:
    message = ChatMessage(role="user", content="hello", timestamp=1_000)

    assert message.timestamp.isoformat() == "1970-01-01T00:00:01+00:00"


async def test_stream_accumulates_content_thinking_tools_and_usage() -> None:
    async def events() -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type=ProviderEventType.THINKING_DELTA, content="think ")
        yield ProviderEvent(type=ProviderEventType.THINKING_DELTA, content="carefully")
        yield ProviderEvent(type=ProviderEventType.THINKING_SIGNATURE_DELTA, content="signature")
        yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="Hello")
        yield ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_call_id="call-1",
            tool_name="lookup",
            tool_arguments='{"q":',
        )
        yield ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_arguments='"FastClaw"}',
        )
        yield ProviderEvent(
            type=ProviderEventType.DONE,
            finish_reason="tool_calls",
            usage=Usage(prompt_tokens=10, completion_tokens=5, cache_read_tokens=4),
        )

    stream = ProviderStream(events())
    received = [event async for event in stream]
    response = stream.result()

    assert received[-1].type is ProviderEventType.DONE
    assert response.content == "Hello"
    assert response.thinking == "think carefully"
    assert response.thinking_signature == "signature"
    assert response.tool_calls[0].id == "call-1"
    assert response.tool_calls[0].function.name == "lookup"
    assert response.tool_calls[0].function.arguments == '{"q":"FastClaw"}'
    assert response.usage.total_tokens == 15
    assert response.usage.cache_read_tokens == 4
    assert response.raw_assistant == {
        "role": "assistant",
        "content": "Hello",
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"q":"FastClaw"}'},
            }
        ],
        "thinking": "think carefully",
        "thinking_signature": "signature",
    }


async def test_stream_rejects_early_eof_and_partial_results() -> None:
    async def events() -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="partial")

    stream = ProviderStream(events())
    with pytest.raises(ProviderStreamError, match="terminal event"):
        async for _ in stream:
            pass

    with pytest.raises(ProviderStreamError, match="terminal event"):
        stream.result()


async def test_stream_close_marks_result_incomplete() -> None:
    closed = False

    async def events() -> AsyncIterator[ProviderEvent]:
        nonlocal closed
        try:
            yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content="partial")
            yield ProviderEvent(type=ProviderEventType.DONE)
        finally:
            closed = True

    stream = ProviderStream(events())
    await anext(stream)
    await stream.aclose()

    assert closed
    with pytest.raises(ProviderStreamError, match="closed before completion"):
        stream.result()


async def test_local_raw_normalizes_empty_and_duplicate_tool_call_ids() -> None:
    async def events() -> AsyncIterator[ProviderEvent]:
        yield ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=0,
            tool_name="first",
        )
        yield ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=1,
            tool_call_id="same",
            tool_name="second",
        )
        yield ProviderEvent(
            type=ProviderEventType.TOOL_CALL_DELTA,
            tool_index=2,
            tool_call_id="same",
            tool_name="third",
        )
        yield ProviderEvent(type=ProviderEventType.DONE)

    stream = ProviderStream(events())
    async for _ in stream:
        pass
    response = stream.result()

    assert [call.id for call in response.tool_calls] == ["tool-call-0", "same", "tool-call-2"]
    assert response.raw_assistant is not None
    raw_calls = cast(list[dict[str, Any]], response.raw_assistant["tool_calls"])
    assert [call["id"] for call in raw_calls] == [
        "tool-call-0",
        "same",
        "tool-call-2",
    ]


@pytest.mark.parametrize(
    ("raw_ids", "structured_ids", "match"),
    [
        (("",), ("",), "empty"),
        (("duplicate", "duplicate"), ("duplicate", "duplicate"), "duplicate"),
        (("raw",), ("structured",), "do not match"),
    ],
)
async def test_authoritative_raw_rejects_invalid_tool_call_ids(
    raw_ids: tuple[str, ...], structured_ids: tuple[str, ...], match: str
) -> None:
    async def events() -> AsyncIterator[ProviderEvent]:
        for index, call_id in enumerate(structured_ids):
            yield ProviderEvent(
                type=ProviderEventType.TOOL_CALL_DELTA,
                tool_index=index,
                tool_call_id=call_id,
                tool_name="lookup",
            )
        yield ProviderEvent(
            type=ProviderEventType.DONE,
            raw_assistant={
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": call_id, "name": "lookup", "input": {}}
                    for call_id in raw_ids
                ],
            },
        )

    stream = ProviderStream(events())
    with pytest.raises(ProviderStreamError, match=match):
        async for _ in stream:
            pass

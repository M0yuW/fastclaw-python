from collections.abc import AsyncIterator

import pytest

from fastclaw.providers import (
    ProviderEvent,
    ProviderEventType,
    ProviderStream,
    ProviderStreamError,
    Usage,
)


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

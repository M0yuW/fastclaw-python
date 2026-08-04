import json

import httpx

from fastclaw.providers import AnthropicProvider, ChatMessage, ChatRequest, MessageRole


def anthropic_sse(*payloads: dict[str, object]) -> bytes:
    return "".join(
        f"event: {payload['type']}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
        for payload in payloads
    ).encode()


async def test_anthropic_stream_preserves_blocks_signature_tools_and_usage() -> None:
    requests: list[dict[str, object]] = []
    raw_assistant = {
        "role": "assistant",
        "content": [{"type": "text", "text": "cache-safe prefix"}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=anthropic_sse(
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 10,
                            "cache_read_input_tokens": 6,
                            "cache_creation_input_tokens": 2,
                        }
                    },
                },
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "reason"},
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "signed"},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "text", "text": ""},
                },
                {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "Hello"},
                },
                {"type": "content_block_stop", "index": 1},
                {
                    "type": "content_block_start",
                    "index": 2,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "lookup",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 2,
                    "delta": {"type": "input_json_delta", "partial_json": '{"q":"x"}'},
                },
                {"type": "content_block_stop", "index": 2},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use"},
                    "usage": {"output_tokens": 5},
                },
                {"type": "message_stop"},
            ),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = AnthropicProvider(name="anthropic", api_key="secret")
    await provider.start(client)
    try:
        response = await provider.chat(
            ChatRequest(
                model="anthropic/claude-test",
                messages=(
                    ChatMessage(role=MessageRole.SYSTEM, content="system rules"),
                    ChatMessage(
                        role=MessageRole.ASSISTANT,
                        content="ignored",
                        raw_assistant=raw_assistant,
                    ),
                    ChatMessage(role=MessageRole.USER, content="continue"),
                ),
            )
        )
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert requests[0]["model"] == "claude-test"
    assert requests[0]["system"] == "system rules"
    assert requests[0]["messages"][0] == raw_assistant  # type: ignore[index]
    assert response.content == "Hello"
    assert response.thinking == "reason"
    assert response.thinking_signature == "signed"
    assert response.tool_calls[0].id == "tool-1"
    assert response.tool_calls[0].function.arguments == '{"q":"x"}'
    assert response.usage.prompt_tokens == 10
    assert response.usage.completion_tokens == 5
    assert response.usage.cache_read_tokens == 6
    assert response.usage.cache_write_tokens == 2
    assert response.raw_assistant == {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "reason", "signature": "signed"},
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "id": "tool-1", "name": "lookup", "input": {"q": "x"}},
        ],
    }

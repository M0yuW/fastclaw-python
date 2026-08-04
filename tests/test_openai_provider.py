import json
from collections.abc import AsyncIterator

import httpx
import pytest

from fastclaw.providers import (
    ChatMessage,
    ChatRequest,
    MessageRole,
    OpenAIProvider,
    ProviderHTTPError,
    ProviderNotStartedError,
    ProviderStreamError,
)


def sse(*payloads: object) -> bytes:
    frames = [
        "data: [DONE]\n\n"
        if payload == "[DONE]"
        else f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
        for payload in payloads
    ]
    return "".join(frames).encode()


async def test_openai_chat_uses_one_stream_and_accumulates_response() -> None:
    requests: list[dict[str, object]] = []
    raw_assistant = {
        "role": "assistant",
        "content": "cached prefix",
        "vendor_extension": {"preserve": True},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {"choices": [{"delta": {"content": "Hel"}, "finish_reason": None}]},
                {
                    "choices": [
                        {
                            "delta": {
                                "content": "lo",
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call-1",
                                        "function": {
                                            "name": "lookup",
                                            "arguments": '{"q":',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": None,
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [{"index": 0, "function": {"arguments": '"x"}'}}]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 12,
                        "completion_tokens": 4,
                        "total_tokens": 16,
                        "prompt_tokens_details": {"cached_tokens": 8},
                    },
                },
                "[DONE]",
            ),
        )

    provider = OpenAIProvider(name="deepseek", api_key="secret", api_base="https://llm/v1")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.start(client)
    try:
        response = await provider.chat(
            ChatRequest(
                model="deepseek/deepseek-v4-pro",
                messages=(
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
        await provider.stop()
        await client.aclose()

    assert len(requests) == 1
    assert requests[0]["model"] == "deepseek-v4-pro"
    assert requests[0]["messages"][0] == raw_assistant  # type: ignore[index]
    assert response.content == "Hello"
    assert response.tool_calls[0].function.arguments == '{"q":"x"}'
    assert response.finish_reason == "tool_calls"
    assert response.usage.cache_read_tokens == 8


async def test_openai_rejects_use_before_start_and_http_errors() -> None:
    provider = OpenAIProvider(name="openai", api_key="secret")
    request = ChatRequest(
        model="gpt-test", messages=(ChatMessage(role=MessageRole.USER, content="hello"),)
    )

    with pytest.raises(ProviderNotStartedError):
        await provider.chat(request)

    def handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await provider.start(client)
    try:
        with pytest.raises(ProviderHTTPError) as error:
            await provider.chat(request)
    finally:
        await client.aclose()

    assert error.value.status_code == 429
    assert error.value.retryable
    assert "rate limited" in error.value.body


async def test_openai_detects_early_eof() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse({"choices": [{"delta": {"content": "partial"}}]}),
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(name="openai", api_key="secret")
    await provider.start(client)
    stream = provider.stream(
        ChatRequest(
            model="gpt-test", messages=(ChatMessage(role=MessageRole.USER, content="hello"),)
        )
    )
    try:
        with pytest.raises(ProviderStreamError, match=r"before \[DONE\]"):
            async for _ in stream:
                pass
    finally:
        await client.aclose()


class TrackingStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.iter_bytes()

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        yield self.content

    async def aclose(self) -> None:
        self.closed = True


async def test_closing_openai_stream_closes_http_response() -> None:
    body = TrackingStream(
        sse({"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]})
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=body, headers={"content-type": "text/event-stream"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAIProvider(name="openai", api_key="secret")
    await provider.start(client)
    stream = provider.stream(
        ChatRequest(
            model="gpt-test", messages=(ChatMessage(role=MessageRole.USER, content="hello"),)
        )
    )
    await anext(stream)
    await stream.aclose()
    await client.aclose()

    assert body.closed

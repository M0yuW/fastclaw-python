"""OpenAI-compatible streaming provider."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from fastclaw.providers._http import (
    iter_sse_data,
    raise_for_provider_status,
    strip_provider_prefix,
)
from fastclaw.providers.errors import ProviderNotStartedError, ProviderStreamError
from fastclaw.providers.models import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    MessageRole,
    ProviderEvent,
    ProviderEventType,
    Usage,
)
from fastclaw.providers.stream import ProviderStream


class OpenAIProvider:
    """LLM provider for OpenAI-compatible chat completion APIs."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        api_base: str = "https://api.openai.com/v1",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._api_key = api_key
        self._endpoint = f"{api_base.rstrip('/')}/chat/completions"
        self._headers = dict(headers or {})
        self._client: httpx.AsyncClient | None = None

    @property
    def name(self) -> str:
        return self._name

    async def start(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def stop(self) -> None:
        self._client = None

    async def ready(self) -> bool:
        return self._client is not None and not self._client.is_closed

    async def chat(self, request: ChatRequest) -> ChatResponse:
        stream = self.stream(request)
        async for _ in stream:
            pass
        return stream.result()

    def stream(self, request: ChatRequest) -> ProviderStream:
        return ProviderStream(self._stream_events(request))

    async def _stream_events(self, request: ChatRequest) -> AsyncIterator[ProviderEvent]:
        client = self._require_client()
        headers = {
            "Accept": "text/event-stream",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            **self._headers,
        }
        finish_reason: str | None = None
        usage: Usage | None = None
        saw_done = False

        async with client.stream(
            "POST", self._endpoint, headers=headers, json=self._payload(request)
        ) as response:
            await raise_for_provider_status(response, self.name)
            async for data in iter_sse_data(response):
                if data == "[DONE]":
                    saw_done = True
                    yield ProviderEvent(
                        type=ProviderEventType.DONE,
                        finish_reason=finish_reason,
                        usage=usage,
                    )
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderStreamError("OpenAI stream returned malformed JSON") from exc
                if not isinstance(payload, dict):
                    raise ProviderStreamError("OpenAI stream payload must be an object")

                parsed_usage = self._parse_usage(payload.get("usage"))
                if parsed_usage is not None:
                    usage = parsed_usage
                choices = payload.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ProviderStreamError("OpenAI stream choice must be an object")
                if choice.get("finish_reason") is not None:
                    finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                if not isinstance(delta, dict):
                    raise ProviderStreamError("OpenAI stream delta must be an object")

                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content=content)
                thinking_field = "reasoning_content" if "reasoning_content" in delta else "thinking"
                thinking = delta.get(thinking_field)
                if isinstance(thinking, str) and thinking:
                    yield ProviderEvent(
                        type=ProviderEventType.THINKING_DELTA,
                        content=thinking,
                        raw_assistant_delta={thinking_field: thinking},
                    )
                signature = delta.get("thinking_signature")
                if isinstance(signature, str) and signature:
                    yield ProviderEvent(
                        type=ProviderEventType.THINKING_SIGNATURE_DELTA,
                        content=signature,
                    )
                for tool_delta in delta.get("tool_calls") or []:
                    if not isinstance(tool_delta, dict):
                        raise ProviderStreamError("OpenAI tool call delta must be an object")
                    function = tool_delta.get("function") or {}
                    if not isinstance(function, dict):
                        raise ProviderStreamError("OpenAI function delta must be an object")
                    yield ProviderEvent(
                        type=ProviderEventType.TOOL_CALL_DELTA,
                        tool_index=int(tool_delta.get("index", 0)),
                        tool_call_id=str(tool_delta.get("id") or ""),
                        tool_name=str(function.get("name") or ""),
                        tool_arguments=str(function.get("arguments") or ""),
                    )

        if not saw_done:
            raise ProviderStreamError("OpenAI stream ended before [DONE]")

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            raise ProviderNotStartedError(f"provider {self.name!r} is not started")
        return self._client

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": strip_provider_prefix(request.model),
            "messages": [self._message_payload(message) for message in request.messages],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if request.tools:
            payload["tools"] = [tool.model_dump(exclude_none=True) for tool in request.tools]
        return payload

    @staticmethod
    def _message_payload(message: ChatMessage) -> dict[str, Any]:
        if message.role is MessageRole.ASSISTANT and message.raw_assistant is not None:
            return dict(message.raw_assistant)
        payload: dict[str, Any] = {"role": message.role.value}
        if isinstance(message.content, tuple):
            payload["content"] = [part.model_dump(exclude_none=True) for part in message.content]
        elif message.content is not None:
            payload["content"] = message.content
        if message.tool_calls:
            payload["tool_calls"] = [
                call.model_dump(exclude_none=True) for call in message.tool_calls
            ]
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            payload["name"] = message.name
        return payload

    @staticmethod
    def _parse_usage(value: object) -> Usage | None:
        if not isinstance(value, dict):
            return None
        prompt_details = value.get("prompt_tokens_details")
        cached = prompt_details.get("cached_tokens", 0) if isinstance(prompt_details, dict) else 0
        return Usage(
            prompt_tokens=int(value.get("prompt_tokens", 0) or 0),
            completion_tokens=int(value.get("completion_tokens", 0) or 0),
            cache_read_tokens=int(cached or 0),
            cache_write_tokens=int(value.get("cache_creation_input_tokens", 0) or 0),
            total_tokens=int(value.get("total_tokens", 0) or 0),
        )

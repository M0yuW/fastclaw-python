"""Anthropic Messages streaming provider."""

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
    ContentPart,
    MessageRole,
    ProviderEvent,
    ProviderEventType,
    Usage,
)
from fastclaw.providers.stream import ProviderStream


class AnthropicProvider:
    """LLM provider for the Anthropic Messages API."""

    def __init__(
        self,
        *,
        name: str,
        api_key: str,
        api_base: str = "https://api.anthropic.com",
        anthropic_version: str = "2023-06-01",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._name = name
        self._api_key = api_key
        base = api_base.rstrip("/")
        self._endpoint = f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
        self._anthropic_version = anthropic_version
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
            "anthropic-version": self._anthropic_version,
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            **self._headers,
        }
        usage = Usage()
        finish_reason: str | None = None
        saw_done = False
        blocks: dict[int, dict[str, Any]] = {}
        tool_arguments: dict[int, list[str]] = {}

        async with client.stream(
            "POST", self._endpoint, headers=headers, json=self._payload(request)
        ) as response:
            await raise_for_provider_status(response, self.name)
            async for data in iter_sse_data(response):
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ProviderStreamError("Anthropic stream returned malformed JSON") from exc
                if not isinstance(payload, dict):
                    raise ProviderStreamError("Anthropic stream payload must be an object")
                event_type = payload.get("type")

                if event_type == "message_start":
                    message = payload.get("message") or {}
                    if isinstance(message, dict):
                        usage = self._merge_usage(usage, message.get("usage"))
                elif event_type == "content_block_start":
                    index = int(payload.get("index", 0))
                    block = payload.get("content_block") or {}
                    if not isinstance(block, dict):
                        raise ProviderStreamError("Anthropic content block must be an object")
                    blocks[index] = dict(block)
                    block_type = block.get("type")
                    if block_type == "text" and block.get("text"):
                        yield ProviderEvent(
                            type=ProviderEventType.CONTENT_DELTA,
                            content=str(block["text"]),
                        )
                    elif block_type == "thinking" and block.get("thinking"):
                        yield ProviderEvent(
                            type=ProviderEventType.THINKING_DELTA,
                            content=str(block["thinking"]),
                        )
                    elif block_type == "tool_use":
                        tool_arguments[index] = []
                        initial = block.get("input")
                        initial_json = "" if initial in (None, {}) else json.dumps(initial)
                        if initial_json:
                            tool_arguments[index].append(initial_json)
                        yield ProviderEvent(
                            type=ProviderEventType.TOOL_CALL_DELTA,
                            tool_index=index,
                            tool_call_id=str(block.get("id") or ""),
                            tool_name=str(block.get("name") or ""),
                            tool_arguments=initial_json,
                        )
                elif event_type == "content_block_delta":
                    index = int(payload.get("index", 0))
                    delta = payload.get("delta") or {}
                    if not isinstance(delta, dict):
                        raise ProviderStreamError("Anthropic content delta must be an object")
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        value = str(delta.get("text") or "")
                        self._append_block(blocks, index, "text", value)
                        yield ProviderEvent(type=ProviderEventType.CONTENT_DELTA, content=value)
                    elif delta_type == "thinking_delta":
                        value = str(delta.get("thinking") or "")
                        self._append_block(blocks, index, "thinking", value)
                        yield ProviderEvent(type=ProviderEventType.THINKING_DELTA, content=value)
                    elif delta_type == "signature_delta":
                        value = str(delta.get("signature") or "")
                        self._append_block(blocks, index, "signature", value)
                        yield ProviderEvent(
                            type=ProviderEventType.THINKING_SIGNATURE_DELTA,
                            content=value,
                        )
                    elif delta_type == "input_json_delta":
                        value = str(delta.get("partial_json") or "")
                        tool_arguments.setdefault(index, []).append(value)
                        yield ProviderEvent(
                            type=ProviderEventType.TOOL_CALL_DELTA,
                            tool_index=index,
                            tool_arguments=value,
                        )
                elif event_type == "content_block_stop":
                    index = int(payload.get("index", 0))
                    if index in tool_arguments and index in blocks:
                        raw_arguments = "".join(tool_arguments[index]) or "{}"
                        try:
                            blocks[index]["input"] = json.loads(raw_arguments)
                        except json.JSONDecodeError as exc:
                            raise ProviderStreamError(
                                "Anthropic tool input ended with malformed JSON"
                            ) from exc
                elif event_type == "message_delta":
                    delta = payload.get("delta") or {}
                    if isinstance(delta, dict) and delta.get("stop_reason") is not None:
                        finish_reason = str(delta["stop_reason"])
                    usage = self._merge_usage(usage, payload.get("usage"))
                elif event_type == "message_stop":
                    saw_done = True
                    raw = {
                        "role": "assistant",
                        "content": [blocks[index] for index in sorted(blocks)],
                    }
                    yield ProviderEvent(
                        type=ProviderEventType.DONE,
                        finish_reason=finish_reason,
                        usage=usage,
                        raw_assistant=raw,
                    )
                    break
                elif event_type == "error":
                    error = payload.get("error") or {}
                    message = error.get("message") if isinstance(error, dict) else None
                    raise ProviderStreamError(str(message or "Anthropic stream returned an error"))

        if not saw_done:
            raise ProviderStreamError("Anthropic stream ended before message_stop")

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            raise ProviderNotStartedError(f"provider {self.name!r} is not started")
        return self._client

    def _payload(self, request: ChatRequest) -> dict[str, Any]:
        systems: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role is MessageRole.SYSTEM:
                systems.append(self._text_content(message))
            else:
                messages.append(self._message_payload(message))
        payload: dict[str, Any] = {
            "model": strip_provider_prefix(request.model),
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "stream": True,
        }
        if systems:
            payload["system"] = "\n\n".join(systems)
        if request.tools:
            payload["tools"] = [
                {
                    "name": tool.function.name,
                    "description": tool.function.description,
                    "input_schema": tool.function.parameters,
                }
                for tool in request.tools
            ]
        if request.thinking_budget_tokens is not None:
            payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": request.thinking_budget_tokens,
            }
        return payload

    def _message_payload(self, message: ChatMessage) -> dict[str, Any]:
        if message.role is MessageRole.ASSISTANT and message.raw_assistant is not None:
            return dict(message.raw_assistant)
        if message.role is MessageRole.TOOL:
            return {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": message.tool_call_id or "",
                        "content": self._text_content(message),
                    }
                ],
            }
        content: list[dict[str, Any]] = []
        if message.thinking:
            content.append(
                {
                    "type": "thinking",
                    "thinking": message.thinking,
                    "signature": message.thinking_signature or "",
                }
            )
        multipart = message.content_parts or (
            message.content if isinstance(message.content, tuple) else ()
        )
        if multipart:
            content.extend(self._content_part_payload(part) for part in multipart)
        elif message.content:
            content.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            try:
                arguments = json.loads(call.function.arguments)
            except json.JSONDecodeError as exc:
                raise ProviderStreamError("tool arguments must be valid JSON") from exc
            content.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": arguments,
                }
            )
        return {"role": message.role.value, "content": content}

    @staticmethod
    def _content_part_payload(part: ContentPart) -> dict[str, Any]:
        if part.type == "text":
            return {"type": "text", "text": part.text or ""}
        assert part.image_url is not None
        return {
            "type": "image",
            "source": {"type": "url", "url": part.image_url.url},
        }

    @staticmethod
    def _text_content(message: ChatMessage) -> str:
        if isinstance(message.content, str):
            return message.content
        multipart = message.content_parts or (
            message.content if isinstance(message.content, tuple) else ()
        )
        if multipart:
            return "\n".join(part.text or "" for part in multipart if part.type == "text")
        return ""

    @staticmethod
    def _append_block(
        blocks: dict[int, dict[str, Any]], index: int, field: str, value: str
    ) -> None:
        block = blocks.setdefault(index, {"type": "text"})
        block[field] = str(block.get(field) or "") + value

    @staticmethod
    def _merge_usage(current: Usage, value: object) -> Usage:
        if not isinstance(value, dict):
            return current
        prompt = int(value.get("input_tokens", current.prompt_tokens) or 0)
        completion = int(value.get("output_tokens", current.completion_tokens) or 0)
        cache_read = int(value.get("cache_read_input_tokens", current.cache_read_tokens) or 0)
        cache_write = int(value.get("cache_creation_input_tokens", current.cache_write_tokens) or 0)
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            total_tokens=prompt + completion,
        )

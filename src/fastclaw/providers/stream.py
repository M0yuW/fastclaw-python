"""Streaming response accumulation shared by all providers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from fastclaw.providers.errors import ProviderStreamError
from fastclaw.providers.models import (
    ChatResponse,
    FunctionCall,
    ProviderEvent,
    ProviderEventType,
    ToolCall,
    Usage,
)


class ResponseAccumulator:
    """Accumulate provider-neutral deltas into one complete response."""

    def __init__(self) -> None:
        self._content: list[str] = []
        self._thinking: list[str] = []
        self._thinking_signature: list[str] = []
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._finish_reason: str | None = None
        self._usage = Usage()
        self._raw_assistant: dict[str, Any] | None = None
        self._done = False

    @property
    def done(self) -> bool:
        return self._done

    def apply(self, event: ProviderEvent) -> None:
        if self._done:
            raise ProviderStreamError("received an event after the terminal event")
        if event.type is ProviderEventType.CONTENT_DELTA:
            self._content.append(event.content)
        elif event.type is ProviderEventType.THINKING_DELTA:
            self._thinking.append(event.content)
        elif event.type is ProviderEventType.THINKING_SIGNATURE_DELTA:
            self._thinking_signature.append(event.content)
        elif event.type is ProviderEventType.TOOL_CALL_DELTA:
            if event.tool_index is None:
                raise ProviderStreamError("tool call deltas require tool_index")
            call = self._tool_calls.setdefault(
                event.tool_index, {"id": "", "name": "", "arguments": []}
            )
            if event.tool_call_id:
                call["id"] = event.tool_call_id
            if event.tool_name:
                call["name"] += event.tool_name
            if event.tool_arguments:
                call["arguments"].append(event.tool_arguments)
        elif event.type is ProviderEventType.DONE:
            self._finish_reason = event.finish_reason
            if event.usage is not None:
                self._usage = event.usage
            self._raw_assistant = event.raw_assistant
            self._done = True

    def response(self) -> ChatResponse:
        if not self._done:
            raise ProviderStreamError("provider stream has not completed")
        tool_calls = tuple(
            ToolCall(
                id=str(call["id"]),
                function=FunctionCall(name=str(call["name"]), arguments="".join(call["arguments"])),
            )
            for _, call in sorted(self._tool_calls.items())
        )
        content = "".join(self._content)
        thinking = "".join(self._thinking)
        signature = "".join(self._thinking_signature)
        raw = self._raw_assistant or self._build_raw_assistant(
            content=content,
            thinking=thinking,
            signature=signature,
            tool_calls=tool_calls,
        )
        return ChatResponse(
            content=content,
            tool_calls=tool_calls,
            thinking=thinking,
            thinking_signature=signature,
            raw_assistant=raw,
            usage=self._usage,
            finish_reason=self._finish_reason,
        )

    @staticmethod
    def _build_raw_assistant(
        *,
        content: str,
        thinking: str,
        signature: str,
        tool_calls: tuple[ToolCall, ...],
    ) -> dict[str, Any]:
        raw: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            raw["tool_calls"] = [
                call.model_dump(by_alias=True, exclude_none=True) for call in tool_calls
            ]
        if thinking:
            raw["thinking"] = thinking
        if signature:
            raw["thinking_signature"] = signature
        return raw


class ProviderStream(AsyncIterator[ProviderEvent]):
    """A provider event iterator with a synchronized terminal result."""

    def __init__(self, source: AsyncIterator[ProviderEvent]) -> None:
        self._source = source
        self._accumulator = ResponseAccumulator()
        self._result: ChatResponse | None = None
        self._error: BaseException | None = None
        self._closed = False

    def __aiter__(self) -> ProviderStream:
        return self

    async def __anext__(self) -> ProviderEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await anext(self._source)
        except StopAsyncIteration:
            self._closed = True
            if self._result is None and self._error is None:
                self._error = ProviderStreamError("provider stream ended before a terminal event")
                raise self._error from None
            raise
        except BaseException as exc:
            self._closed = True
            self._error = exc
            raise

        try:
            self._accumulator.apply(event)
            if event.type is ProviderEventType.DONE:
                self._result = self._accumulator.response()
        except BaseException as exc:
            self._closed = True
            self._error = exc
            await self._close_source()
            raise
        return event

    def result(self) -> ChatResponse:
        """Return the complete response after the terminal event."""

        if self._error is not None:
            raise self._error
        if self._result is None:
            raise ProviderStreamError("provider stream has not completed")
        return self._result

    async def aclose(self) -> None:
        """Close the upstream HTTP stream without accepting a partial result."""

        if not self._closed:
            self._closed = True
            if self._result is None:
                self._error = ProviderStreamError("provider stream was closed before completion")
            await self._close_source()

    async def _close_source(self) -> None:
        close = getattr(self._source, "aclose", None)
        if close is not None:
            await close()

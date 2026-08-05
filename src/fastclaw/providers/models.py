"""Typed provider requests, responses, and streaming events."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)


def _to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ProviderModel(BaseModel):
    """Base model with Python names and FastClaw-compatible JSON aliases."""

    model_config = ConfigDict(
        alias_generator=_to_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class ImageURL(ProviderModel):
    url: str
    detail: Literal["auto", "low", "high"] = "auto"


class ContentPart(ProviderModel):
    type: Literal["text", "image_url"]
    text: str | None = None
    image_url: ImageURL | None = None

    @model_validator(mode="after")
    def validate_payload(self) -> ContentPart:
        if self.type == "text" and self.text is None:
            raise ValueError("text content parts require text")
        if self.type == "image_url" and self.image_url is None:
            raise ValueError("image_url content parts require image_url")
        return self


class FunctionCall(ProviderModel):
    name: str
    arguments: str = "{}"


class ToolCall(ProviderModel):
    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ToolFunction(ProviderModel):
    name: str
    description: str = ""
    parameters: dict[str, JsonValue] = Field(default_factory=dict)


class ToolDefinition(ProviderModel):
    type: Literal["function"] = "function"
    function: ToolFunction


class Usage(ProviderModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0

    @model_validator(mode="before")
    @classmethod
    def fill_total(cls, value: Any) -> Any:
        if isinstance(value, dict) and not value.get("total_tokens", value.get("totalTokens")):
            updated = dict(value)
            updated["total_tokens"] = int(
                value.get("prompt_tokens", value.get("promptTokens", 0)) or 0
            ) + int(value.get("completion_tokens", value.get("completionTokens", 0)) or 0)
            return updated
        return value


class ChatMessage(ProviderModel):
    role: MessageRole
    content: str | tuple[ContentPart, ...] | None = None
    content_parts: tuple[ContentPart, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    thinking: str | None = None
    thinking_signature: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.fromtimestamp(0, UTC))
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    origin: str = ""
    provider: str = ""
    model: str = ""
    raw_assistant: dict[str, JsonValue] | None = Field(
        default=None,
        validation_alias=AliasChoices("_raw", "rawAssistant"),
        serialization_alias="_raw",
    )

    @field_validator("timestamp", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: object) -> object:
        """Accept Go DB RFC3339 values and provider-wire Unix milliseconds."""

        if isinstance(value, bool):
            raise ValueError("timestamp must be RFC3339 or Unix milliseconds")
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, UTC)
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @field_validator("timestamp")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)


class ChatRequest(ProviderModel):
    messages: tuple[ChatMessage, ...]
    model: str
    tools: tuple[ToolDefinition, ...] = ()
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    thinking_budget_tokens: int | None = Field(default=None, gt=0)


class ChatResponse(ProviderModel):
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    thinking: str = ""
    thinking_signature: str = ""
    raw_assistant: dict[str, JsonValue] | None = Field(default=None, alias="_raw")
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None


class ProviderEventType(StrEnum):
    CONTENT_DELTA = "content_delta"
    THINKING_DELTA = "thinking_delta"
    THINKING_SIGNATURE_DELTA = "thinking_signature_delta"
    TOOL_CALL_DELTA = "tool_call_delta"
    DONE = "done"


class ProviderEvent(ProviderModel):
    type: ProviderEventType
    content: str = ""
    tool_index: int | None = Field(default=None, ge=0)
    tool_call_id: str = ""
    tool_name: str = ""
    tool_arguments: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    raw_assistant_delta: dict[str, JsonValue] = Field(default_factory=dict)
    raw_assistant: dict[str, JsonValue] | None = Field(default=None, alias="_raw")

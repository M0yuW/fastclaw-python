"""Single-agent run requests and stable SSE v2 event models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fastclaw.providers import ChatMessage, ToolCall


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class AgentModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=_camel,
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class AgentRunRequest(AgentModel):
    model: str
    message: str
    allowed_tools: frozenset[str] | None = None
    max_rounds: int = Field(default=8, ge=1, le=64)
    max_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    thinking_budget_tokens: int | None = Field(default=None, gt=0)
    tool_timeout: float = Field(default=30.0, gt=0)
    system_prompt: str = ""


class AgentEventType(StrEnum):
    CONTENT_DELTA = "content_delta"
    CONTENT = "content"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    DONE = "done"


class AgentEvent(AgentModel):
    type: AgentEventType
    turn_id: str
    message_id: str
    round: int = Field(ge=0)
    seq: int = Field(ge=0)
    content: str = ""
    tool_call: ToolCall | None = None
    tool_result: str = ""
    tool_metadata: dict[str, Any] = Field(default_factory=dict)
    is_error: bool = False
    error: str = ""
    message: ChatMessage | None = None


class AgentRunError(RuntimeError):
    pass

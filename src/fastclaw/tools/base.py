"""Tool extension contracts."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    is_error: bool = False
    direct_return: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult: ...

"""Tool registration, policy filtering, and failure isolation."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import anyio

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition
from fastclaw.tools.base import Tool, ToolResult


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        name = tool.definition.function.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def definitions(self, allowed: frozenset[str] | None = None) -> tuple[ToolDefinition, ...]:
        return tuple(
            tool.definition
            for name, tool in self._tools.items()
            if allowed is None or name in allowed
        )

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: ExecutionContext,
        *,
        allowed: frozenset[str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> ToolResult:
        if allowed is not None and name not in allowed:
            return ToolResult(content=f"tool {name!r} is denied by policy", is_error=True)
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(content=f"tool {name!r} is not registered", is_error=True)
        try:
            with anyio.fail_after(timeout_seconds):
                return await tool.execute(arguments, context)
        except TimeoutError:
            return ToolResult(content=f"tool {name!r} timed out", is_error=True)
        except Exception as exc:
            return ToolResult(
                content=f"tool {name!r} failed: {type(exc).__name__}: {exc}",
                is_error=True,
            )

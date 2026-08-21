"""Tool registration, policy filtering, and failure isolation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any
from uuid import uuid4

import anyio

from fastclaw.execution import ExecutionContext
from fastclaw.observability import log_stage
from fastclaw.providers import ToolDefinition
from fastclaw.tools.base import BatchTool, Tool, ToolResult

logger = logging.getLogger(__name__)


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
        started_at = asyncio.get_running_loop().time()
        log_stage(logger, "tool_started", context=context, tool=name)
        try:
            with anyio.fail_after(timeout_seconds):
                result = await tool.execute(arguments, context)
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="failed" if result.is_error else "completed",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.WARNING if result.is_error else logging.INFO,
            )
            return result
        except TimeoutError:
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="timed_out",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.WARNING,
            )
            logger.warning(
                "tool %s timed out after %.1fs (root=%s agent=%s call_path=%s)",
                name,
                timeout_seconds,
                context.root_execution_id,
                context.agent_id,
                " -> ".join(context.call_path),
            )
            return ToolResult(
                content=f"tool {name!r} timed out",
                is_error=True,
                metadata={"errorCode": "timeout", "timeoutSeconds": timeout_seconds},
            )
        except Exception:
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="failed",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.ERROR,
            )
            return self._unexpected_failure(name)

    def supports_batch(
        self, names: tuple[str, ...], *, allowed: frozenset[str] | None = None
    ) -> bool:
        if len(names) < 2 or len(set(names)) != 1:
            return False
        name = names[0]
        if allowed is not None and name not in allowed:
            return False
        return isinstance(self._tools.get(name), BatchTool)

    async def execute_batch(
        self,
        name: str,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
        *,
        allowed: frozenset[str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> tuple[ToolResult, ...]:
        if not self.supports_batch((name,) * len(arguments), allowed=allowed):
            raise ValueError(f"tool {name!r} does not support batch execution")
        tool = self._tools[name]
        assert isinstance(tool, BatchTool)
        started_at = asyncio.get_running_loop().time()
        log_stage(logger, "tool_started", context=context, tool=name)
        try:
            isolated_batch = getattr(tool, "execute_many_isolated", None)
            if callable(isolated_batch):
                # Delegations have their own per-child timeout contract. An
                # outer batch deadline would cancel completed siblings and
                # replace every result with the same batch timeout.
                results = await isolated_batch(
                    arguments,
                    context,
                    timeout_seconds=timeout_seconds,
                )
            else:
                with anyio.fail_after(timeout_seconds):
                    results = await tool.execute_many(arguments, context)
            if len(results) != len(arguments):
                raise RuntimeError("batch tool returned an unexpected result count")
            if any(result.direct_return for result in results):
                raise RuntimeError("batch tools cannot return direct responses")
            has_error = any(result.is_error for result in results)
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="failed" if has_error else "completed",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.WARNING if has_error else logging.INFO,
            )
            return results
        except TimeoutError:
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="timed_out",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.WARNING,
            )
            logger.warning(
                "batch tool %s timed out after %.1fs (root=%s agent=%s call_path=%s calls=%d)",
                name,
                timeout_seconds,
                context.root_execution_id,
                context.agent_id,
                " -> ".join(context.call_path),
                len(arguments),
            )
            return tuple(
                ToolResult(
                    content=f"tool {name!r} timed out",
                    is_error=True,
                    metadata={"errorCode": "timeout", "timeoutSeconds": timeout_seconds},
                )
                for _ in arguments
            )
        except Exception:
            log_stage(
                logger,
                "tool_finished",
                context=context,
                tool=name,
                outcome="failed",
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
                level=logging.ERROR,
            )
            failure = self._unexpected_failure(name)
            return tuple(failure for _ in arguments)

    @staticmethod
    def _unexpected_failure(name: str) -> ToolResult:
        correlation_id = str(uuid4())
        logger.exception("tool %s failed (correlation_id=%s)", name, correlation_id)
        return ToolResult(
            content=f"tool {name!r} failed (reference {correlation_id})",
            is_error=True,
            metadata={"correlationId": correlation_id},
        )

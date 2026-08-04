"""Trusted `spawn_subagent` tool backed by the in-process message bus."""

from __future__ import annotations

from typing import Any

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.bus import InProcessMessageBus
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools import ToolResult


class SpawnSubagentTool:
    def __init__(self, bus: InProcessMessageBus) -> None:
        self._bus = bus
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="spawn_subagent",
                description="Delegate one task to another Agent in the same tenant.",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    "required": ["agent_id", "task"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        result = await self._bus.request(
            context,
            target_agent_id=str(arguments["agent_id"]),
            task=str(arguments["task"]),
        )
        return ToolResult(
            content=result.value,
            metadata={"correlationId": result.correlation_id},
        )

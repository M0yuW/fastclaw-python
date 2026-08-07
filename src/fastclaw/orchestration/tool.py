"""Trusted `spawn_subagent` tool backed by the in-process message bus."""

from __future__ import annotations

from typing import Any

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.bus import DelegationRequest, MessageBus
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools import ToolResult


class SpawnSubagentTool:
    def __init__(self, bus: MessageBus, target_agent_ids: tuple[str, ...] | None = None) -> None:
        self._bus = bus
        self._target_agent_ids = target_agent_ids
        agent_id: dict[str, object] = {"type": "string"}
        if target_agent_ids is not None:
            agent_id["enum"] = list(target_agent_ids)
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="spawn_subagent",
                description="Delegate one task to another Agent in the same tenant.",
                parameters={
                    "type": "object",
                    "properties": {
                        "agent_id": agent_id,
                        "task": {"type": "string"},
                    },
                    "required": ["agent_id", "task"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        agent_id = arguments.get("agent_id")
        task = arguments.get("task")
        if not isinstance(agent_id, str) or not agent_id or not isinstance(task, str):
            return ToolResult(content="invalid delegation arguments", is_error=True)
        if self._target_agent_ids is not None and agent_id not in self._target_agent_ids:
            return ToolResult(content="delegation target is not allowed", is_error=True)
        result = await self._bus.request(
            context,
            target_agent_id=agent_id,
            task=task,
        )
        return ToolResult(
            content=result.value,
            metadata={"correlationId": result.correlation_id},
        )

    async def execute_many(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
    ) -> tuple[ToolResult, ...]:
        requests: list[DelegationRequest] = []
        request_indexes: list[int] = []
        results: list[ToolResult | None] = [None] * len(arguments)
        for index, item in enumerate(arguments):
            agent_id = item.get("agent_id")
            task = item.get("task")
            if not isinstance(agent_id, str) or not agent_id or not isinstance(task, str):
                results[index] = ToolResult(
                    content="invalid delegation arguments",
                    is_error=True,
                )
                continue
            if self._target_agent_ids is not None and agent_id not in self._target_agent_ids:
                results[index] = ToolResult(
                    content="delegation target is not allowed",
                    is_error=True,
                )
                continue
            requests.append(DelegationRequest(agent_id=agent_id, task=task))
            request_indexes.append(index)

        outcomes = await self._bus.batch(context, requests)
        for index, outcome in zip(request_indexes, outcomes, strict=True):
            if outcome.result is not None:
                results[index] = ToolResult(
                    content=outcome.result.value,
                    metadata={"correlationId": outcome.result.correlation_id},
                )
                continue
            assert outcome.error is not None
            results[index] = ToolResult(
                content=f"{outcome.error.code}: {outcome.error.message}",
                is_error=True,
                metadata={
                    "errorCode": outcome.error.code,
                    "correlationId": outcome.error.correlation_id,
                },
            )
        return tuple(result for result in results if result is not None)

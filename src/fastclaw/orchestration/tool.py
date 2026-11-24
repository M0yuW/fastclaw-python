"""Trusted `spawn_subagent` tool backed by the in-process message bus."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.bus import DelegationRequest, MessageBus
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools import ToolResult

logger = logging.getLogger(__name__)


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
                        "delivery": {
                            "type": "object",
                            "required": ["mode"],
                            "description": "Configure before delegation; seal before synthesis.",
                            "properties": {
                                "mode": {"enum": ["summary", "targeted", "broadcast", "seal"]},
                                "targets": {"type": "array", "items": {"type": "string"}},
                                "asOf": {"type": "string", "format": "date-time"},
                            },
                        },
                        "review": {
                            "type": "object",
                            "description": "Quote a claim from resultsForReview; submit alone.",
                            "properties": {
                                **{
                                    key: {"type": "string"}
                                    for key in ("resultId", "claim", "period")
                                },
                                "eventVersion": {"type": "integer"},
                            },
                            "required": ["resultId", "claim", "period", "eventVersion"],
                        },
                        "agent_id": agent_id,
                        "task": {"type": "string"},
                    },
                    "anyOf": [
                        {"required": ["agent_id", "task"]},
                        {"required": ["review"]},
                        {"required": ["delivery"]},
                    ],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        if "delivery" in arguments:
            if (
                set(arguments) != {"delivery"}
                or not isinstance(arguments["delivery"], dict)
                or context.evidence is None
            ):
                return ToolResult(content="submit delivery alone in a team task", is_error=True)
            try:
                return ToolResult(
                    content=context.evidence.configure(context, arguments["delivery"])
                )
            except ValueError as exc:
                return ToolResult(content=str(exc), is_error=True)
        if "review" in arguments:
            return await self._review(arguments, context)
        agent_id = arguments.get("agent_id")
        task = arguments.get("task")
        if not isinstance(agent_id, str) or not agent_id or not isinstance(task, str):
            return ToolResult(content="invalid delegation arguments", is_error=True)
        if self._target_agent_ids is not None and agent_id not in self._target_agent_ids:
            return ToolResult(content="delegation target is not allowed", is_error=True)
        if context.evidence:
            context.evidence.started = True
        result = await self._bus.request(
            context,
            target_agent_id=agent_id,
            task=task,
        )
        self._remember(context, result.correlation_id, agent_id, result.value)
        return ToolResult(
            content=result.value,
            metadata={"correlationId": result.correlation_id},
        )

    @staticmethod
    def _remember(context: ExecutionContext, result_id: str, agent: str, result: str) -> None:
        evidence = context.evidence
        if (
            evidence
            and evidence.matches(context)
            and len(context.call_path) == 1
            and result.strip()
        ):
            evidence.results[result_id] = {"agent": agent, "result": result}

    async def _review(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        evidence = context.evidence
        req = arguments["review"]
        if (
            evidence is None
            or not evidence.matches(context)
            or len(context.call_path) != 1
            or set(arguments) != {"review"}
            or not isinstance(req, dict)
            or set(req) != {"resultId", "period", "claim", "eventVersion"}
        ):
            return ToolResult(content="invalid review request", is_error=True)
        if not all(
            isinstance(req[k], str) and req[k].strip() for k in ("resultId", "period", "claim")
        ):
            return ToolResult(content="invalid claim or period", is_error=True)
        source = evidence.results.get(req["resultId"])
        version = req["eventVersion"]
        if (
            not source
            or req["claim"] not in source["result"]
            or type(version) is not int
            or not 2 <= version < len(evidence.events) + 2
            or (
                self._target_agent_ids is not None and source["agent"] not in self._target_agent_ids
            )
        ):
            return ToolResult(content="unknown original result, claim or event", is_error=True)
        key = json.dumps(req, sort_keys=True)
        async with evidence.review_locks.setdefault(key, asyncio.Lock()):
            if key not in evidence.reviews:
                packet = json.dumps({"review": req, "evidence": evidence.events[version - 2]})
                task = (
                    "Review the quoted claim for the specified period using the supplied evidence. "
                    "Return JSON with status (unchanged, revised, or pending), "
                    "conclusion and reason. "
                    "For unchanged repeat the original claim exactly.\n" + packet
                )
                evidence.review_inputs[task] = version
                try:
                    reply = await self._bus.request(
                        context, target_agent_id=source["agent"], task=task
                    )
                    parsed = json.loads(reply.value)
                    if not isinstance(parsed, dict):
                        raise ValueError("invalid review response")
                    status, conclusion, reason = (
                        parsed.get(k) for k in ("status", "conclusion", "reason")
                    )
                    if not (
                        isinstance(reason, str)
                        and reason.strip()
                        and (
                            status == "pending"
                            or (status == "unchanged" and conclusion == req["claim"])
                            or (
                                status == "revised"
                                and isinstance(conclusion, str)
                                and conclusion.strip()
                                and conclusion != req["claim"]
                            )
                        )
                    ):
                        raise ValueError("invalid review response")
                    decision = {"status": status, "conclusion": conclusion, "reason": reason}
                except (ValueError, RuntimeError, TimeoutError) as exc:
                    decision = {"status": "pending", "reason": str(exc)}
                evidence.reviews[key] = json.dumps(
                    {"review": req, "agent": source["agent"], "decision": decision}
                )
            return ToolResult(content=evidence.reviews[key])

    async def execute_many(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
    ) -> tuple[ToolResult, ...]:
        if any("delivery" in item for item in arguments):
            if len(arguments) != 1:
                return tuple(
                    ToolResult(content="submit delivery alone", is_error=True) for _ in arguments
                )
            return (await self.execute(arguments[0], context),)
        if any("review" in item for item in arguments):
            ordinary = tuple(item for item in arguments if "review" not in item)
            reviews = tuple(item for item in arguments if "review" in item)
            normal_results, review_results = await asyncio.gather(
                self.execute_many(ordinary, context),
                asyncio.gather(*(self.execute(item, context) for item in reviews)),
            )
            normal_iter, review_iter = iter(normal_results), iter(review_results)
            return tuple(
                next(review_iter if "review" in item else normal_iter) for item in arguments
            )
        if not arguments:
            return ()
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

        try:
            if requests and context.evidence:
                context.evidence.started = True
            outcomes = await self._bus.batch(context, requests)
        except asyncio.CancelledError:
            logger.warning(
                "delegation batch cancelled (root=%s source=%s targets=%s)",
                context.root_execution_id,
                context.agent_id,
                ",".join(request.agent_id for request in requests),
            )
            raise
        for index, outcome in zip(request_indexes, outcomes, strict=True):
            if outcome.result is not None:
                self._remember(
                    context,
                    outcome.result.correlation_id,
                    outcome.request.agent_id,
                    outcome.result.value,
                )
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


class PublishEvidenceTool:
    definition = ToolDefinition(
        function=ToolFunction(
            name="publish_evidence",
            description="Share a sourced fact with the coordinator for this task.",
            parameters={
                "type": "object",
                "required": ["key", "content", "source"],
                "properties": {
                    k: {"type": "string"} for k in ("key", "content", "source", "publishedAt")
                },
            },
        )
    )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        if context.evidence is None:
            return ToolResult(content="missing delegated task", is_error=True)
        try:
            return ToolResult(content=context.evidence.publish(context, arguments))
        except ValueError as exc:
            return ToolResult(content=str(exc), is_error=True)

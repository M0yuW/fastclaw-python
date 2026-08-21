"""Trusted `spawn_subagent` tool backed by the in-process message bus."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.bus import (
    DelegationError,
    DelegationErrorCode,
    DelegationOutcome,
    DelegationRequest,
    MessageBus,
)
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools import ToolResult
from fastclaw.tools.football_shared import (
    FootballEvidenceCache,
    football_evidence_is_current,
    get_football_evidence_cache,
)

logger = logging.getLogger(__name__)

_SETTLEMENT_TASK = re.compile(
    r"复盘|结算|赛果|实际结果|核对(?:已结束)?比赛|核对.*赛果|"
    r"settle(?:ment)?\s+ledger|finished\s+matches",
    re.IGNORECASE,
)


def _is_settlement_task(task: str) -> bool:
    return bool(_SETTLEMENT_TASK.search(task))


class SpawnSubagentTool:
    def __init__(
        self,
        bus: MessageBus,
        target_agent_ids: tuple[str, ...] | None = None,
        *,
        data_agent_id: str = "",
        cache: FootballEvidenceCache | None = None,
    ) -> None:
        self._bus = bus
        self._target_agent_ids = target_agent_ids
        self._data_agent_id = data_agent_id
        self._cache = cache or get_football_evidence_cache()
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
        await self._hydrate_session_base(context)
        agent_id = arguments.get("agent_id")
        task = arguments.get("task")
        if not isinstance(agent_id, str) or not agent_id or not isinstance(task, str):
            return ToolResult(content="invalid delegation arguments", is_error=True)
        if self._target_agent_ids is not None and agent_id not in self._target_agent_ids:
            return ToolResult(content="delegation target is not allowed", is_error=True)
        settlement_task = _is_settlement_task(task)
        settlement_results_start = len(context.shared_state.football_settlement_results)
        settlement_errors_start = len(context.shared_state.football_settlement_errors)
        if settlement_task and self._data_agent_id and agent_id == self._data_agent_id:
            # This flag is trusted Runtime state, not model-provided input. It
            # narrows the child tool surface before the delegated run starts.
            context.shared_state.football_settlement_review = True
            context.shared_state.football_settlement_attempted = True
        if self._data_agent_id and not context.shared_state.has_football_base():
            if agent_id != self._data_agent_id:
                return ToolResult(
                    content=(
                        "the data analyst must confirm football evidence before other specialists"
                    ),
                    is_error=True,
                    metadata={"errorCode": "football_data_first"},
                )
        result = await self._bus.request(
            context,
            target_agent_id=agent_id,
            task=task,
        )
        if self._data_agent_id and agent_id == self._data_agent_id:
            if settlement_task:
                return self._settlement_result(
                    context,
                    child_report=str(result.value or ""),
                    correlation_id=result.correlation_id,
                    results_start=settlement_results_start,
                    errors_start=settlement_errors_start,
                )
            elif context.shared_state.has_football_negative():
                return self._negative_evidence_result(
                    context,
                    child_report=str(result.value or ""),
                    correlation_id=result.correlation_id,
                )
            elif not context.shared_state.has_football_base():
                detail = str(result.value or "").strip()
                if len(detail) > 500:
                    detail = f"{detail[:497]}..."
                return ToolResult(
                    content=(
                        "data analyst completed without publishing machine-readable football "
                        "base evidence; retry the data task and call football_data "
                        "action=base_evidence before reporting success"
                        + (f". Underlying result: {detail}" if detail else "")
                    ),
                    is_error=True,
                    metadata={"errorCode": "football_base_not_published"},
                )
        return ToolResult(
            content=result.value,
            metadata={"correlationId": result.correlation_id},
        )

    @staticmethod
    def _negative_evidence_result(
        context: ExecutionContext, *, child_report: str, correlation_id: str
    ) -> ToolResult:
        report = str(child_report or "").strip()
        if len(report) > 1000:
            report = f"{report[:997]}..."
        evidence: list[object] = []
        for content in context.shared_state.football_negative_evidence:
            try:
                evidence.append(json.loads(content))
            except json.JSONDecodeError:
                evidence.append({"raw": content})
        return ToolResult(
            content=json.dumps(
                {
                    "status": "no_match",
                    "evidence": evidence,
                    "child_report": report,
                    "instruction": (
                        "The requested fixture was not confirmed by the reviewed season "
                        "schedule. Stop prediction analysis and do not delegate other specialists."
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={
                "status": "fixture_unconfirmed",
                "correlationId": correlation_id,
                "negativeEvidenceCount": len(evidence),
            },
        )

    @staticmethod
    def _settlement_result(
        context: ExecutionContext,
        *,
        child_report: str,
        correlation_id: str,
        results_start: int = 0,
        errors_start: int = 0,
    ) -> ToolResult:
        """Return child observations even when its final prose turn failed.

        A settlement review is allowed to be partially successful. The data
        tool records each results response in shared trusted state, so a
        transient failure for one competition cannot erase verified rows from
        another competition.
        """

        report = str(child_report or "").strip()
        if len(report) > 1000:
            report = f"{report[:997]}..."
        observations: list[object] = []
        result_contents = context.shared_state.football_settlement_results[results_start:]
        error_contents = context.shared_state.football_settlement_errors[errors_start:]
        for content in result_contents:
            try:
                observations.append(json.loads(content))
            except json.JSONDecodeError:
                observations.append({"raw": content})
        verified_matches = SpawnSubagentTool._verified_matches(observations)
        payload = {
            "status": (
                "verified"
                if verified_matches
                and not error_contents
                else "partial"
                if verified_matches
                else "unavailable"
                if error_contents
                else "empty"
            ),
            "results": observations,
            "verified_matches": verified_matches,
            "errors": list(error_contents),
            "child_report": report,
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            metadata={
                "status": "settlement_review",
                "correlationId": correlation_id,
                "verifiedResultGroups": len(result_contents),
            },
        )

    @staticmethod
    def _verified_matches(observations: list[object]) -> list[dict[str, object]]:
        """Extract only explicitly finished rows with numeric final scores."""

        matches: list[dict[str, object]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for observation in observations:
            if not isinstance(observation, dict):
                continue
            source = str(observation.get("source") or "")
            for row in observation.get("rows") or ():
                if not isinstance(row, dict):
                    continue
                status = str(row.get("status") or "").strip()
                if not re.search(r"\bft\b|finished|final", status, re.IGNORECASE):
                    continue
                home = str(row.get("home") or "").strip()
                away = str(row.get("away") or "").strip()
                date = str(row.get("date") or "").strip()
                try:
                    home_score = int(str(row.get("home_score")))
                    away_score = int(str(row.get("away_score")))
                except (TypeError, ValueError):
                    continue
                if not home or not away or not date or home_score < 0 or away_score < 0:
                    continue
                key = (date, home.casefold(), away.casefold(), str(row.get("event_id") or ""))
                if key in seen:
                    continue
                seen.add(key)
                actual_result = (
                    "主胜"
                    if home_score > away_score
                    else "客胜"
                    if home_score < away_score
                    else "平"
                )
                matches.append(
                    {
                        "event_id": row.get("event_id"),
                        "competition": row.get("competition"),
                        "season": row.get("season"),
                        "date": date,
                        "match": row.get("match") or f"{home} vs {away}",
                        "home": home,
                        "away": away,
                        "actual_score": f"{home_score}-{away_score}",
                        "actual_result": actual_result,
                        "status": status,
                        "source": source,
                    }
                )
        return matches

    async def execute_many(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
    ) -> tuple[ToolResult, ...]:
        return await self._execute_many(arguments, context, timeout_seconds=None)

    async def execute_many_isolated(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
        *,
        timeout_seconds: float,
    ) -> tuple[ToolResult, ...]:
        """Apply the delegation timeout to each child, not the whole batch.

        One slow provider response must not cancel successful sibling analysts
        or turn every tool result into the same opaque timeout.
        """

        return await self._execute_many(
            arguments,
            context,
            timeout_seconds=timeout_seconds,
        )

    async def _execute_many(
        self,
        arguments: tuple[dict[str, Any], ...],
        context: ExecutionContext,
        *,
        timeout_seconds: float | None,
    ) -> tuple[ToolResult, ...]:
        await self._hydrate_session_base(context)
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

        data_indexes = [
            index
            for index, item in enumerate(arguments)
            if item.get("agent_id") == self._data_agent_id
        ]
        settlement_data_indexes = {
            index
            for index in data_indexes
            if _is_settlement_task(str(arguments[index].get("task") or ""))
        }
        if self._data_agent_id and data_indexes:
            # A single model turn may contain several data tasks (for example,
            # two groups of fixtures). They must be serialized so every base
            # bundle is published before specialists read football_context.
            # The old implementation ran only the first data task and marked
            # the remaining data calls as false `football_data_first` errors.
            for data_index in data_indexes:
                if timeout_seconds is None:
                    results[data_index] = await self.execute(arguments[data_index], context)
                else:
                    results[data_index] = await self._execute_one_with_timeout(
                        arguments[data_index],
                        context,
                        timeout_seconds=timeout_seconds,
                    )

            if any(results[index] is None or results[index].is_error for index in data_indexes):
                for index in range(len(arguments)):
                    if results[index] is None:
                        results[index] = ToolResult(
                            content=(
                                "the data analyst must confirm football evidence "
                                "before other specialists"
                            ),
                            is_error=True,
                            metadata={"errorCode": "football_data_first"},
                        )
                return tuple(result for result in results if result is not None)

            if settlement_data_indexes and not context.shared_state.has_football_base():
                context.shared_state.football_settlement_attempted = True
                # A result-verification delegation is its own phase. Do not
                # allow a same-turn specialist batch to bypass the base gate.
                for index in range(len(arguments)):
                    if results[index] is None:
                        results[index] = ToolResult(
                            content=(
                                "football settlement evidence must be reviewed before "
                                "other specialist delegations"
                            ),
                            is_error=True,
                            metadata={"errorCode": "football_settlement_first"},
                        )
                return tuple(result for result in results if result is not None)

            if (
                context.shared_state.has_football_negative()
                and not context.shared_state.has_football_base()
            ):
                for index in range(len(arguments)):
                    if results[index] is None:
                        results[index] = ToolResult(
                            content=(
                                "the requested fixture was not confirmed; stop prediction "
                                "analysis instead of delegating other specialists"
                            ),
                            is_error=True,
                            metadata={"errorCode": "football_fixture_unconfirmed"},
                        )
                return tuple(result for result in results if result is not None)

            retained = [
                (index, request)
                for index, request in zip(request_indexes, requests, strict=True)
                if index not in data_indexes
            ]
            request_indexes = [index for index, _request in retained]
            requests = [request for _index, request in retained]
        elif self._data_agent_id and not context.shared_state.has_football_base():
            for index in range(len(arguments)):
                if results[index] is None:
                    results[index] = ToolResult(
                        content="the data analyst must run first",
                        is_error=True,
                        metadata={"errorCode": "football_data_first"},
                    )
            return tuple(result for result in results if result is not None)

        if not requests:
            return tuple(result for result in results if result is not None)

        try:
            if timeout_seconds is None:
                outcomes = await self._bus.batch(context, requests)
            else:
                outcomes = await asyncio.gather(
                    *(
                        self._request_with_timeout(
                            context,
                            request,
                            timeout_seconds=timeout_seconds,
                        )
                        for request in requests
                    )
                )
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

    async def _execute_one_with_timeout(
        self,
        arguments: dict[str, Any],
        context: ExecutionContext,
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        try:
            async with asyncio.timeout(timeout_seconds):
                return await self.execute(arguments, context)
        except TimeoutError:
            return ToolResult(
                content="delegation timed out",
                is_error=True,
                metadata={
                    "errorCode": DelegationErrorCode.TIMEOUT,
                    "timeoutSeconds": timeout_seconds,
                },
            )
        except Exception:
            logger.exception(
                "data delegation failed (root=%s source=%s)",
                context.root_execution_id,
                context.agent_id,
            )
            return ToolResult(
                content="handler_error: delegated task failed",
                is_error=True,
                metadata={"errorCode": DelegationErrorCode.HANDLER_ERROR},
            )

    async def _request_with_timeout(
        self,
        context: ExecutionContext,
        request: DelegationRequest,
        *,
        timeout_seconds: float,
    ) -> DelegationOutcome:
        try:
            async with asyncio.timeout(timeout_seconds):
                return (await self._bus.batch(context, (request,)))[0]
        except TimeoutError:
            return DelegationOutcome(
                request=request,
                error=DelegationError(
                    DelegationErrorCode.TIMEOUT,
                    "delegation timed out",
                ),
            )

    async def _hydrate_session_base(self, context: ExecutionContext) -> None:
        """Restore confirmed evidence before applying the data-first gate.

        A continued session gets a fresh ``SharedExecutionState``.  Without
        this rehydration, the gate rejects the odds/tactics/history delegation
        before those specialists can call ``football_context`` themselves.
        """

        if context.shared_state.has_football_base():
            return
        for key, result in await self._cache.get_session_base(context.session_id):
            if not result.is_error and football_evidence_is_current(result.content):
                context.shared_state.publish_football_base(key.value, result.content)

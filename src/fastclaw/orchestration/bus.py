"""Tenant-safe request/reply message bus for agent delegation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.queue import (
    AsyncTaskQueue,
    BackpressureError,
    QueueShutdownError,
    TaskQueue,
    TaskResult,
    WaitTicket,
)

logger = logging.getLogger(__name__)


class MessageBusError(RuntimeError):
    pass


class UnknownAgentError(MessageBusError):
    pass


class CrossTenantError(MessageBusError):
    pass


class DelegationCycleError(MessageBusError):
    pass


class DelegatedTaskError(MessageBusError):
    def __init__(self, correlation_id: str) -> None:
        self.correlation_id = correlation_id
        super().__init__("delegated task failed")


class DelegationErrorCode(StrEnum):
    UNKNOWN_AGENT = "unknown_agent"
    CROSS_TENANT = "cross_tenant"
    CYCLE = "cycle"
    BACKPRESSURE = "backpressure"
    SHUTDOWN = "shutdown"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    HANDLER_ERROR = "handler_error"


AgentHandler = Callable[[str, ExecutionContext], Awaitable[str]]
WaitNode = tuple[str, str]


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    agent_id: str
    task: str


@dataclass(frozen=True, slots=True)
class DelegationError:
    code: DelegationErrorCode
    message: str
    correlation_id: str = ""


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    request: DelegationRequest
    result: TaskResult | None = None
    error: DelegationError | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None


class MessageBus(Protocol):
    async def request(
        self, context: ExecutionContext, target_agent_id: str, task: str
    ) -> TaskResult: ...

    async def batch(
        self, context: ExecutionContext, requests: Iterable[DelegationRequest]
    ) -> tuple[DelegationOutcome, ...]: ...

    async def cancel_root(self, root_execution_id: str) -> None: ...
    async def shutdown(self) -> None: ...


class InProcessMessageBus:
    def __init__(self, queue: TaskQueue | None = None) -> None:
        self._queue = queue or AsyncTaskQueue()
        self._handlers: dict[str, tuple[str, AgentHandler]] = {}
        self._wait_graph: dict[WaitNode, dict[WaitNode, int]] = {}
        self._wait_lock = asyncio.Lock()
        self._closing = False

    def register(self, *, user_id: str, agent_id: str, handler: AgentHandler) -> None:
        if self._closing:
            raise MessageBusError("message bus is shutting down")
        if agent_id in self._handlers:
            raise ValueError(f"agent {agent_id!r} is already registered")
        self._handlers[agent_id] = (user_id, handler)

    def unregister(self, agent_id: str) -> None:
        """Prevent new delegations while allowing already-started calls to finish."""
        self._handlers.pop(agent_id, None)

    async def request(
        self, context: ExecutionContext, target_agent_id: str, task: str
    ) -> TaskResult:
        if self._closing:
            raise MessageBusError("message bus is shutting down")
        registration = self._handlers.get(target_agent_id)
        if registration is None:
            raise UnknownAgentError(f"agent {target_agent_id!r} is not registered")
        owner_user_id, handler = registration
        if owner_user_id != context.user_id:
            raise CrossTenantError("cross-tenant agent delegation is denied")
        if target_agent_id in context.call_path:
            path = " -> ".join((*context.call_path, target_agent_id))
            raise DelegationCycleError(f"delegation cycle detected: {path}")

        source_node: WaitNode | None = None
        target_node = (context.root_execution_id, target_agent_id)
        if context.call_path:
            source_node = (context.root_execution_id, context.call_path[-1])
            await self._add_wait(source_node, target_node)

        correlation_id = str(uuid4())
        child_context = replace(
            context,
            agent_id=target_agent_id,
            call_path=(*context.call_path, target_agent_id),
        )

        async def run() -> TaskResult:
            try:
                value = await handler(task, child_context)
            except asyncio.CancelledError:
                raise
            except MessageBusError:
                raise
            except Exception as exc:
                logger.exception("delegated task %s failed", correlation_id)
                raise DelegatedTaskError(correlation_id) from exc
            return TaskResult(correlation_id=correlation_id, value=value)

        ticket: WaitTicket | None = None
        try:
            ticket = await self._queue.submit(
                target=(context.user_id, target_agent_id),
                dedup_key=(context.user_id, context.root_execution_id, target_agent_id, task),
                root_execution_id=context.root_execution_id,
                inherit_slot=bool(context.call_path),
                handler=run,
            )
            try:
                return await ticket.result()
            except asyncio.CancelledError:
                await ticket.release(cancel=True)
                raise
        finally:
            if ticket is not None:
                await ticket.release()
            if source_node is not None:
                await self._remove_wait(source_node, target_node)

    async def batch(
        self, context: ExecutionContext, requests: Iterable[DelegationRequest]
    ) -> tuple[DelegationOutcome, ...]:
        items = tuple(requests)
        results = await asyncio.gather(
            *(self.request(context, item.agent_id, item.task) for item in items),
            return_exceptions=True,
        )
        outcomes: list[DelegationOutcome] = []
        for item, result in zip(items, results, strict=True):
            if isinstance(result, BaseException):
                outcomes.append(DelegationOutcome(request=item, error=self._safe_error(result)))
            else:
                outcomes.append(DelegationOutcome(request=item, result=result))
        return tuple(outcomes)

    async def cancel_root(self, root_execution_id: str) -> None:
        await self._queue.cancel_root(root_execution_id)

    async def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self._queue.shutdown()
        async with self._wait_lock:
            self._wait_graph.clear()

    @staticmethod
    def _safe_error(error: BaseException) -> DelegationError:
        if isinstance(error, UnknownAgentError):
            return DelegationError(DelegationErrorCode.UNKNOWN_AGENT, "target agent is unavailable")
        if isinstance(error, CrossTenantError):
            return DelegationError(
                DelegationErrorCode.CROSS_TENANT, "target agent is not accessible"
            )
        if isinstance(error, DelegationCycleError):
            return DelegationError(DelegationErrorCode.CYCLE, "delegation cycle rejected")
        if isinstance(error, BackpressureError):
            return DelegationError(DelegationErrorCode.BACKPRESSURE, "delegation queue is full")
        if isinstance(error, QueueShutdownError):
            return DelegationError(
                DelegationErrorCode.SHUTDOWN, "delegation service is shutting down"
            )
        if isinstance(error, asyncio.CancelledError):
            return DelegationError(DelegationErrorCode.CANCELLED, "delegation was cancelled")
        if isinstance(error, TimeoutError):
            return DelegationError(DelegationErrorCode.TIMEOUT, "delegation timed out")
        correlation_id = error.correlation_id if isinstance(error, DelegatedTaskError) else ""
        return DelegationError(
            DelegationErrorCode.HANDLER_ERROR,
            "delegated task failed",
            correlation_id=correlation_id,
        )

    async def _add_wait(self, source: WaitNode, target: WaitNode) -> None:
        async with self._wait_lock:
            targets = self._wait_graph.setdefault(source, {})
            targets[target] = targets.get(target, 0) + 1
            if self._has_path(target, source):
                self._decrement_wait(source, target)
                raise DelegationCycleError("delegation wait graph contains a cycle")

    async def _remove_wait(self, source: WaitNode, target: WaitNode) -> None:
        async with self._wait_lock:
            self._decrement_wait(source, target)

    def _decrement_wait(self, source: WaitNode, target: WaitNode) -> None:
        targets = self._wait_graph.get(source)
        if targets is None or target not in targets:
            return
        if targets[target] == 1:
            targets.pop(target)
        else:
            targets[target] -= 1
        if not targets:
            self._wait_graph.pop(source, None)

    def _has_path(self, start: WaitNode, goal: WaitNode) -> bool:
        pending = [start]
        visited: set[WaitNode] = set()
        while pending:
            node = pending.pop()
            if node == goal:
                return True
            if node in visited:
                continue
            visited.add(node)
            pending.extend(self._wait_graph.get(node, {}))
        return False

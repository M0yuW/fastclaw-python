"""Tenant-safe request/reply message bus for agent delegation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration.queue import AsyncTaskQueue, TaskQueue, TaskResult


class MessageBusError(RuntimeError):
    pass


class UnknownAgentError(MessageBusError):
    pass


class CrossTenantError(MessageBusError):
    pass


class DelegationCycleError(MessageBusError):
    pass


AgentHandler = Callable[[str, ExecutionContext], Awaitable[str]]
WaitNode = tuple[str, str]


@dataclass(frozen=True, slots=True)
class DelegationRequest:
    agent_id: str
    task: str


class MessageBus(Protocol):
    async def request(
        self, context: ExecutionContext, target_agent_id: str, task: str
    ) -> TaskResult: ...

    async def batch(
        self, context: ExecutionContext, requests: Iterable[DelegationRequest]
    ) -> tuple[TaskResult, ...]: ...

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
            value = await handler(task, child_context)
            return TaskResult(correlation_id=correlation_id, value=value)

        try:
            future = await self._queue.submit(
                target=(context.user_id, target_agent_id),
                dedup_key=(context.root_execution_id, target_agent_id, task),
                root_execution_id=context.root_execution_id,
                inherit_slot=bool(context.call_path),
                handler=run,
            )
            try:
                return await asyncio.shield(future)
            except asyncio.CancelledError:
                await self._queue.cancel_root(context.root_execution_id)
                raise
        finally:
            if source_node is not None:
                await self._remove_wait(source_node, target_node)

    async def batch(
        self, context: ExecutionContext, requests: Iterable[DelegationRequest]
    ) -> tuple[TaskResult, ...]:
        return tuple(
            await asyncio.gather(
                *(self.request(context, item.agent_id, item.task) for item in requests)
            )
        )

    async def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        await self._queue.shutdown()
        async with self._wait_lock:
            self._wait_graph.clear()

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

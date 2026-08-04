from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration import (
    AsyncTaskQueue,
    BackpressureError,
    CrossTenantError,
    DelegationCycleError,
    DelegationRequest,
    InProcessMessageBus,
    QueueShutdownError,
)


def context(
    *, root: str = "root-1", agent_id: str = "gateway", path: tuple[str, ...] = ()
) -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id=agent_id,
        session_id="session-1",
        root_execution_id=root,
        call_path=path,
    )


@pytest.mark.asyncio
async def test_nested_delegation_does_not_deadlock_at_max_concurrent_one() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=1))

    async def specialist(task: str, child: ExecutionContext) -> str:
        assert child.call_path == ("coordinator", "specialist")
        return f"specialist:{task}"

    async def coordinator(task: str, child: ExecutionContext) -> str:
        result = await bus.request(child, "specialist", task)
        return f"coordinator:{result.value}"

    bus.register(user_id="user-1", agent_id="coordinator", handler=coordinator)
    bus.register(user_id="user-1", agent_id="specialist", handler=specialist)

    result = await asyncio.wait_for(bus.request(context(), "coordinator", "analyze"), timeout=1)

    assert result.value == "coordinator:specialist:analyze"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_same_target_is_fifo_while_different_targets_run_in_parallel() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=2))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execution_order: list[str] = []

    async def serial(task: str, child: ExecutionContext) -> str:
        del child
        execution_order.append(f"start:{task}")
        if task == "first":
            first_started.set()
            await release_first.wait()
        execution_order.append(f"done:{task}")
        return task

    parallel_started = {"left": asyncio.Event(), "right": asyncio.Event()}

    def parallel_handler(name: str) -> Callable[[str, ExecutionContext], Awaitable[str]]:
        async def handler(task: str, child: ExecutionContext) -> str:
            del task, child
            parallel_started[name].set()
            await parallel_started["right" if name == "left" else "left"].wait()
            return name

        return handler

    bus.register(user_id="user-1", agent_id="serial", handler=serial)
    bus.register(user_id="user-1", agent_id="left", handler=parallel_handler("left"))
    bus.register(user_id="user-1", agent_id="right", handler=parallel_handler("right"))

    first = asyncio.create_task(bus.request(context(root="one"), "serial", "first"))
    await first_started.wait()
    second = asyncio.create_task(bus.request(context(root="two"), "serial", "second"))
    await asyncio.sleep(0)
    assert execution_order == ["start:first"]
    release_first.set()
    await asyncio.gather(first, second)
    assert execution_order == ["start:first", "done:first", "start:second", "done:second"]

    results = await asyncio.wait_for(
        asyncio.gather(
            bus.request(context(root="left-root"), "left", "task"),
            bus.request(context(root="right-root"), "right", "task"),
        ),
        timeout=1,
    )
    assert {result.value for result in results} == {"left", "right"}
    await bus.shutdown()


@pytest.mark.asyncio
async def test_batch_deduplicates_exact_agent_and_task_within_root() -> None:
    bus = InProcessMessageBus()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del child
        calls += 1
        await asyncio.sleep(0)
        return task.upper()

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    results = await bus.batch(
        context(),
        (
            DelegationRequest(agent_id="worker", task="same"),
            DelegationRequest(agent_id="worker", task="same"),
            DelegationRequest(agent_id="worker", task="different"),
        ),
    )

    assert calls == 2
    assert results[0] == results[1]
    assert results[2].value == "DIFFERENT"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cycles_and_cross_tenant_requests_are_rejected() -> None:
    bus = InProcessMessageBus()

    async def agent_a(task: str, child: ExecutionContext) -> str:
        del task
        return (await bus.request(child, "agent-b", "from-a")).value

    async def agent_b(task: str, child: ExecutionContext) -> str:
        del task
        return (await bus.request(child, "agent-a", "from-b")).value

    bus.register(user_id="user-1", agent_id="agent-a", handler=agent_a)
    bus.register(user_id="user-1", agent_id="agent-b", handler=agent_b)
    bus.register(user_id="user-2", agent_id="foreign", handler=agent_b)

    with pytest.raises(DelegationCycleError, match="cycle"):
        await bus.request(context(), "agent-a", "start")
    with pytest.raises(CrossTenantError):
        await bus.request(context(), "foreign", "denied")
    await bus.shutdown()


@pytest.mark.asyncio
async def test_wait_graph_rejects_a_cycle_even_with_inconsistent_call_paths() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="agent-a", handler=blocking)
    bus.register(user_id="user-1", agent_id="agent-b", handler=blocking)
    first = asyncio.create_task(
        bus.request(
            context(root="shared", agent_id="agent-a", path=("agent-a",)),
            "agent-b",
            "first",
        )
    )
    await started.wait()

    with pytest.raises(DelegationCycleError, match="wait graph"):
        await bus.request(
            context(root="shared", agent_id="agent-b", path=("agent-b",)),
            "agent-a",
            "second",
        )

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await bus.shutdown()


@pytest.mark.asyncio
async def test_backpressure_rejects_more_than_configured_pending_tasks() -> None:
    queue = AsyncTaskQueue(max_concurrent=1, max_pending=1)
    bus = InProcessMessageBus(queue)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await release.wait()
        return "done"

    bus.register(user_id="user-1", agent_id="first", handler=blocking)
    bus.register(user_id="user-1", agent_id="second", handler=blocking)
    running = asyncio.create_task(bus.request(context(root="one"), "first", "task"))
    await started.wait()

    with pytest.raises(BackpressureError, match="pending task limit"):
        await bus.request(context(root="two"), "second", "task")

    release.set()
    await running
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cancellation_propagates_to_running_handler_and_clears_pending() -> None:
    queue = AsyncTaskQueue()
    bus = InProcessMessageBus(queue)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="worker", handler=blocking)
    request = asyncio.create_task(bus.request(context(), "worker", "task"))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert queue.pending_count == 0
    await bus.shutdown()


@pytest.mark.asyncio
async def test_shutdown_completes_waiters_once_and_is_idempotent() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="worker", handler=blocking)
    request = asyncio.create_task(bus.request(context(), "worker", "task"))
    await started.wait()
    await bus.shutdown()
    await bus.shutdown()

    with pytest.raises(QueueShutdownError):
        await request

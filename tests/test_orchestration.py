from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.orchestration import (
    AsyncTaskQueue,
    BackpressureError,
    CrossTenantError,
    DelegationCycleError,
    DelegationErrorCode,
    DelegationRequest,
    InProcessMessageBus,
    QueueShutdownError,
    SpawnSubagentTool,
    TaskResult,
    WaitTicket,
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


def test_spawn_subagent_schema_exposes_only_delegation_arguments() -> None:
    tool = SpawnSubagentTool(InProcessMessageBus())

    properties = tool.definition.function.parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"agent_id", "task"}


@pytest.mark.asyncio
async def test_spawn_subagent_ignores_model_supplied_identity() -> None:
    bus = InProcessMessageBus()
    received: list[ExecutionContext] = []

    async def handler(task: str, child: ExecutionContext) -> str:
        assert task == "delegated task"
        received.append(child)
        return "complete"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    tool = SpawnSubagentTool(bus)
    try:
        result = await tool.execute(
            {
                "agent_id": "worker",
                "task": "delegated task",
                "user_id": "attacker",
                "userId": "attacker",
                "root_execution_id": "attacker-root",
                "rootExecutionId": "attacker-root",
                "call_path": ["attacker"],
                "callPath": ["attacker"],
            },
            context(),
        )

        assert result.content == "complete"
        assert len(received) == 1
        assert received[0].user_id == "user-1"
        assert received[0].root_execution_id == "root-1"
        assert received[0].call_path == ("worker",)
    finally:
        await bus.shutdown()


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
    assert results[0].result == results[1].result
    assert results[0].succeeded
    assert results[2].result is not None
    assert results[2].result.value == "DIFFERENT"
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


def test_duplicate_agent_registration_is_rejected() -> None:
    bus = InProcessMessageBus()

    async def handler(task: str, child: ExecutionContext) -> str:
        del task, child
        return "done"

    bus.register(user_id="user-1", agent_id="global-agent-id", handler=handler)
    with pytest.raises(ValueError, match="already registered"):
        bus.register(user_id="user-1", agent_id="global-agent-id", handler=handler)


@pytest.mark.asyncio
async def test_cancelling_one_shared_waiter_does_not_cancel_the_job() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del task, child
        calls += 1
        started.set()
        await release.wait()
        return "shared-result"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    first = asyncio.create_task(bus.request(context(), "worker", "same"))
    second = asyncio.create_task(bus.request(context(), "worker", "same"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    assert (await second).value == "shared-result"
    assert calls == 1
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cancelling_one_root_branch_does_not_cancel_its_sibling() -> None:
    bus = InProcessMessageBus()
    started = {"left": asyncio.Event(), "right": asyncio.Event()}
    release_right = asyncio.Event()

    def make_handler(name: str) -> Callable[[str, ExecutionContext], Awaitable[str]]:
        async def handler(task: str, child: ExecutionContext) -> str:
            del task, child
            started[name].set()
            if name == "right":
                await release_right.wait()
            else:
                await asyncio.Event().wait()
            return name

        return handler

    bus.register(user_id="user-1", agent_id="left", handler=make_handler("left"))
    bus.register(user_id="user-1", agent_id="right", handler=make_handler("right"))
    left = asyncio.create_task(bus.request(context(), "left", "task"))
    right = asyncio.create_task(bus.request(context(), "right", "task"))
    await asyncio.gather(started["left"].wait(), started["right"].wait())
    left.cancel()
    with pytest.raises(asyncio.CancelledError):
        await left
    release_right.set()

    assert (await right).value == "right"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_new_submit_does_not_attach_to_a_cancelling_dedup_job() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del task, child
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
        return f"run-{calls}"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    first = asyncio.create_task(bus.request(context(), "worker", "same"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await cleaning.wait()
    replacement = asyncio.create_task(bus.request(context(), "worker", "same"))
    await asyncio.sleep(0)
    release_cleanup.set()

    assert (await replacement).value == "run-2"
    assert calls == 2
    await bus.shutdown()


@pytest.mark.asyncio
async def test_batch_returns_all_results_in_order_and_redacts_errors() -> None:
    bus = InProcessMessageBus()

    async def success(task: str, child: ExecutionContext) -> str:
        del child
        await asyncio.sleep(0)
        return task.upper()

    async def failure(task: str, child: ExecutionContext) -> str:
        del task, child
        raise RuntimeError("database /private/secret.db on internal.example failed")

    bus.register(user_id="user-1", agent_id="success", handler=success)
    bus.register(user_id="user-1", agent_id="failure", handler=failure)
    outcomes = await bus.batch(
        context(),
        (
            DelegationRequest("success", "first"),
            DelegationRequest("failure", "second"),
            DelegationRequest("missing", "third"),
            DelegationRequest("success", "fourth"),
        ),
    )

    assert [outcome.request.task for outcome in outcomes] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    assert outcomes[0].result is not None and outcomes[0].result.value == "FIRST"
    assert outcomes[1].error is not None
    assert outcomes[1].error.code is DelegationErrorCode.HANDLER_ERROR
    assert "/private" not in outcomes[1].error.message
    assert "internal.example" not in outcomes[1].error.message
    assert outcomes[2].error is not None
    assert outcomes[2].error.code is DelegationErrorCode.UNKNOWN_AGENT
    assert outcomes[3].result is not None and outcomes[3].result.value == "FOURTH"
    await bus.shutdown()


async def test_seeded_submit_cancel_shutdown_interleavings_leave_no_jobs() -> None:
    for seed in range(12):
        generator = random.Random(seed)
        queue = AsyncTaskQueue(max_concurrent=3, max_pending=24)
        tickets: list[WaitTicket] = []

        async def completed(index: int, delay: float) -> TaskResult:
            await asyncio.sleep(delay)
            return TaskResult(correlation_id=f"correlation-{index}", value=str(index))

        for index in range(32):
            action = generator.randrange(4)
            root = f"root-{generator.randrange(5)}"
            if action < 2:
                try:
                    delay = generator.random() / 1000

                    async def handler(
                        index: int = index,
                        delay: float = delay,
                    ) -> TaskResult:
                        return await completed(index, delay)

                    ticket = await queue.submit(
                        target=("user", f"agent-{generator.randrange(4)}"),
                        dedup_key=("user", root, f"agent-{index % 4}", f"task-{index}"),
                        root_execution_id=root,
                        inherit_slot=False,
                        handler=handler,
                    )
                except BackpressureError:
                    continue
                tickets.append(ticket)
            elif action == 2:
                await queue.cancel_root(root)
            elif tickets:
                await tickets[generator.randrange(len(tickets))].release(cancel=True)
            await asyncio.sleep(0)

        await queue.shutdown()
        await asyncio.gather(*(ticket.result() for ticket in tickets), return_exceptions=True)

        assert queue.pending_count == 0
        assert not queue._workers


async def test_task_snapshots_keep_safe_recent_terminal_state() -> None:
    queue = AsyncTaskQueue()

    async def handler() -> TaskResult:
        return TaskResult(correlation_id="correlation", value="complete")

    ticket = await queue.submit(
        target=("user", "agent"),
        dedup_key=("user", "root", "agent", "task"),
        root_execution_id="root",
        inherit_slot=False,
        handler=handler,
    )
    assert (await ticket.result()).value == "complete"
    await ticket.release()
    await asyncio.sleep(0)

    snapshots = queue.recent_tasks()
    assert len(snapshots) == 1
    assert snapshots[0].agent_id == "agent"
    assert snapshots[0].chat_key == "root"
    assert snapshots[0].status == "completed"
    assert snapshots[0].error == ""
    assert snapshots[0].done_at is not None
    await queue.shutdown()

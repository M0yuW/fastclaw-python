# Phase 4: Multi-Agent runtime

## Contracts

`MessageBus` defines correlated request/reply and batch delegation.
`TaskQueue` defines bounded submission, root cancellation, and shutdown. The
first implementation is deliberately single-process and asyncio-native; these
protocols are the seam for a future Redis broker without introducing
distributed consistency into the first cutover.

Each registered Agent has one tenant owner. Requests to another tenant are
rejected before enqueue. `spawn_subagent` receives only `agent_id` and `task`
from tool arguments, while user, Session, root execution, and call path come
from the trusted `ExecutionContext`.

## Scheduling semantics

- Each target Agent has a FIFO queue; different targets may run concurrently.
- A global semaphore bounds root executions.
- Nested work inherits its root's slot, preventing a parent/child deadlock when
  `max_concurrent=1`.
- Pending work is capped at 256 by default and fails immediately with a
  backpressure error when saturated.
- Exact `(agent_id, task)` requests within the same root execution share one
  correlation Future. Tenant and root are also part of the internal key to
  prevent cross-request result or cancellation leakage.
- Call paths reject direct recursion and a reference-counted wait graph rejects
  cycles formed by concurrent waits.

## Cancellation and shutdown

Cancelling a request cancels queued and running work for its root execution.
Shared dedup Futures are shielded from an individual waiter until root
cancellation is propagated. Shutdown rejects new work, completes every pending
Future exactly once with a shutdown error, cancels executions, drains workers,
and is idempotent.

## Acceptance evidence

Tests cover `max_concurrent=1` nested delegation, same-target FIFO,
cross-target parallelism, exact batch deduplication, call-path and wait-graph
cycles, cross-tenant denial, queue saturation, cancellation propagation,
pending cleanup, and repeated shutdown.

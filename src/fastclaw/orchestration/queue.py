"""Bounded in-process task queue with per-target FIFO execution."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class QueueError(RuntimeError):
    pass


class BackpressureError(QueueError):
    pass


class QueueShutdownError(QueueError):
    pass


@dataclass(frozen=True, slots=True)
class TaskResult:
    correlation_id: str
    value: str


TaskHandler = Callable[[], Awaitable[TaskResult]]
DedupKey = tuple[str, str, str, str]
TargetKey = tuple[str, str]


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    FINISHED = "finished"


class TaskQueue(Protocol):
    async def submit(
        self,
        *,
        target: TargetKey,
        dedup_key: DedupKey,
        root_execution_id: str,
        inherit_slot: bool,
        handler: TaskHandler,
    ) -> WaitTicket: ...

    async def cancel_root(self, root_execution_id: str) -> None: ...
    async def shutdown(self) -> None: ...


@dataclass(slots=True)
class _Job:
    target: TargetKey
    dedup_key: DedupKey
    root_execution_id: str
    inherit_slot: bool
    handler: TaskHandler
    future: asyncio.Future[TaskResult]
    generation: int
    state: JobState = JobState.QUEUED
    waiters: set[int] = field(default_factory=set)
    execution: asyncio.Task[TaskResult] | None = None


class WaitTicket:
    """One waiter's cancellable reference to a potentially shared Job."""

    def __init__(self, queue: AsyncTaskQueue, job: _Job, token: int) -> None:
        self._queue = queue
        self._job = job
        self._token = token
        self._released = False

    async def result(self) -> TaskResult:
        return await asyncio.shield(self._job.future)

    async def release(self, *, cancel: bool = False) -> None:
        if self._released:
            return
        self._released = True
        await self._queue._release_waiter(self._job, self._token, cancel=cancel)


class AsyncTaskQueue:
    """Serialize each target while allowing bounded cross-target concurrency."""

    def __init__(self, *, max_concurrent: int = 8, max_pending: int = 256) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be positive")
        if max_pending < 1:
            raise ValueError("max_pending must be positive")
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_pending = max_pending
        self._lock = asyncio.Lock()
        self._queues: dict[TargetKey, deque[_Job]] = {}
        self._workers: dict[TargetKey, asyncio.Task[None]] = {}
        self._jobs: dict[DedupKey, _Job] = {}
        self._closing = False
        self._next_waiter = 0
        self._next_generation = 0

    @property
    def pending_count(self) -> int:
        return len(self._jobs)

    async def submit(
        self,
        *,
        target: TargetKey,
        dedup_key: DedupKey,
        root_execution_id: str,
        inherit_slot: bool,
        handler: TaskHandler,
    ) -> WaitTicket:
        async with self._lock:
            if self._closing:
                raise QueueShutdownError("task queue is shutting down")
            existing = self._jobs.get(dedup_key)
            if existing is not None and existing.state in {
                JobState.QUEUED,
                JobState.RUNNING,
            }:
                token = self._new_waiter(existing)
                return WaitTicket(self, existing, token)
            if len(self._jobs) >= self._max_pending:
                raise BackpressureError(
                    f"task queue reached its {self._max_pending} pending task limit"
                )
            future = asyncio.get_running_loop().create_future()
            self._next_generation += 1
            job = _Job(
                target=target,
                dedup_key=dedup_key,
                root_execution_id=root_execution_id,
                inherit_slot=inherit_slot,
                handler=handler,
                future=future,
                generation=self._next_generation,
            )
            token = self._new_waiter(job)
            self._jobs[dedup_key] = job
            self._queues.setdefault(target, deque()).append(job)
            worker = self._workers.get(target)
            if worker is None or worker.done():
                self._workers[target] = asyncio.create_task(self._drain(target))
            return WaitTicket(self, job, token)

    def _new_waiter(self, job: _Job) -> int:
        self._next_waiter += 1
        job.waiters.add(self._next_waiter)
        return self._next_waiter

    async def _release_waiter(self, job: _Job, token: int, *, cancel: bool) -> None:
        execution: asyncio.Task[TaskResult] | None = None
        async with self._lock:
            if token not in job.waiters:
                return
            job.waiters.remove(token)
            if cancel and not job.waiters and job.state in {JobState.QUEUED, JobState.RUNNING}:
                job.state = JobState.CANCELLING
                if self._jobs.get(job.dedup_key) is job:
                    self._jobs.pop(job.dedup_key, None)
                if not job.future.done():
                    job.future.cancel()
                execution = job.execution
        if execution is not None and not execution.done():
            execution.cancel()

    async def cancel_root(self, root_execution_id: str) -> None:
        executions: list[asyncio.Task[TaskResult]] = []
        async with self._lock:
            matches = [
                job
                for job in self._jobs.values()
                if job.root_execution_id == root_execution_id
                and job.state in {JobState.QUEUED, JobState.RUNNING}
            ]
            for job in matches:
                job.state = JobState.CANCELLING
                if self._jobs.get(job.dedup_key) is job:
                    self._jobs.pop(job.dedup_key, None)
                if not job.future.done():
                    job.future.cancel()
                if job.execution is not None and not job.execution.done():
                    executions.append(job.execution)
        for execution in executions:
            execution.cancel()

    async def shutdown(self) -> None:
        async with self._lock:
            if not self._closing:
                self._closing = True
                for job in tuple(self._jobs.values()):
                    job.state = JobState.CANCELLING
                    if not job.future.done():
                        job.future.set_exception(QueueShutdownError("task queue shut down"))
                    if job.execution is not None and not job.execution.done():
                        job.execution.cancel()
                self._jobs.clear()
            workers = tuple(self._workers.values())
            for worker in workers:
                if not worker.done():
                    worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._lock:
            self._queues.clear()
            self._workers.clear()

    async def _drain(self, target: TargetKey) -> None:
        try:
            while True:
                async with self._lock:
                    queue = self._queues.get(target)
                    if not queue:
                        self._queues.pop(target, None)
                        return
                    job = queue.popleft()
                    if job.state is not JobState.QUEUED:
                        continue
                    job.state = JobState.RUNNING
                    job.execution = asyncio.create_task(self._execute(job))
                try:
                    result = await job.execution
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        raise
                    await self._finish(job, cancelled=True)
                except BaseException as exc:
                    await self._finish(job, error=exc)
                else:
                    await self._finish(job, result=result)
        finally:
            async with self._lock:
                if self._workers.get(target) is asyncio.current_task():
                    self._workers.pop(target, None)

    async def _execute(self, job: _Job) -> TaskResult:
        if job.inherit_slot:
            return await job.handler()
        async with self._semaphore:
            return await job.handler()

    async def _finish(
        self,
        job: _Job,
        *,
        result: TaskResult | None = None,
        error: BaseException | None = None,
        cancelled: bool = False,
    ) -> None:
        async with self._lock:
            if job.state is not JobState.CANCELLING:
                job.state = JobState.FINISHED
            if self._jobs.get(job.dedup_key) is job:
                self._jobs.pop(job.dedup_key, None)
            if job.future.done():
                return
            if cancelled:
                job.future.cancel()
            elif error is not None:
                job.future.set_exception(error)
            elif result is not None:
                job.future.set_result(result)

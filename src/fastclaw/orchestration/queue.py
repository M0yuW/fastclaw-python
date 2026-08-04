"""Bounded in-process task queue with per-target FIFO execution."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
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
DedupKey = tuple[str, str, str]
TargetKey = tuple[str, str]


class TaskQueue(Protocol):
    async def submit(
        self,
        *,
        target: TargetKey,
        dedup_key: DedupKey,
        root_execution_id: str,
        inherit_slot: bool,
        handler: TaskHandler,
    ) -> asyncio.Future[TaskResult]: ...

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
    execution: asyncio.Task[TaskResult] | None = None


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
    ) -> asyncio.Future[TaskResult]:
        async with self._lock:
            if self._closing:
                raise QueueShutdownError("task queue is shutting down")
            existing = self._jobs.get(dedup_key)
            if existing is not None:
                return existing.future
            if len(self._jobs) >= self._max_pending:
                raise BackpressureError(
                    f"task queue reached its {self._max_pending} pending task limit"
                )
            future = asyncio.get_running_loop().create_future()
            job = _Job(
                target=target,
                dedup_key=dedup_key,
                root_execution_id=root_execution_id,
                inherit_slot=inherit_slot,
                handler=handler,
                future=future,
            )
            self._jobs[dedup_key] = job
            self._queues.setdefault(target, deque()).append(job)
            worker = self._workers.get(target)
            if worker is None or worker.done():
                self._workers[target] = asyncio.create_task(self._drain(target))
            return future

    async def cancel_root(self, root_execution_id: str) -> None:
        async with self._lock:
            matches = [
                job for job in self._jobs.values() if job.root_execution_id == root_execution_id
            ]
            for job in matches:
                if not job.future.done():
                    job.future.cancel()
                if job.execution is not None and not job.execution.done():
                    job.execution.cancel()

    async def shutdown(self) -> None:
        async with self._lock:
            if self._closing:
                workers = tuple(self._workers.values())
            else:
                self._closing = True
                for job in self._jobs.values():
                    if not job.future.done():
                        job.future.set_exception(QueueShutdownError("task queue shut down"))
                    if job.execution is not None and not job.execution.done():
                        job.execution.cancel()
                workers = tuple(self._workers.values())
                for worker in workers:
                    worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        async with self._lock:
            self._queues.clear()
            self._workers.clear()
            self._jobs.clear()

    async def _drain(self, target: TargetKey) -> None:
        while True:
            async with self._lock:
                queue = self._queues.get(target)
                if not queue:
                    self._queues.pop(target, None)
                    self._workers.pop(target, None)
                    return
                job = queue.popleft()
            if job.future.cancelled():
                await self._forget(job)
                continue
            try:
                job.execution = asyncio.create_task(self._execute(job))
                result = await job.execution
                if not job.future.done():
                    job.future.set_result(result)
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                if not job.future.done():
                    job.future.cancel()
            except BaseException as exc:
                if not job.future.done():
                    job.future.set_exception(exc)
            finally:
                await self._forget(job)

    async def _execute(self, job: _Job) -> TaskResult:
        if job.inherit_slot:
            return await job.handler()
        async with self._semaphore:
            return await job.handler()

    async def _forget(self, job: _Job) -> None:
        async with self._lock:
            if self._jobs.get(job.dedup_key) is job:
                self._jobs.pop(job.dedup_key, None)

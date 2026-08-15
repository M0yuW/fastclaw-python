"""Trusted execution metadata propagated independently of model arguments."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field


@dataclass(slots=True)
class SharedExecutionState:
    """Small mutable state shared by one root run and its delegated Agents."""

    football_base_evidence: dict[str, str] = field(default_factory=dict)
    ev_requested: bool = False

    def publish_football_base(self, key: str, content: str) -> None:
        self.football_base_evidence[key] = content

    def football_base(self, key: str) -> str | None:
        content = self.football_base_evidence.get(key)
        if content is not None:
            return content
        if len(self.football_base_evidence) == 1:
            return next(iter(self.football_base_evidence.values()))
        return None

    def has_football_base(self) -> bool:
        return bool(self.football_base_evidence)

    def render_football_base(self) -> str:
        if not self.football_base_evidence:
            return ""
        return next(reversed(self.football_base_evidence.values()))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_id: str
    agent_id: str
    session_id: str
    root_execution_id: str
    call_path: tuple[str, ...] = ()
    task_id: str = ""
    shared_state: SharedExecutionState = field(default_factory=SharedExecutionState)


_execution: ContextVar[ExecutionContext | None] = ContextVar("fastclaw_execution", default=None)


@contextmanager
def use_execution(context: ExecutionContext) -> Iterator[ExecutionContext]:
    token: Token[ExecutionContext | None] = _execution.set(context)
    try:
        yield context
    finally:
        _execution.reset(token)


def require_execution() -> ExecutionContext:
    context = _execution.get()
    if context is None:
        raise RuntimeError("trusted execution context is missing")
    return context


def current_execution() -> ExecutionContext | None:
    return _execution.get()

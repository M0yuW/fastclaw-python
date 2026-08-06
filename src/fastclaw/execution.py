"""Trusted execution metadata propagated independently of model arguments."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_id: str
    agent_id: str
    session_id: str
    root_execution_id: str
    call_path: tuple[str, ...] = ()


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

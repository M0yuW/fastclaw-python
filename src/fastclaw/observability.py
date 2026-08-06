"""Structured, context-aware service logging without model-visible details."""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastclaw.execution import current_execution

_correlation_id: ContextVar[str] = ContextVar("fastclaw_correlation_id", default="")


@contextmanager
def use_correlation_id(value: str = "") -> Any:
    correlation_id = value or f"req_{uuid4().hex}"
    token: Token[str] = _correlation_id.set(correlation_id)
    try:
        yield correlation_id
    finally:
        _correlation_id.reset(token)


def current_correlation_id() -> str:
    return _correlation_id.get()


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        execution = current_execution()
        record.correlation_id = current_correlation_id()
        record.root_execution_id = execution.root_execution_id if execution else ""
        record.call_path = list(execution.call_path) if execution else []
        record.user_id = execution.user_id if execution else ""
        record.agent_id = execution.agent_id if execution else ""
        record.session_id = execution.session_id if execution else ""
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
            "correlationId": getattr(record, "correlation_id", ""),
            "rootExecutionId": getattr(record, "root_execution_id", ""),
            "callPath": getattr(record, "call_path", []),
            "userId": getattr(record, "user_id", ""),
            "agentId": getattr(record, "agent_id", ""),
            "sessionId": getattr(record, "session_id", ""),
        }
        if record.exc_info:
            exception_type = record.exc_info[0]
            if exception_type is not None:
                payload["exception"] = exception_type.__name__
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_json_logging() -> None:
    context_filter = ContextFilter()
    formatter = JsonFormatter()
    root = logging.getLogger()
    if not root.handlers:
        root.addHandler(logging.StreamHandler())
    for handler in root.handlers:
        handler.addFilter(context_filter)
        handler.setFormatter(formatter)

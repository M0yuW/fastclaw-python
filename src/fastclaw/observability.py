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


def log_stage(
    logger: logging.Logger,
    stage: str,
    *,
    context: Any | None = None,
    task_id: str = "",
    target_agent_id: str = "",
    provider: str = "",
    tool: str = "",
    outcome: str = "",
    duration_ms: int | None = None,
    level: int = logging.INFO,
) -> None:
    """Emit one sanitized execution-stage event for service operators."""
    execution = context or current_execution()
    fields: dict[str, Any] = {
        "stage": stage,
        "task_id": task_id or (execution.task_id if execution is not None else ""),
        "target_agent_id": target_agent_id,
        "provider_name": provider,
        "tool_name": tool,
        "outcome": outcome,
        "root_execution_id": execution.root_execution_id if execution is not None else "",
        "call_path": list(execution.call_path) if execution is not None else [],
        "user_id": execution.user_id if execution is not None else "",
        "agent_id": execution.agent_id if execution is not None else "",
        "session_id": execution.session_id if execution is not None else "",
    }
    if duration_ms is not None:
        fields["duration_ms"] = duration_ms
    root_id = execution.root_execution_id if execution is not None else ""
    agent_id = execution.agent_id if execution is not None else ""
    logger.log(
        level,
        "execution stage=%s task=%s root=%s agent=%s target=%s outcome=%s duration_ms=%s",
        stage,
        fields["task_id"],
        root_id,
        agent_id,
        target_agent_id,
        outcome,
        "" if duration_ms is None else duration_ms,
        extra=fields,
    )


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        execution = current_execution()
        record.correlation_id = current_correlation_id()
        record.root_execution_id = getattr(record, "root_execution_id", "") or (
            execution.root_execution_id if execution else ""
        )
        record.call_path = getattr(record, "call_path", []) or (
            list(execution.call_path) if execution else []
        )
        record.user_id = getattr(record, "user_id", "") or (execution.user_id if execution else "")
        record.agent_id = getattr(record, "agent_id", "") or (
            execution.agent_id if execution else ""
        )
        record.session_id = getattr(record, "session_id", "") or (
            execution.session_id if execution else ""
        )
        record.task_id = getattr(record, "task_id", "") or (execution.task_id if execution else "")
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
            "taskId": getattr(record, "task_id", ""),
        }
        optional_fields = {
            "stage": "stage",
            "targetAgentId": "target_agent_id",
            "provider": "provider_name",
            "tool": "tool_name",
            "outcome": "outcome",
            "durationMs": "duration_ms",
        }
        for output_name, record_name in optional_fields.items():
            value = getattr(record, record_name, None)
            if value not in {None, ""}:
                payload[output_name] = value
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

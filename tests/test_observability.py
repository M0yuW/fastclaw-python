from __future__ import annotations

import json
import logging

from fastclaw.execution import ExecutionContext, use_execution
from fastclaw.observability import ContextFilter, JsonFormatter, use_correlation_id


def test_json_log_contains_request_and_execution_context() -> None:
    record = logging.LogRecord("fastclaw.test", logging.INFO, "", 0, "started", (), None)
    context = ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="root-1",
        call_path=("agent-1", "agent-2"),
        task_id="task-1",
    )

    with use_correlation_id("request-1"), use_execution(context):
        assert ContextFilter().filter(record)
        payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "started"
    assert payload["correlationId"] == "request-1"
    assert payload["rootExecutionId"] == "root-1"
    assert payload["callPath"] == ["agent-1", "agent-2"]
    assert payload["userId"] == "user-1"
    assert payload["taskId"] == "task-1"


def test_json_log_contains_sanitized_stage_fields() -> None:
    record = logging.LogRecord("fastclaw.test", logging.INFO, "", 0, "stage", (), None)
    record.stage = "tool_finished"
    record.target_agent_id = "agent-2"
    record.provider_name = "deepseek"
    record.tool_name = "football_data"
    record.outcome = "timed_out"
    record.duration_ms = 120_000
    ContextFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["stage"] == "tool_finished"
    assert payload["targetAgentId"] == "agent-2"
    assert payload["provider"] == "deepseek"
    assert payload["tool"] == "football_data"
    assert payload["outcome"] == "timed_out"
    assert payload["durationMs"] == 120_000


def test_json_log_serializes_only_exception_class() -> None:
    try:
        raise RuntimeError("secret SQL and /absolute/path")
    except RuntimeError:
        record = logging.LogRecord(
            "fastclaw.test",
            logging.ERROR,
            "",
            0,
            "operation failed",
            (),
            __import__("sys").exc_info(),
        )
    ContextFilter().filter(record)

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception"] == "RuntimeError"
    assert "secret SQL" not in json.dumps(payload)

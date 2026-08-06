"""Go/Python HTTP and SSE contract comparison without shared persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx


class DifferentialMismatch(AssertionError):
    pass


@dataclass(frozen=True, slots=True)
class DifferentialCase:
    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    stream: bool = False
    comparison: str = "json_shape"
    equal_paths: tuple[str, ...] = ()
    require_terminal_tasks: bool = False
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    status_code: int
    json_body: Any = None
    events: tuple[dict[str, Any], ...] = ()


def json_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: json_shape(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        shapes = {json.dumps(json_shape(item), sort_keys=True) for item in value}
        return [json.loads(item) for item in sorted(shapes)]
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__


def parse_sse(payload: str) -> tuple[dict[str, Any], ...]:
    events: list[dict[str, Any]] = []
    for block in payload.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line[5:].lstrip() for line in block.splitlines() if line.startswith("data:")
        )
        if not data or data == "[DONE]":
            continue
        decoded = json.loads(data)
        if not isinstance(decoded, dict):
            raise DifferentialMismatch("SSE data must be a JSON object")
        events.append(decoded)
    return tuple(events)


def sse_signature(events: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    return tuple(
        (
            event.get("version"),
            event.get("type"),
            tuple(sorted((event.get("data") or {}).keys())),
        )
        for event in events
    )


def compatible_sse_signature(events: tuple[dict[str, Any], ...]) -> tuple[Any, ...]:
    """Compare shared SSE v2 semantics while retaining safe Python extensions."""
    signature: list[Any] = []
    for event in events:
        data = event.get("data") or {}
        if event.get("type") == "content" and not data.get("content"):
            # Locked Go emits an empty content event before each ToolCall.
            continue
        keys = set(data)
        if event.get("type") == "tool_result":
            # Python preserves optional ToolResult metadata consumed by the Web.
            keys.discard("metadata")
        signature.append((event.get("version"), event.get("type"), tuple(sorted(keys))))
    return tuple(signature)


def tool_semantics(events: tuple[dict[str, Any], ...]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            str(event.get("type") or ""),
            str((event.get("data") or {}).get("name") or ""),
            str((event.get("data") or {}).get("result") or "")
            if event.get("type") == "tool_result"
            else "",
        )
        for event in events
        if event.get("type") in {"tool_call", "tool_result"}
    )


def validate_sse(events: tuple[dict[str, Any], ...]) -> None:
    if not events or events[-1].get("type") != "done":
        raise DifferentialMismatch("SSE stream has no terminal done event")
    previous_seq = -1
    pending_tool_calls: set[str] = set()
    for event in events:
        data = event.get("data")
        if not isinstance(data, dict):
            raise DifferentialMismatch("SSE event data must be an object")
        seq = data.get("seq")
        if not isinstance(seq, int) or seq <= previous_seq:
            raise DifferentialMismatch("SSE seq must be strictly increasing")
        previous_seq = seq
        if event.get("type") == "tool_call":
            call_id = str(data.get("id") or "")
            if not call_id or not data.get("name"):
                raise DifferentialMismatch("tool_call has no stable id or name")
            if call_id in pending_tool_calls:
                raise DifferentialMismatch("tool_call id is duplicated")
            pending_tool_calls.add(call_id)
        elif event.get("type") == "tool_result":
            call_id = str(data.get("id") or "")
            if call_id not in pending_tool_calls:
                raise DifferentialMismatch("tool_result has no matching tool_call")
            pending_tool_calls.remove(call_id)
    if pending_tool_calls:
        raise DifferentialMismatch("ToolCall has no matching ToolResult")


def require_terminal_tasks(payload: Any) -> None:
    tasks = payload.get("tasks") if isinstance(payload, dict) else payload
    if not isinstance(tasks, list):
        raise DifferentialMismatch("task response must be an array")
    active = [
        str(item.get("id") or "")
        for item in tasks
        if isinstance(item, dict)
        and item.get("status") not in {"done", "completed", "failed", "cancelled"}
    ]
    if active:
        raise DifferentialMismatch("task response contains non-terminal work")


def json_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise DifferentialMismatch(f"JSON path {path!r} does not exist")
    return current


async def capture(
    client: httpx.AsyncClient,
    case: DifferentialCase,
    *,
    token: str = "",
) -> CapturedResponse:
    headers = dict(case.headers)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    response = await client.request(case.method, case.path, json=case.body, headers=headers)
    if case.stream:
        events = parse_sse(response.text)
        validate_sse(events)
        return CapturedResponse(status_code=response.status_code, events=events)
    if case.comparison == "status" and not case.require_terminal_tasks:
        return CapturedResponse(status_code=response.status_code)
    try:
        body = response.json()
    except json.JSONDecodeError as exc:
        raise DifferentialMismatch(f"{case.name}: response is not JSON") from exc
    return CapturedResponse(status_code=response.status_code, json_body=body)


async def run_case(
    go_client: httpx.AsyncClient,
    python_client: httpx.AsyncClient,
    case: DifferentialCase,
    *,
    go_token: str = "",
    python_token: str = "",
) -> dict[str, Any]:
    if case.stream and case.comparison == "selected":
        raise DifferentialMismatch(f"{case.name}: selected comparison does not support streams")
    go = await capture(go_client, case, token=go_token)
    python = await capture(python_client, case, token=python_token)
    if go.status_code != python.status_code:
        raise DifferentialMismatch(
            f"{case.name}: status differs: Go={go.status_code}, Python={python.status_code}"
        )
    if case.require_terminal_tasks:
        require_terminal_tasks(go.json_body)
        require_terminal_tasks(python.json_body)
    if case.comparison == "status":
        return {
            "name": case.name,
            "status": go.status_code,
            "stream": False,
            "events": 0,
            "contract": "status-only",
        }
    if case.comparison not in {"json_shape", "selected", "sse_compatible"}:
        raise DifferentialMismatch(f"{case.name}: unknown comparison mode")
    if case.comparison == "sse_compatible" and not case.stream:
        raise DifferentialMismatch(f"{case.name}: sse_compatible requires a stream")
    go_contract: Any
    python_contract: Any
    if case.comparison == "selected":
        if not case.equal_paths:
            raise DifferentialMismatch(f"{case.name}: selected comparison has no paths")
        go_contract = {path: json_shape(json_path(go.json_body, path)) for path in case.equal_paths}
        python_contract = {
            path: json_shape(json_path(python.json_body, path)) for path in case.equal_paths
        }
    elif case.stream:
        signature = (
            compatible_sse_signature if case.comparison == "sse_compatible" else sse_signature
        )
        go_contract = signature(go.events)
        python_contract = signature(python.events)
    else:
        go_contract = json_shape(go.json_body)
        python_contract = json_shape(python.json_body)
    if go_contract != python_contract:
        raise DifferentialMismatch(f"{case.name}: response contract differs")
    if case.comparison == "sse_compatible" and tool_semantics(go.events) != tool_semantics(
        python.events
    ):
        raise DifferentialMismatch(f"{case.name}: tool semantics differ")
    for path in case.equal_paths:
        if json_path(go.json_body, path) != json_path(python.json_body, path):
            raise DifferentialMismatch(f"{case.name}: semantic value differs at {path}")
    result = {
        "name": case.name,
        "status": go.status_code,
        "stream": case.stream,
        "events": len(go.events) if case.stream else 0,
        "contract": go_contract,
    }
    if case.stream:
        result["pythonEvents"] = len(python.events)
        result["normalizedEvents"] = len(go_contract)
    return result

#!/usr/bin/env python3
"""Run credentialed business-result smoke against the local Python Gateway.

The command deliberately mutates Session history. It accepts authentication
only through environment variables and emits hashes/counts instead of model or
tool content so credentials and business payloads do not enter the audit log.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx

_ROOT = Path(__file__).parents[1]
_DEFAULT_FIXTURE = _ROOT / "tests" / "fixtures" / "cutover-live-smoke.json"
_GO_DATABASE = (Path.home() / ".fastclaw" / "fastclaw.db").resolve()
_LIVE_ACKNOWLEDGEMENT = "I_ACKNOWLEDGE_THIS_WRITES_CUTOVER_SESSIONS"
_SESSION_ENV = {
    "production": "FASTCLAW_CUTOVER_PRODUCTION_SESSION",
    "benchmark": "FASTCLAW_CUTOVER_BENCHMARK_SESSION",
}


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    principal: str
    agent_id: str
    message: str
    expected_delegations: tuple[str, ...]
    required_tools: Mapping[str, int]


def _load_fixture(path: Path) -> tuple[Scenario, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("scenarios"), list):
        raise RuntimeError("unsupported live smoke fixture")
    scenarios = tuple(
        Scenario(
            name=str(item["name"]),
            principal=str(item["principal"]),
            agent_id=str(item["agentId"]),
            message=str(item["message"]),
            expected_delegations=tuple(str(value) for value in item["expectedDelegations"]),
            required_tools={str(name): int(count) for name, count in item["requiredTools"].items()},
        )
        for item in payload["scenarios"]
    )
    if not scenarios or len({item.name for item in scenarios}) != len(scenarios):
        raise RuntimeError("live smoke scenarios must be non-empty and uniquely named")
    if any(item.principal not in _SESSION_ENV for item in scenarios):
        raise RuntimeError("live smoke fixture contains an unknown principal")
    return scenarios


def _load_sessions(environment: Mapping[str, str]) -> dict[str, str]:
    sessions: dict[str, str] = {}
    missing: list[str] = []
    for principal, name in _SESSION_ENV.items():
        value = environment.get(name, "").strip()
        if value:
            sessions[principal] = value
        else:
            missing.append(name)
    if missing:
        raise RuntimeError("missing cutover Session environment: " + ", ".join(missing))
    if len(set(sessions.values())) != len(sessions):
        raise RuntimeError("production and benchmark cutover Sessions must be distinct")
    return sessions


def _validate_target(base_url: str, database: Path, *, acknowledgement: str) -> Path:
    if acknowledgement != _LIVE_ACKNOWLEDGEMENT:
        raise RuntimeError(
            "refusing to run without the exact --acknowledge-live-cutover value; "
            "this smoke writes persistent Session history"
        )
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("live cutover smoke only targets a credential-free local HTTP URL")
    resolved = database.expanduser().resolve()
    if resolved == _GO_DATABASE or ".fastclaw" in resolved.parts:
        raise RuntimeError("refusing to write smoke Sessions to the Go data directory")
    if not resolved.is_file():
        raise RuntimeError(f"Python cutover database does not exist: {resolved}")
    return resolved


async def _stream_events(
    client: httpx.AsyncClient,
    *,
    cookie: str,
    scenario: Scenario,
    session_id: str,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    async with client.stream(
        "POST",
        "/api/chat/stream",
        cookies={"fastclaw_session": cookie},
        json={
            "agentId": scenario.agent_id,
            "sessionId": session_id,
            "message": scenario.message,
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line.removeprefix("data: "))
            if not isinstance(payload, dict):
                raise RuntimeError(f"{scenario.name}: SSE payload is not an object")
            events.append(payload)
    return events


def _validate_events(scenario: Scenario, events: list[dict[str, Any]]) -> dict[str, Any]:
    types = [str(item.get("type") or "") for item in events]
    if types.count("done") != 1 or "error" in types:
        raise RuntimeError(f"{scenario.name}: stream did not finish successfully exactly once")
    calls = [item for item in events if item.get("type") == "tool_call"]
    results = [item for item in events if item.get("type") == "tool_result"]
    call_ids = [str(item.get("data", {}).get("id") or "") for item in calls]
    result_ids = [str(item.get("data", {}).get("id") or "") for item in results]
    if not all(call_ids) or Counter(call_ids) != Counter(result_ids):
        raise RuntimeError(f"{scenario.name}: ToolCall/ToolResult pairing differs")
    if len(call_ids) != len(set(call_ids)):
        raise RuntimeError(f"{scenario.name}: duplicate ToolCall ID")
    failed = [item for item in results if item.get("data", {}).get("isError") is True]
    if failed:
        raise RuntimeError(f"{scenario.name}: one or more tools reported a structured error")

    tool_counts = Counter(str(item.get("data", {}).get("name") or "") for item in calls)
    if tool_counts != Counter(scenario.required_tools):
        raise RuntimeError(f"{scenario.name}: tool call counts differ from the fixture")
    targets: list[str] = []
    for item in calls:
        data = item.get("data", {})
        if data.get("name") != "spawn_subagent":
            continue
        try:
            arguments = json.loads(str(data.get("arguments") or "{}"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{scenario.name}: malformed delegation arguments") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError(f"{scenario.name}: delegation arguments are not an object")
        targets.append(str(arguments.get("agent_id") or ""))
    if Counter(targets) != Counter(scenario.expected_delegations):
        raise RuntimeError(f"{scenario.name}: delegated Agent set differs from the fixture")

    content = "".join(
        str(item.get("data", {}).get("delta") or "")
        for item in events
        if item.get("type") == "content_delta"
    )
    final_content = next(
        (
            str(item.get("data", {}).get("content") or "")
            for item in reversed(events)
            if item.get("type") == "content"
        ),
        content,
    )
    if not final_content.strip():
        raise RuntimeError(f"{scenario.name}: completed without assistant content")
    return {
        "scenario": scenario.name,
        "toolCalls": dict(sorted(tool_counts.items())),
        "delegatedAgents": sorted(targets),
        "assistantContentBytes": len(final_content.encode()),
        "assistantContentSha256": hashlib.sha256(final_content.encode()).hexdigest(),
        "done": 1,
        "toolErrors": 0,
    }


def _validate_persisted_sessions(
    database_path: Path,
    session_ids: Mapping[str, str],
    scenarios: tuple[Scenario, ...],
) -> dict[str, int]:
    expected = {
        session_ids[item.name]: 1 + len(set(item.expected_delegations)) for item in scenarios
    }
    database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        actual: dict[str, int] = {}
        for session_id, expected_count in expected.items():
            rows = database.execute(
                "select s.user_id, s.agent_id, s.messages, a.user_id "
                "from sessions s join agents a on a.id=s.agent_id where s.key=?",
                (session_id,),
            ).fetchall()
            actual[session_id] = len(rows)
            if len(rows) != expected_count:
                raise RuntimeError("persisted Agent Session count differs from the fixture")
            if any(str(row[0]) != str(row[3]) for row in rows):
                raise RuntimeError("cross-tenant Session detected in live smoke history")
            for _, _, raw_messages, _ in rows:
                messages = (
                    json.loads(raw_messages) if isinstance(raw_messages, str) else raw_messages
                )
                _validate_stored_tool_pairs(messages)
        return actual
    finally:
        database.close()


def _validate_stored_tool_pairs(messages: Any) -> None:
    if not isinstance(messages, list):
        raise RuntimeError("stored Session messages are not an array")
    calls: Counter[str] = Counter()
    results: Counter[str] = Counter()
    for message in messages:
        if not isinstance(message, dict):
            raise RuntimeError("stored Session message is not an object")
        for call in message.get("toolCalls", []):
            call_id = str(call.get("id") or "") if isinstance(call, dict) else ""
            if not call_id:
                raise RuntimeError("stored Session contains an empty ToolCall ID")
            calls[call_id] += 1
        if message.get("role") == "tool":
            call_id = str(message.get("toolCallId") or "")
            if not call_id:
                raise RuntimeError("stored tool message has no ToolCall ID")
            if message.get("metadata", {}).get("isError") is True:
                raise RuntimeError("stored tool message records a structured failure")
            results[call_id] += 1
    if calls != results or any(count != 1 for count in calls.values()):
        raise RuntimeError("stored ToolCall/ToolResult history is not exactly paired")


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    database_path = _validate_target(
        args.base_url,
        Path(args.database),
        acknowledgement=args.acknowledge_live_cutover,
    )
    sessions = _load_sessions(os.environ)
    fixture_path = Path(args.fixture)
    scenarios = _load_fixture(fixture_path)
    fixture_bytes = await anyio.Path(fixture_path).read_bytes()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    session_ids = {item.name: f"cutover-live-{item.name}-{run_id}" for item in scenarios}
    results: list[dict[str, Any]] = []
    timeout = httpx.Timeout(args.timeout, connect=5.0)
    async with httpx.AsyncClient(base_url=args.base_url, timeout=timeout) as client:
        ready = await client.get("/readyz")
        if ready.status_code != 200:
            raise RuntimeError("Python Gateway is not ready for credentialed live smoke")
        identities: dict[str, str] = {}
        for principal, cookie in sessions.items():
            response = await client.get("/api/me", cookies={"fastclaw_session": cookie})
            response.raise_for_status()
            identities[principal] = str(response.json()["user"]["id"])
        if len(set(identities.values())) != len(identities):
            raise RuntimeError("production and benchmark Sessions resolve to the same user")
        for scenario in scenarios:
            events = await _stream_events(
                client,
                cookie=sessions[scenario.principal],
                scenario=scenario,
                session_id=session_ids[scenario.name],
            )
            results.append(_validate_events(scenario, events))
        tasks = await client.get("/api/tasks", cookies={"fastclaw_session": sessions["production"]})
        tasks.raise_for_status()
        active = [
            item
            for item in tasks.json()
            if item.get("status") not in {"completed", "failed", "cancelled"}
        ]
        if active:
            raise RuntimeError("task queue still contains active work after live smoke")
    persisted = _validate_persisted_sessions(database_path, session_ids, scenarios)
    return {
        "runId": run_id,
        "fixtureSha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "scenarios": results,
        "persistedSessionCounts": persisted,
        "activeTasks": 0,
        "crossTenantSessions": 0,
        "secretsRecorded": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18954")
    parser.add_argument("--database", required=True)
    parser.add_argument("--fixture", type=Path, default=_DEFAULT_FIXTURE)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument(
        "--acknowledge-live-cutover",
        default="",
        help=f"must equal {_LIVE_ACKNOWLEDGEMENT!r}",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

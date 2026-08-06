#!/usr/bin/env python3
"""Verify migrated coordinator, SSE, persistence and cancellation wiring."""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_LOCKED_LIVE_DATABASES = {
    (Path.home() / ".fastclaw" / "fastclaw.db").resolve(),
    (Path.home() / ".fastclaw-python" / "fastclaw.db").resolve(),
}


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    session_cookie: str
    agent_id: str
    session_key: str
    expected_tool_calls: int
    expected_sessions: int


def _events(response: httpx.Response) -> list[dict[str, Any]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    database_path = _validate_disposable_database(
        Path(args.database), acknowledge=args.acknowledge_disposable_copy
    )
    scenarios = (
        Scenario(
            "finance-production",
            args.production_session,
            "agt_0b741037d5175a6c0c2a",
            "cutover-wiring-finance-production",
            2,
            3,
        ),
        Scenario(
            "worldcup-production",
            args.production_session,
            "agt_0810c26790ce1c32bb11",
            "cutover-wiring-worldcup-production",
            7,
            7,
        ),
        Scenario(
            "finance-benchmark",
            args.benchmark_session,
            "finance-coordinator",
            "cutover-wiring-finance-benchmark",
            5,
            6,
        ),
        Scenario(
            "runtime-benchmark",
            args.benchmark_session,
            "bench-coordinator",
            "cutover-wiring-runtime-benchmark",
            4,
            5,
        ),
    )
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=args.base_url, timeout=30) as client:
        for scenario in scenarios:
            response = await client.post(
                "/api/chat/stream",
                cookies={"fastclaw_session": scenario.session_cookie},
                json={
                    "agentId": scenario.agent_id,
                    "sessionId": scenario.session_key,
                    "message": f"fixed wiring fixture: {scenario.name}",
                },
            )
            response.raise_for_status()
            events = _events(response)
            types = [str(event["type"]) for event in events]
            if "error" in types or types.count("done") != 1:
                raise RuntimeError(f"{scenario.name}: stream did not complete exactly once")
            if types.count("tool_call") != scenario.expected_tool_calls:
                raise RuntimeError(f"{scenario.name}: unexpected ToolCall count")
            if types.count("tool_result") != scenario.expected_tool_calls:
                raise RuntimeError(f"{scenario.name}: unexpected ToolResult count")
            calls = {str(event["data"]["id"]) for event in events if event["type"] == "tool_call"}
            tool_results = {
                str(event["data"]["id"]) for event in events if event["type"] == "tool_result"
            }
            if calls != tool_results:
                raise RuntimeError(f"{scenario.name}: ToolCall/ToolResult IDs differ")
            results.append(
                {
                    "scenario": scenario.name,
                    "toolCalls": scenario.expected_tool_calls,
                    "done": 1,
                    "pairedToolCalls": True,
                }
            )

        await _verify_abort(client, args.production_session)
        tasks = await client.get(
            "/api/tasks",
            cookies={"fastclaw_session": args.production_session},
        )
        tasks.raise_for_status()
        active = [
            item
            for item in tasks.json()
            if item["status"] not in {"completed", "failed", "cancelled"}
        ]
        if active:
            raise RuntimeError("task queue still contains active work")

    session_counts, cross_tenant, cancelled_persisted = _database_checks(database_path, scenarios)
    return {
        "scenarios": results,
        "sessionCounts": session_counts,
        "activeTasks": 0,
        "crossTenantSessions": cross_tenant,
        "partialAssistantPersistedAfterAbort": bool(cancelled_persisted),
    }


async def _verify_abort(client: httpx.AsyncClient, session_cookie: str) -> None:
    async with client.stream(
        "POST",
        "/api/chat/stream",
        cookies={"fastclaw_session": session_cookie},
        json={
            "agentId": "agt_095e21e187b9dde2b2f8",
            "sessionId": "cutover-wiring-cancel",
            "message": "cancel-wiring-smoke",
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if '"type":"content_delta"' in line.replace(" ", ""):
                break
        else:
            raise RuntimeError("abort fixture returned no content delta")
    await asyncio.sleep(0.5)


def _database_checks(
    path: Path, scenarios: tuple[Scenario, ...]
) -> tuple[dict[str, int], int, int]:
    database = sqlite3.connect(path)
    try:
        counts = {
            scenario.session_key: int(
                database.execute(
                    "select count(*) from sessions where key = ?", (scenario.session_key,)
                ).fetchone()[0]
            )
            for scenario in scenarios
        }
        expected = {scenario.session_key: scenario.expected_sessions for scenario in scenarios}
        if counts != expected:
            raise RuntimeError(f"persisted Agent Session counts differ: {counts}")
        cross_tenant = int(
            database.execute(
                "select count(*) from sessions s join agents a on a.id=s.agent_id "
                "where s.key like 'cutover-wiring-%' and s.user_id<>a.user_id"
            ).fetchone()[0]
        )
        cancelled = int(
            database.execute(
                "select count(*) from sessions where key='cutover-wiring-cancel'"
            ).fetchone()[0]
        )
    finally:
        database.close()
    if cross_tenant or cancelled:
        raise RuntimeError("tenant or cancellation persistence invariant failed")
    return counts, cross_tenant, cancelled


def _validate_disposable_database(path: Path, *, acknowledge: bool) -> Path:
    resolved = path.expanduser().resolve()
    if not acknowledge:
        raise RuntimeError(
            "refusing to run without --acknowledge-disposable-copy; the smoke mutates "
            "sessions and must only target an isolated database copy"
        )
    if resolved in _LOCKED_LIVE_DATABASES:
        raise RuntimeError("refusing to run against a locked Go or Python live database")
    if not resolved.is_file():
        raise RuntimeError(f"disposable database does not exist: {resolved}")
    return resolved


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:18955")
    parser.add_argument("--database", required=True)
    parser.add_argument("--production-session", required=True)
    parser.add_argument("--benchmark-session", required=True)
    parser.add_argument(
        "--acknowledge-disposable-copy",
        action="store_true",
        help="confirm that --database is an isolated copy that may be mutated",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args)), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

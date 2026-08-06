from __future__ import annotations

import json

import httpx
import pytest

from fastclaw.differential import (
    DifferentialCase,
    DifferentialMismatch,
    parse_sse,
    require_terminal_tasks,
    run_case,
    validate_sse,
)


def sse(events: list[dict[str, object]]) -> str:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events)


async def test_differential_compares_json_shape_without_dynamic_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        identifier = "go-id" if request.url.host == "go.test" else "python-id"
        return httpx.Response(200, json={"agents": [{"id": identifier, "name": "Agent"}]})

    transport = httpx.MockTransport(handler)
    async with (
        httpx.AsyncClient(base_url="https://go.test", transport=transport) as go,
        httpx.AsyncClient(base_url="https://python.test", transport=transport) as python,
    ):
        result = await run_case(go, python, DifferentialCase("agents", "GET", "/v1/agents"))

    assert result["status"] == 200


async def test_differential_can_compare_status_for_legacy_plain_text_probe() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text="ok" if request.url.host == "go.test" else '{"status":"ok"}',
        )
    )
    async with (
        httpx.AsyncClient(base_url="https://go.test", transport=transport) as go,
        httpx.AsyncClient(base_url="https://python.test", transport=transport) as python,
    ):
        result = await run_case(
            go,
            python,
            DifferentialCase("health", "GET", "/healthz", comparison="status"),
        )
    assert result["contract"] == "status-only"


async def test_differential_validates_sse_order_and_tool_pairing() -> None:
    events = [
        {
            "version": 2,
            "type": "tool_call",
            "data": {
                "turnId": "t",
                "messageId": "m",
                "round": 0,
                "seq": 0,
                "id": "c",
                "name": "echo",
            },
        },
        {
            "version": 2,
            "type": "tool_result",
            "data": {
                "turnId": "t",
                "messageId": "m",
                "round": 0,
                "seq": 1,
                "id": "c",
                "name": "echo",
                "result": "ok",
            },
        },
        {
            "version": 2,
            "type": "done",
            "data": {"turnId": "t", "messageId": "m", "round": 0, "seq": 2},
        },
    ]
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            text=sse(events),
            headers={"content-type": "text/event-stream"},
        )
    )
    async with (
        httpx.AsyncClient(base_url="https://go.test", transport=transport) as go,
        httpx.AsyncClient(base_url="https://python.test", transport=transport) as python,
    ):
        result = await run_case(
            go,
            python,
            DifferentialCase("chat", "POST", "/api/chat/stream", stream=True),
        )
    assert result["events"] == 3


def test_differential_rejects_missing_tool_result_and_contract_drift() -> None:
    incomplete = parse_sse(
        sse(
            [
                {
                    "version": 2,
                    "type": "tool_call",
                    "data": {"seq": 0, "id": "call", "name": "echo"},
                },
                {"version": 2, "type": "done", "data": {"seq": 1}},
            ]
        )
    )
    with pytest.raises(DifferentialMismatch, match="no matching ToolResult"):
        validate_sse(incomplete)

    with pytest.raises(DifferentialMismatch, match="non-terminal"):
        require_terminal_tasks([{"id": "task-1", "status": "running"}])

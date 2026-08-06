from __future__ import annotations

import runpy
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import pytest

SCRIPT_GLOBALS = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "cutover_live_smoke.py")
)
load_fixture = cast(Callable[[Path], tuple[Any, ...]], SCRIPT_GLOBALS["_load_fixture"])
load_sessions = cast(
    Callable[[Mapping[str, str]], dict[str, str]], SCRIPT_GLOBALS["_load_sessions"]
)
validate_events = cast(
    Callable[[Any, list[dict[str, Any]]], dict[str, Any]],
    SCRIPT_GLOBALS["_validate_events"],
)
validate_stored_tool_pairs = cast(
    Callable[[Any], None], SCRIPT_GLOBALS["_validate_stored_tool_pairs"]
)
validate_target = cast(Callable[..., Path], SCRIPT_GLOBALS["_validate_target"])
ACK = cast(str, SCRIPT_GLOBALS["_LIVE_ACKNOWLEDGEMENT"])
FIXTURE = Path(__file__).parent / "fixtures" / "cutover-live-smoke.json"


def test_live_fixture_locks_all_four_coordinators() -> None:
    scenarios = load_fixture(FIXTURE)

    assert [item.name for item in scenarios] == [
        "finance-production",
        "worldcup-production",
        "finance-benchmark",
        "runtime-benchmark",
    ]
    assert sum(len(item.expected_delegations) for item in scenarios) == 17
    assert all("CUTOVER FIXTURE" in item.message for item in scenarios)


def test_live_smoke_authentication_is_environment_only() -> None:
    with pytest.raises(RuntimeError, match="FASTCLAW_CUTOVER_PRODUCTION_SESSION"):
        load_sessions({})
    with pytest.raises(RuntimeError, match="must be distinct"):
        load_sessions(
            {
                "FASTCLAW_CUTOVER_PRODUCTION_SESSION": "same",
                "FASTCLAW_CUTOVER_BENCHMARK_SESSION": "same",
            }
        )
    assert load_sessions(
        {
            "FASTCLAW_CUTOVER_PRODUCTION_SESSION": "production-secret",
            "FASTCLAW_CUTOVER_BENCHMARK_SESSION": "benchmark-secret",
        }
    ) == {"production": "production-secret", "benchmark": "benchmark-secret"}


def test_live_smoke_requires_exact_ack_and_local_python_target(tmp_path: Path) -> None:
    database = tmp_path / "python.db"
    database.touch()

    with pytest.raises(RuntimeError, match="exact --acknowledge-live-cutover"):
        validate_target("http://127.0.0.1:18954", database, acknowledgement="")
    with pytest.raises(RuntimeError, match="local HTTP"):
        validate_target("https://example.test", database, acknowledgement=ACK)
    assert (
        validate_target("http://localhost:18954", database, acknowledgement=ACK)
        == database.resolve()
    )


def test_live_smoke_rejects_structured_tool_errors() -> None:
    scenario = load_fixture(FIXTURE)[0]
    calls = [
        {
            "type": "tool_call",
            "data": {
                "id": f"call-{index}",
                "name": "spawn_subagent",
                "arguments": '{"agent_id":"' + target + '","task":"fixture"}',
            },
        }
        for index, target in enumerate(scenario.expected_delegations)
    ]
    results = [
        {
            "type": "tool_result",
            "data": {"id": f"call-{index}", "name": "spawn_subagent", "isError": index == 0},
        }
        for index, _ in enumerate(scenario.expected_delegations)
    ]
    events = [
        *calls,
        *results,
        {"type": "content", "data": {"content": "answer"}},
        {"type": "done", "data": {}},
    ]

    with pytest.raises(RuntimeError, match="structured error"):
        validate_events(scenario, events)


def test_live_smoke_rejects_failed_or_unpaired_stored_tools() -> None:
    valid = [
        {"role": "assistant", "toolCalls": [{"id": "call-1"}]},
        {"role": "tool", "toolCallId": "call-1", "metadata": {}},
    ]
    validate_stored_tool_pairs(valid)

    with pytest.raises(RuntimeError, match="structured failure"):
        validate_stored_tool_pairs(
            [
                valid[0],
                {"role": "tool", "toolCallId": "call-1", "metadata": {"isError": True}},
            ]
        )
    with pytest.raises(RuntimeError, match="not exactly paired"):
        validate_stored_tool_pairs([valid[0]])

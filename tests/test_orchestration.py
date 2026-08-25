from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from fastclaw.execution import ExecutionContext, SharedExecutionState
from fastclaw.orchestration import (
    AsyncTaskQueue,
    BackpressureError,
    CrossTenantError,
    DelegationCycleError,
    DelegationErrorCode,
    DelegationRequest,
    InProcessMessageBus,
    QueueShutdownError,
    SpawnSubagentTool,
    TaskResult,
    WaitTicket,
)
from fastclaw.tools import ToolResult
from fastclaw.tools.football_context import FootballContextTool
from fastclaw.tools.football_shared import (
    FootballEvidenceCache,
    football_event_key,
    football_fixture_state_key,
)
from fastclaw.tools.registry import ToolRegistry


def context(
    *, root: str = "root-1", agent_id: str = "gateway", path: tuple[str, ...] = ()
) -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id=agent_id,
        session_id="session-1",
        root_execution_id=root,
        call_path=path,
    )


def _fixture_content(
    home: str,
    away: str,
    *,
    status: str = "confirmed",
    competition: str = "Spanish La Liga",
    requested_date: str = "2026-08-22",
    source_date: str | None = None,
) -> str:
    return json.dumps(
        {
            "status": status,
            "fixture": {
                "competition": competition,
                "season": "2026-2027",
                "requested_date": requested_date,
                "date": source_date or requested_date,
                "home": home,
                "away": away,
            },
            "evidence_schema_version": 4,
            "historical_context": {},
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_football_context_accepts_requested_and_source_dates_for_one_fixture() -> None:
    state = SharedExecutionState()
    requested_date = "2026-08-23"
    source_date = "2026-08-22"
    key = football_fixture_state_key(
        "意甲", "2026-27", requested_date, "国际米兰", "蒙扎"
    )
    content = _fixture_content(
        "Inter Milan",
        "Monza",
        competition="Italian Serie A",
        requested_date=requested_date,
        source_date=source_date,
    )
    state.publish_football_observation(key, "confirmed", content)
    tool = FootballContextTool(cache=FootballEvidenceCache())

    for lookup_date in (requested_date, source_date):
        result = await tool.execute(
            {
                "competition": "意甲",
                "season": "2026-2027",
                "date": lookup_date,
                "team_a": "Inter Milan",
                "team_b": "Monza",
            },
            replace(context(), shared_state=state),
        )
        assert not result.is_error
        assert json.loads(result.content)["fixture"]["date"] == source_date


@pytest.mark.asyncio
async def test_football_context_cache_accepts_source_date_alias_after_restore() -> None:
    cache = FootballEvidenceCache()
    requested_date = "2026-08-23"
    source_date = "2026-08-22"
    key = football_event_key(
        "意甲", "2026-27", requested_date, "Inter Milan", "Monza"
    )
    content = _fixture_content(
        "Inter Milan",
        "Monza",
        competition="Italian Serie A",
        requested_date=requested_date,
        source_date=source_date,
    )
    await cache.put("base", key, ToolResult(content=content))

    result = await FootballContextTool(cache=cache).execute(
        {
            "competition": "意甲",
            "season": "2026-2027",
            "date": source_date,
            "team_a": "Inter Milan",
            "team_b": "Monza",
        },
        context(),
    )
    assert not result.is_error
    assert result.metadata["cacheHit"] is True
    assert json.loads(result.content)["fixture"]["requested_date"] == requested_date


@pytest.mark.asyncio
async def test_data_delegation_failure_keeps_confirmed_evidence_and_is_not_generic_tool_failure(
) -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task
        key = football_fixture_state_key(
            "意甲", "2026-27", "2026-08-23", "国际米兰", "蒙扎"
        )
        child.shared_state.publish_football_observation(
            key,
            "confirmed",
            _fixture_content(
                "Inter Milan",
                "Monza",
                competition="Italian Serie A",
                requested_date="2026-08-23",
                source_date="2026-08-22",
            ),
        )
        raise RuntimeError("provider connection dropped")

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "reconfirm the fixture"}, context()
        )
        payload = json.loads(result.content)
        assert not result.is_error
        assert payload["status"] == "confirmed"
        assert len(payload["confirmed"]) == 1
        assert "data delegation failed" in payload["child_report"]
        assert "spawn_subagent" not in payload["child_report"]
        assert result.metadata["degraded"] is True
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_data_failure_does_not_reuse_hydrated_fixture_from_an_older_request() -> None:
    """A continued session must not report an old fixture for a new failed task."""

    bus = InProcessMessageBus()
    cache = FootballEvidenceCache()
    stale_key = football_event_key(
        "荷甲", "2026-27", "2026-08-22", "福图纳锡塔德", "阿尔克马尔"
    )
    await cache.put_session_base(
        "session-1",
        stale_key,
        ToolResult(
            content=_fixture_content(
                "Fortuna Sittard",
                "AZ Alkmaar",
                competition="Dutch Eredivisie",
            )
        ),
    )

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task, child
        raise RuntimeError("provider timed out before publishing the new fixture")

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=cache)
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "confirm the current Ligue 1 fixtures"},
            context(),
        )

        assert result.is_error
        assert result.metadata["errorCode"] == "football_data_delegation_failed"
        assert "Fortuna Sittard" not in result.content
        assert "AZ Alkmaar" not in result.content
        assert '"status":"unavailable"' in result.content
    finally:
        await bus.shutdown()


def test_fixture_state_key_unifies_reviewed_team_aliases_and_keeps_identity_boundaries() -> None:
    athletic_club = football_fixture_state_key(
        "西甲",
        "2026-27",
        "2026-08-22",
        "Athletic Club",
        "Sevilla",
    )
    athletic_bilbao = football_fixture_state_key(
        "Spanish La Liga",
        "2026-2027",
        "2026-08-22",
        "Athletic Bilbao",
        "Sevilla FC",
    )
    assert athletic_club == athletic_bilbao
    assert "2026-08-22" in athletic_club

    assert football_fixture_state_key(
        "法甲", "2026-27", "2026-08-22", "RC Lens", "AJ Auxerre"
    ) == football_fixture_state_key(
        "Ligue 1", "2026-2027", "2026-08-22", "Lens", "Auxerre"
    )
    assert football_fixture_state_key(
        "法甲", "2026-27", "2026-08-23", "Lens", "Auxerre"
    ) != football_fixture_state_key(
        "法甲", "2026-27", "2026-08-22", "Auxerre", "Lens"
    )


def test_fixture_evidence_state_confirmed_cannot_be_downgraded_and_keeps_audit() -> None:
    state = SharedExecutionState()
    key = football_fixture_state_key(
        "西甲", "2026-27", "2026-08-22", "Athletic Club", "Sevilla"
    )

    state.publish_football_observation(
        key,
        "no_match",
        _fixture_content("Athletic Club", "Sevilla", status="no_match"),
    )
    state.publish_football_observation(
        key,
        "confirmed",
        _fixture_content("Athletic Bilbao", "Sevilla"),
    )
    state.publish_football_observation(
        key,
        "unavailable",
        json.dumps({"status": "unavailable", "fixture": None}),
    )

    current = state.football_fixture_state(key)
    assert current is not None
    assert current.status == "confirmed"
    assert current.content is not None
    assert len(current.audit) == 3
    assert state.football_delegation_summary()["status"] == "confirmed"


def test_fixture_delegation_summary_is_partial_for_two_confirmed_and_one_no_match() -> None:
    state = SharedExecutionState()
    fixtures = (
        ("Fortuna Sittard", "AZ Alkmaar", "confirmed"),
        ("Athletic Bilbao", "Sevilla", "confirmed"),
        ("Lens", "Auxerre", "no_match"),
    )
    for home, away, status in fixtures:
        key = football_fixture_state_key("测试联赛", "2026-27", "2026-08-22", home, away)
        state.publish_football_observation(
            key,
            status,
            _fixture_content(home, away, status=status),
        )

    summary = state.football_delegation_summary()
    assert summary["status"] == "partial"
    assert len(summary["confirmed"]) == 2
    assert len(summary["unresolved"]) == 1
    assert summary["next_action"] == "delegate_specialists"


@pytest.mark.asyncio
async def test_data_delegation_ignores_historical_negative_after_alias_retry_and_allows_specialists(
) -> None:
    bus = InProcessMessageBus()
    specialist_called: list[str] = []

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task
        key = football_fixture_state_key(
            "西甲", "2026-27", "2026-08-22", "Athletic Club", "Sevilla"
        )
        child.shared_state.publish_football_observation(
            key, "no_match", _fixture_content("Athletic Club", "Sevilla", status="no_match")
        )
        child.shared_state.publish_football_observation(
            key, "confirmed", _fixture_content("Athletic Bilbao", "Sevilla")
        )
        return "three fixtures confirmed; one had an alias retry"

    async def specialist_handler(task: str, child: ExecutionContext) -> str:
        del child
        specialist_called.append(task)
        return "specialist complete"

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    bus.register(user_id="user-1", agent_id="odds", handler=specialist_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        results = await tool.execute_many(
            (
                {"agent_id": "data", "task": "confirm fixtures"},
                {"agent_id": "odds", "task": "analyze confirmed odds"},
            ),
            context(),
        )
        payload = json.loads(results[0].content)
        assert not results[0].is_error
        assert payload["status"] == "confirmed"
        assert not results[0].metadata.get("status") == "fixture_unconfirmed"
        assert specialist_called == ["analyze confirmed odds"]
        assert results[1].content == "specialist complete"
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_three_fixture_alias_retry_scenario_reaches_specialist_phase() -> None:
    """Reproduce session s-1787319787602-vymppy without network or ledger writes."""

    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=5))
    specialist_calls: list[str] = []

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task
        fixtures = (
            ("荷甲", "2026-27", "2026-08-22", "福图纳锡塔德", "阿尔克马尔", False),
            ("西甲", "2026-27", "2026-08-22", "Athletic Club", "塞维利亚", True),
            ("法甲", "2026-27", "2026-08-22", "RC Lens", "AJ Auxerre", True),
        )
        for competition, season, date, home, away, retried in fixtures:
            first_key = football_fixture_state_key(competition, season, date, home, away)
            if retried:
                child.shared_state.publish_football_observation(
                    first_key,
                    "no_match",
                    _fixture_content(home, away, status="no_match"),
                )
                if home == "Athletic Club":
                    home, away = "Athletic Bilbao", "塞维利亚"
                else:
                    home, away = "Lens", "Auxerre"
            confirmed_key = football_fixture_state_key(
                competition, season, date, home, away
            )
            child.shared_state.publish_football_observation(
                confirmed_key,
                "confirmed",
                _fixture_content(home, away),
            )
        return "all three base evidence bundles confirmed"

    async def specialist_handler(task: str, child: ExecutionContext) -> str:
        assert child.shared_state.football_delegation_summary()["status"] == "confirmed"
        specialist_calls.append(task)
        return task

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    for agent_id in ("tactics", "odds", "history", "risk"):
        bus.register(user_id="user-1", agent_id=agent_id, handler=specialist_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        results = await tool.execute_many(
            (
                {"agent_id": "data", "task": "confirm the three fixtures"},
                {"agent_id": "tactics", "task": "tactics"},
                {"agent_id": "odds", "task": "odds"},
                {"agent_id": "history", "task": "history"},
                {"agent_id": "risk", "task": "risk"},
            ),
            context(),
        )
        payload = json.loads(results[0].content)
        assert payload["status"] == "confirmed"
        assert len(payload["confirmed"]) == 3
        assert payload["unresolved"] == []
        assert specialist_calls == ["tactics", "odds", "history", "risk"]
        assert all(not result.is_error for result in results)
    finally:
        await bus.shutdown()


def test_spawn_subagent_schema_exposes_only_delegation_arguments() -> None:
    tool = SpawnSubagentTool(InProcessMessageBus())

    properties = tool.definition.function.parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"agent_id", "task"}


@pytest.mark.asyncio
async def test_spawn_subagent_restricts_runtime_targets_and_schema() -> None:
    bus = InProcessMessageBus()

    async def handler(task: str, child: ExecutionContext) -> str:
        del child
        return task

    bus.register(user_id="user-1", agent_id="allowed", handler=handler)
    bus.register(user_id="user-1", agent_id="blocked", handler=handler)
    tool = SpawnSubagentTool(bus, ("allowed",))
    try:
        properties = tool.definition.function.parameters["properties"]
        assert isinstance(properties, dict)
        agent_id = properties["agent_id"]
        assert isinstance(agent_id, dict)
        assert agent_id["enum"] == ["allowed"]

        rejected = await tool.execute({"agent_id": "blocked", "task": "do not run"}, context())
        assert rejected.is_error
        assert rejected.content == "delegation target is not allowed"

        accepted = await tool.execute({"agent_id": "allowed", "task": "run"}, context())
        assert accepted.content == "run"
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_spawn_subagent_ignores_model_supplied_identity() -> None:
    bus = InProcessMessageBus()
    received: list[ExecutionContext] = []

    async def handler(task: str, child: ExecutionContext) -> str:
        assert task == "delegated task"
        received.append(child)
        return "complete"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    tool = SpawnSubagentTool(bus)
    try:
        result = await tool.execute(
            {
                "agent_id": "worker",
                "task": "delegated task",
                "user_id": "attacker",
                "userId": "attacker",
                "root_execution_id": "attacker-root",
                "rootExecutionId": "attacker-root",
                "call_path": ["attacker"],
                "callPath": ["attacker"],
            },
            context(),
        )

        assert result.content == "complete"
        assert len(received) == 1
        assert received[0].user_id == "user-1"
        assert received[0].root_execution_id == "root-1"
        assert received[0].call_path == ("worker",)
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_data_delegation_requires_published_base_evidence() -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task, child
        return "prose report only"

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "confirm the fixture"},
            context(),
        )

        assert result.is_error
        assert result.metadata["errorCode"] == "football_base_not_published"
        assert "action=base_evidence" in result.content
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_data_delegation_returns_trusted_unconfirmed_fixture_evidence() -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task
        child.shared_state.publish_football_negative(
            json.dumps(
                {
                    "status": "no_match",
                    "fixture": None,
                    "requested": {
                        "competition": "UEFA Champions League",
                        "season": "2026-2027",
                        "date": None,
                        "home": "Levski Sofia",
                        "away": "AEK Athens",
                    },
                }
            )
        )
        return "The reviewed season schedule did not contain this fixture."

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "confirm the fixture"},
            context(),
        )

        payload = json.loads(result.content)
        assert not result.is_error
        assert result.metadata["status"] == "fixture_unconfirmed"
        assert payload["status"] == "no_match"
        assert payload["evidence"][0]["requested"]["home"] == "Levski Sofia"
        assert "Stop prediction analysis" in payload["instruction"]
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_settlement_data_delegation_bypasses_upcoming_fixture_gate() -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del child
        assert "赛果" in task
        return '{"status":"success","rows":[]}'

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    execution = context()
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "复盘账本并核对已结束比赛赛果"},
            execution,
        )

        assert not result.is_error
        assert execution.shared_state.football_settlement_attempted is True
        assert execution.shared_state.football_settlement_review is True
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_settlement_delegation_preserves_verified_results_when_child_report_fails() -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        del task
        child.shared_state.football_settlement_results.append(
            json.dumps(
                {
                    "action": "results",
                    "competition": "Dutch Eredivisie",
                    "rows": [
                        {
                            "match": "Willem II vs NEC Nijmegen",
                            "home": "Willem II",
                            "away": "NEC Nijmegen",
                            "date": "2026-08-15",
                            "status": "FT",
                            "home_score": "1",
                            "away_score": "4",
                        }
                    ],
                }
            )
        )
        return "child later hit an unrelated provider error"

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        result = await tool.execute(
            {"agent_id": "data", "task": "复盘账本并核对已结束比赛赛果"},
            context(),
        )

        payload = json.loads(result.content)
        assert not result.is_error
        assert payload["status"] == "verified"
        assert payload["results"][0]["rows"][0]["status"] == "FT"
        assert payload["verified_matches"][0]["actual_result"] == "客胜"
        assert payload["verified_matches"][0]["actual_score"] == "1-4"
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_settlement_batch_returns_only_each_delegation_result_delta() -> None:
    bus = InProcessMessageBus()

    async def data_handler(task: str, child: ExecutionContext) -> str:
        match = "Alpha vs Beta" if "Alpha" in task else "Gamma vs Delta"
        home, away = match.split(" vs ")
        child.shared_state.football_settlement_results.append(
            json.dumps(
                {
                    "source": "fixture:results",
                    "rows": [
                        {
                            "event_id": match,
                            "competition": "Fixture League",
                            "season": "2026",
                            "date": "2026-08-20",
                            "match": match,
                            "home": home,
                            "away": away,
                            "status": "FT",
                            "home_score": "1",
                            "away_score": "0",
                        }
                    ],
                }
            )
        )
        return f"verified {match}"

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=FootballEvidenceCache())
    try:
        results = await tool.execute_many(
            (
                {"agent_id": "data", "task": "复盘账本赛果 Alpha vs Beta"},
                {"agent_id": "data", "task": "复盘账本赛果 Gamma vs Delta"},
            ),
            context(),
        )

        first = json.loads(results[0].content)
        second = json.loads(results[1].content)
        assert [row["match"] for row in first["verified_matches"]] == ["Alpha vs Beta"]
        assert [row["match"] for row in second["verified_matches"]] == ["Gamma vs Delta"]
        assert results[0].metadata["verifiedResultGroups"] == 1
        assert results[1].metadata["verifiedResultGroups"] == 1
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_continued_session_rehydrates_base_before_specialist_gate() -> None:
    bus = InProcessMessageBus()
    cache = FootballEvidenceCache()

    async def specialist_handler(task: str, child: ExecutionContext) -> str:
        assert task == "fetch market odds"
        assert child.shared_state.has_football_base()
        return "odds attempted"

    bus.register(user_id="user-1", agent_id="odds", handler=specialist_handler)
    await cache.put_session_base(
        "session-1",
        football_event_key(
            "Spanish La Liga",
            "2026-2027",
            "2026-08-15",
            "Deportivo Alavés",
            "Getafe",
        ),
        ToolResult(
            content=(
                    '{"evidence_schema_version":4,"fixture":'
                '{"competition":"Spanish La Liga"},"historical_context":{}}'
            ),
        ),
    )
    tool = SpawnSubagentTool(bus, data_agent_id="data", cache=cache)
    try:
        result = await tool.execute(
            {"agent_id": "odds", "task": "fetch market odds"},
            context(),
        )

        assert result.content == "odds attempted"
        assert not result.is_error
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_delegation_emits_sanitized_stage_lifecycle(caplog: pytest.LogCaptureFixture) -> None:
    bus = InProcessMessageBus()
    received: list[ExecutionContext] = []

    async def handler(task: str, child: ExecutionContext) -> str:
        assert task == "private task body"
        received.append(child)
        return "complete"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    try:
        with caplog.at_level(logging.INFO, logger="fastclaw.orchestration.bus"):
            result = await bus.request(context(), "worker", "private task body")

        stage_records = [record for record in caplog.records if hasattr(record, "stage")]
        stages = [record.stage for record in stage_records]
        assert stages == ["queued", "handler_started", "reply_published", "completed"]
        assert received[0].task_id == result.correlation_id
        assert all("private task body" not in record.getMessage() for record in caplog.records)
        assert all(
            getattr(record, "task_id", "") == result.correlation_id for record in stage_records
        )
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_spawn_subagent_batch_runs_different_targets_in_parallel_and_keeps_order() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=2))
    both_started = asyncio.Event()
    release = asyncio.Event()
    started: set[str] = set()

    async def handler(task: str, child: ExecutionContext) -> str:
        del child
        started.add(task)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        return task.upper()

    bus.register(user_id="user-1", agent_id="left", handler=handler)
    bus.register(user_id="user-1", agent_id="right", handler=handler)
    tool = SpawnSubagentTool(bus)
    pending = asyncio.create_task(
        tool.execute_many(
            (
                {"agent_id": "left", "task": "first"},
                {"agent_id": "right", "task": "second"},
            ),
            context(),
        )
    )
    try:
        await asyncio.wait_for(both_started.wait(), timeout=1)
        release.set()
        results = await pending

        assert [result.content for result in results] == ["FIRST", "SECOND"]
        assert all(not result.is_error for result in results)
    finally:
        release.set()
        if not pending.done():
            pending.cancel()
        await bus.shutdown()


@pytest.mark.asyncio
async def test_spawn_subagent_batch_timeout_isolated_per_child() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=2))

    async def handler(task: str, child: ExecutionContext) -> str:
        del child
        if task == "slow":
            await asyncio.sleep(1)
        return task.upper()

    bus.register(user_id="user-1", agent_id="fast", handler=handler)
    bus.register(user_id="user-1", agent_id="slow", handler=handler)
    registry = ToolRegistry((SpawnSubagentTool(bus),))
    try:
        results = await registry.execute_batch(
            "spawn_subagent",
            (
                {"agent_id": "fast", "task": "fast"},
                {"agent_id": "slow", "task": "slow"},
            ),
            context(),
            timeout_seconds=0.05,
        )

        assert results[0].content == "FAST"
        assert not results[0].is_error
        assert results[1].is_error
        assert results[1].metadata["errorCode"] == DelegationErrorCode.TIMEOUT
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_spawn_subagent_batch_serializes_multiple_data_tasks_before_specialists() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=4))
    order: list[str] = []

    async def data_handler(task: str, child: ExecutionContext) -> str:
        child.shared_state.publish_football_base(task, task)
        order.append(task)
        return task

    async def specialist_handler(task: str, child: ExecutionContext) -> str:
        assert child.shared_state.has_football_base()
        order.append(task)
        return task

    bus.register(user_id="user-1", agent_id="data", handler=data_handler)
    bus.register(user_id="user-1", agent_id="tactics", handler=specialist_handler)
    bus.register(user_id="user-1", agent_id="history", handler=specialist_handler)
    tool = SpawnSubagentTool(
        bus,
        data_agent_id="data",
        cache=FootballEvidenceCache(),
    )
    try:
        results = await tool.execute_many(
            (
                {"agent_id": "data", "task": "group-a"},
                {"agent_id": "data", "task": "group-b"},
                {"agent_id": "tactics", "task": "tactics"},
                {"agent_id": "history", "task": "history"},
            ),
            context(),
        )

        assert [result.content for result in results] == [
            "group-a",
            "group-b",
            "tactics",
            "history",
        ]
        assert order[:2] == ["group-a", "group-b"]
        assert set(order[2:]) == {"tactics", "history"}
        assert all(not result.is_error for result in results)
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_spawn_subagent_batch_isolates_and_sanitizes_item_errors() -> None:
    bus = InProcessMessageBus()

    async def failure(task: str, child: ExecutionContext) -> str:
        del task, child
        raise RuntimeError("database /private/secret.db on internal.example failed")

    bus.register(user_id="user-1", agent_id="failure", handler=failure)
    tool = SpawnSubagentTool(bus)
    try:
        results = await tool.execute_many(
            (
                {"agent_id": "failure", "task": "first"},
                {"agent_id": "missing", "task": "second"},
                {"agent_id": "", "task": "third"},
            ),
            context(),
        )

        assert [result.is_error for result in results] == [True, True, True]
        assert "handler_error: delegated task failed" in results[0].content
        assert "unknown_agent: target agent is unavailable" == results[1].content
        assert results[2].content == "invalid delegation arguments"
        assert all("/private" not in result.content for result in results)
        assert all("internal.example" not in result.content for result in results)
    finally:
        await bus.shutdown()


@pytest.mark.asyncio
async def test_nested_delegation_does_not_deadlock_at_max_concurrent_one() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=1))

    async def specialist(task: str, child: ExecutionContext) -> str:
        assert child.call_path == ("coordinator", "specialist")
        return f"specialist:{task}"

    async def coordinator(task: str, child: ExecutionContext) -> str:
        result = await bus.request(child, "specialist", task)
        return f"coordinator:{result.value}"

    bus.register(user_id="user-1", agent_id="coordinator", handler=coordinator)
    bus.register(user_id="user-1", agent_id="specialist", handler=specialist)

    result = await asyncio.wait_for(bus.request(context(), "coordinator", "analyze"), timeout=1)

    assert result.value == "coordinator:specialist:analyze"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_same_target_is_fifo_while_different_targets_run_in_parallel() -> None:
    bus = InProcessMessageBus(AsyncTaskQueue(max_concurrent=2))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execution_order: list[str] = []

    async def serial(task: str, child: ExecutionContext) -> str:
        del child
        execution_order.append(f"start:{task}")
        if task == "first":
            first_started.set()
            await release_first.wait()
        execution_order.append(f"done:{task}")
        return task

    parallel_started = {"left": asyncio.Event(), "right": asyncio.Event()}

    def parallel_handler(name: str) -> Callable[[str, ExecutionContext], Awaitable[str]]:
        async def handler(task: str, child: ExecutionContext) -> str:
            del task, child
            parallel_started[name].set()
            await parallel_started["right" if name == "left" else "left"].wait()
            return name

        return handler

    bus.register(user_id="user-1", agent_id="serial", handler=serial)
    bus.register(user_id="user-1", agent_id="left", handler=parallel_handler("left"))
    bus.register(user_id="user-1", agent_id="right", handler=parallel_handler("right"))

    first = asyncio.create_task(bus.request(context(root="one"), "serial", "first"))
    await first_started.wait()
    second = asyncio.create_task(bus.request(context(root="two"), "serial", "second"))
    await asyncio.sleep(0)
    assert execution_order == ["start:first"]
    release_first.set()
    await asyncio.gather(first, second)
    assert execution_order == ["start:first", "done:first", "start:second", "done:second"]

    results = await asyncio.wait_for(
        asyncio.gather(
            bus.request(context(root="left-root"), "left", "task"),
            bus.request(context(root="right-root"), "right", "task"),
        ),
        timeout=1,
    )
    assert {result.value for result in results} == {"left", "right"}
    await bus.shutdown()


@pytest.mark.asyncio
async def test_batch_deduplicates_exact_agent_and_task_within_root() -> None:
    bus = InProcessMessageBus()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del child
        calls += 1
        await asyncio.sleep(0)
        return task.upper()

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    results = await bus.batch(
        context(),
        (
            DelegationRequest(agent_id="worker", task="same"),
            DelegationRequest(agent_id="worker", task="same"),
            DelegationRequest(agent_id="worker", task="different"),
        ),
    )

    assert calls == 2
    assert results[0].result == results[1].result
    assert results[0].succeeded
    assert results[2].result is not None
    assert results[2].result.value == "DIFFERENT"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cycles_and_cross_tenant_requests_are_rejected() -> None:
    bus = InProcessMessageBus()

    async def agent_a(task: str, child: ExecutionContext) -> str:
        del task
        return (await bus.request(child, "agent-b", "from-a")).value

    async def agent_b(task: str, child: ExecutionContext) -> str:
        del task
        return (await bus.request(child, "agent-a", "from-b")).value

    bus.register(user_id="user-1", agent_id="agent-a", handler=agent_a)
    bus.register(user_id="user-1", agent_id="agent-b", handler=agent_b)
    bus.register(user_id="user-2", agent_id="foreign", handler=agent_b)

    with pytest.raises(DelegationCycleError, match="cycle"):
        await bus.request(context(), "agent-a", "start")
    with pytest.raises(CrossTenantError):
        await bus.request(context(), "foreign", "denied")
    await bus.shutdown()


@pytest.mark.asyncio
async def test_wait_graph_rejects_a_cycle_even_with_inconsistent_call_paths() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="agent-a", handler=blocking)
    bus.register(user_id="user-1", agent_id="agent-b", handler=blocking)
    first = asyncio.create_task(
        bus.request(
            context(root="shared", agent_id="agent-a", path=("agent-a",)),
            "agent-b",
            "first",
        )
    )
    await started.wait()

    with pytest.raises(DelegationCycleError, match="wait graph"):
        await bus.request(
            context(root="shared", agent_id="agent-b", path=("agent-b",)),
            "agent-a",
            "second",
        )

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await bus.shutdown()


@pytest.mark.asyncio
async def test_backpressure_rejects_more_than_configured_pending_tasks() -> None:
    queue = AsyncTaskQueue(max_concurrent=1, max_pending=1)
    bus = InProcessMessageBus(queue)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await release.wait()
        return "done"

    bus.register(user_id="user-1", agent_id="first", handler=blocking)
    bus.register(user_id="user-1", agent_id="second", handler=blocking)
    running = asyncio.create_task(bus.request(context(root="one"), "first", "task"))
    await started.wait()

    with pytest.raises(BackpressureError, match="pending task limit"):
        await bus.request(context(root="two"), "second", "task")

    release.set()
    await running
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cancellation_propagates_to_running_handler_and_clears_pending() -> None:
    queue = AsyncTaskQueue()
    bus = InProcessMessageBus(queue)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="worker", handler=blocking)
    request = asyncio.create_task(bus.request(context(), "worker", "task"))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0)

    assert queue.pending_count == 0
    await bus.shutdown()


@pytest.mark.asyncio
async def test_shutdown_completes_waiters_once_and_is_idempotent() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()

    async def blocking(task: str, child: ExecutionContext) -> str:
        del task, child
        started.set()
        await asyncio.Event().wait()
        return "unreachable"

    bus.register(user_id="user-1", agent_id="worker", handler=blocking)
    request = asyncio.create_task(bus.request(context(), "worker", "task"))
    await started.wait()
    await bus.shutdown()
    await bus.shutdown()

    with pytest.raises(QueueShutdownError):
        await request


def test_duplicate_agent_registration_is_rejected() -> None:
    bus = InProcessMessageBus()

    async def handler(task: str, child: ExecutionContext) -> str:
        del task, child
        return "done"

    bus.register(user_id="user-1", agent_id="global-agent-id", handler=handler)
    with pytest.raises(ValueError, match="already registered"):
        bus.register(user_id="user-1", agent_id="global-agent-id", handler=handler)


@pytest.mark.asyncio
async def test_cancelling_one_shared_waiter_does_not_cancel_the_job() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del task, child
        calls += 1
        started.set()
        await release.wait()
        return "shared-result"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    first = asyncio.create_task(bus.request(context(), "worker", "same"))
    second = asyncio.create_task(bus.request(context(), "worker", "same"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()

    assert (await second).value == "shared-result"
    assert calls == 1
    await bus.shutdown()


@pytest.mark.asyncio
async def test_cancelling_one_root_branch_does_not_cancel_its_sibling() -> None:
    bus = InProcessMessageBus()
    started = {"left": asyncio.Event(), "right": asyncio.Event()}
    release_right = asyncio.Event()

    def make_handler(name: str) -> Callable[[str, ExecutionContext], Awaitable[str]]:
        async def handler(task: str, child: ExecutionContext) -> str:
            del task, child
            started[name].set()
            if name == "right":
                await release_right.wait()
            else:
                await asyncio.Event().wait()
            return name

        return handler

    bus.register(user_id="user-1", agent_id="left", handler=make_handler("left"))
    bus.register(user_id="user-1", agent_id="right", handler=make_handler("right"))
    left = asyncio.create_task(bus.request(context(), "left", "task"))
    right = asyncio.create_task(bus.request(context(), "right", "task"))
    await asyncio.gather(started["left"].wait(), started["right"].wait())
    left.cancel()
    with pytest.raises(asyncio.CancelledError):
        await left
    release_right.set()

    assert (await right).value == "right"
    await bus.shutdown()


@pytest.mark.asyncio
async def test_new_submit_does_not_attach_to_a_cancelling_dedup_job() -> None:
    bus = InProcessMessageBus()
    started = asyncio.Event()
    cleaning = asyncio.Event()
    release_cleanup = asyncio.Event()
    calls = 0

    async def handler(task: str, child: ExecutionContext) -> str:
        nonlocal calls
        del task, child
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release_cleanup.wait()
        return f"run-{calls}"

    bus.register(user_id="user-1", agent_id="worker", handler=handler)
    first = asyncio.create_task(bus.request(context(), "worker", "same"))
    await started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await cleaning.wait()
    replacement = asyncio.create_task(bus.request(context(), "worker", "same"))
    await asyncio.sleep(0)
    release_cleanup.set()

    assert (await replacement).value == "run-2"
    assert calls == 2
    await bus.shutdown()


@pytest.mark.asyncio
async def test_batch_returns_all_results_in_order_and_redacts_errors() -> None:
    bus = InProcessMessageBus()

    async def success(task: str, child: ExecutionContext) -> str:
        del child
        await asyncio.sleep(0)
        return task.upper()

    async def failure(task: str, child: ExecutionContext) -> str:
        del task, child
        raise RuntimeError("database /private/secret.db on internal.example failed")

    bus.register(user_id="user-1", agent_id="success", handler=success)
    bus.register(user_id="user-1", agent_id="failure", handler=failure)
    outcomes = await bus.batch(
        context(),
        (
            DelegationRequest("success", "first"),
            DelegationRequest("failure", "second"),
            DelegationRequest("missing", "third"),
            DelegationRequest("success", "fourth"),
        ),
    )

    assert [outcome.request.task for outcome in outcomes] == [
        "first",
        "second",
        "third",
        "fourth",
    ]
    assert outcomes[0].result is not None and outcomes[0].result.value == "FIRST"
    assert outcomes[1].error is not None
    assert outcomes[1].error.code is DelegationErrorCode.HANDLER_ERROR
    assert "/private" not in outcomes[1].error.message
    assert "internal.example" not in outcomes[1].error.message
    assert outcomes[2].error is not None
    assert outcomes[2].error.code is DelegationErrorCode.UNKNOWN_AGENT
    assert outcomes[3].result is not None and outcomes[3].result.value == "FOURTH"
    await bus.shutdown()


async def test_seeded_submit_cancel_shutdown_interleavings_leave_no_jobs() -> None:
    for seed in range(12):
        generator = random.Random(seed)
        queue = AsyncTaskQueue(max_concurrent=3, max_pending=24)
        tickets: list[WaitTicket] = []

        async def completed(index: int, delay: float) -> TaskResult:
            await asyncio.sleep(delay)
            return TaskResult(correlation_id=f"correlation-{index}", value=str(index))

        for index in range(32):
            action = generator.randrange(4)
            root = f"root-{generator.randrange(5)}"
            if action < 2:
                try:
                    delay = generator.random() / 1000

                    async def handler(
                        index: int = index,
                        delay: float = delay,
                    ) -> TaskResult:
                        return await completed(index, delay)

                    ticket = await queue.submit(
                        target=("user", f"agent-{generator.randrange(4)}"),
                        dedup_key=("user", root, f"agent-{index % 4}", f"task-{index}"),
                        root_execution_id=root,
                        inherit_slot=False,
                        handler=handler,
                    )
                except BackpressureError:
                    continue
                tickets.append(ticket)
            elif action == 2:
                await queue.cancel_root(root)
            elif tickets:
                await tickets[generator.randrange(len(tickets))].release(cancel=True)
            await asyncio.sleep(0)

        await queue.shutdown()
        await asyncio.gather(*(ticket.result() for ticket in tickets), return_exceptions=True)

        assert queue.pending_count == 0
        assert not queue._workers


async def test_task_snapshots_keep_safe_recent_terminal_state() -> None:
    queue = AsyncTaskQueue()

    async def handler() -> TaskResult:
        return TaskResult(correlation_id="correlation", value="complete")

    ticket = await queue.submit(
        target=("user", "agent"),
        dedup_key=("user", "root", "agent", "task"),
        root_execution_id="root",
        inherit_slot=False,
        handler=handler,
    )
    assert (await ticket.result()).value == "complete"
    await ticket.release()
    await asyncio.sleep(0)

    snapshots = queue.recent_tasks()
    assert snapshots[0].user_id == "user"
    assert len(snapshots) == 1
    assert snapshots[0].agent_id == "agent"
    assert snapshots[0].chat_key == "root"
    assert snapshots[0].status == "completed"
    assert snapshots[0].error == ""
    assert snapshots[0].done_at is not None
    await queue.shutdown()

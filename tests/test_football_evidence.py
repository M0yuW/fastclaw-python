from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import httpx
import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.tools import FOOTBALL_COMPETITIONS, FootballDataTool, ToolResult
from fastclaw.tools.football_data import EspnPublicFetcher
from fastclaw.tools.football_evidence import SourceResult, TheOddsApiSource
from fastclaw.tools.football_shared import FootballEvidenceCache


class FixtureFetcher:
    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def execute(
        self, arguments: dict[str, object], execution: ExecutionContext
    ) -> ToolResult:
        del execution
        url = str(arguments["url"])
        self.urls.append(url)
        for marker, response in self.responses.items():
            if marker not in url:
                continue
            if isinstance(response, BaseException):
                raise response
            if isinstance(response, ToolResult):
                return response
            return ToolResult(content=json.dumps(response))
        return ToolResult(content="fixture not found", is_error=True)


class StaticOdds:
    def __init__(self, result: SourceResult) -> None:
        self.result = result

    async def fetch(self, *_args: object, **_kwargs: object) -> SourceResult:
        return self.result


def context() -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="run-1",
    )


PRIMARY_EVENT = {
    "idEvent": "21001",
    "strLeague": "Swedish Allsvenskan",
    "strSeason": "2026",
    "strEvent": "IK Sirius vs IF Brommapojkarna",
    "strHomeTeam": "IK Sirius",
    "strAwayTeam": "IF Brommapojkarna",
    "dateEvent": "2026-08-10",
    "strTime": "17:00:00",
    "strVenue": "Studenternas IP",
}

ESPN_TEAMS = [
    {"homeAway": "home", "team": {"id": "1", "displayName": "IK Sirius"}},
    {
        "homeAway": "away",
        "team": {"id": "2", "displayName": "IF Brommapojkarna"},
    },
]


def fixture_responses() -> dict[str, object]:
    teams = copy.deepcopy(ESPN_TEAMS)
    return {
        "search_all_leagues.php": {
            "countries": [
                {
                    "idLeague": "4613",
                    "strLeague": "Swedish Allsvenskan",
                    "strLeagueAlternate": "Allsvenskan",
                    "strCountry": "Sweden",
                }
            ]
        },
        "eventsday.php": {"events": [PRIMARY_EVENT]},
        "eventsseason.php": {"events": [PRIMARY_EVENT]},
        "lookuptable.php": {"table": [{"intRank": "1", "strTeam": "IK Sirius", "intPoints": "42"}]},
        "scoreboard": {
            "leagues": [{"slug": "swe.1"}],
            "events": [
                {
                    "id": "401842783",
                    "date": "2026-08-10T17:00Z",
                    "competitions": [{"competitors": teams}],
                }
            ],
        },
        "summary": {
            "header": {
                "league": {"slug": "swe.1"},
                "competitions": [
                    {
                        "date": "2026-08-10T17:00Z",
                        "status": {"type": {"description": "Scheduled"}},
                        "competitors": teams,
                    }
                ],
            },
            "gameInfo": {"venue": {"fullName": "Studenternas IP"}},
            "boxscore": {"form": []},
            "headToHeadGames": [],
        },
        "getMatchCalculatorV1": {
            "value": {
                "matchInfoList": [
                    {
                        "subMatchList": [
                            {
                                "matchId": "swe-1",
                                "leagueAllName": "Swedish Allsvenskan",
                                "homeTeamAllName": "IK Sirius",
                                "awayTeamAllName": "IF Brommapojkarna",
                                "had": {"h": "2.10", "d": "3.20", "a": "3.05"},
                            }
                        ]
                    }
                ]
            }
        },
    }


def evidence_arguments(**extra: object) -> dict[str, object]:
    return {
        "action": "evidence",
        "competition": "瑞典超",
        "country": "Sweden",
        "date": "2026-08-10",
        "season": "2026",
        "team_a": "IK Sirius",
        "team_b": "IF Brommapojkarna",
        **extra,
    }


async def run_evidence(
    responses: dict[str, object] | None = None,
    *,
    odds: SourceResult | None = None,
    allow_sporttery: bool = False,
) -> tuple[ToolResult, FixtureFetcher]:
    fetcher = FixtureFetcher(responses or fixture_responses())
    odds_result = odds or SourceResult(
        "the_odds_api",
        "success",
        [
            {
                "home_team": "IK Sirius",
                "away_team": "IF Brommapojkarna",
                "commence_time": "2026-08-10T17:00:00Z",
            }
        ],
    )
    tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        odds_source=StaticOdds(odds_result),
        allow_sporttery=allow_sporttery,
    )
    return await tool.execute(evidence_arguments(), context()), fetcher


@pytest.mark.asyncio
async def test_data_base_evidence_is_cached_by_fixture_identity() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    cache = FootballEvidenceCache()
    first_tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=cache,
    )
    second_tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=cache,
    )

    first = await first_tool.execute(
        {**evidence_arguments(), "action": "base_evidence"}, context()
    )
    request_count = len(fetcher.urls)
    second = await second_tool.execute(
        {**evidence_arguments(), "action": "base_evidence"}, context()
    )

    assert not first.is_error
    assert not second.is_error
    assert second.metadata["cacheHit"] is True
    assert len(fetcher.urls) == request_count


@pytest.mark.asyncio
async def test_primary_and_espn_success_preserve_primary_fixture_identity() -> None:
    result, _fetcher = await run_evidence()
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["fixture"]["event_id"] == "21001"
    assert payload["details"]["source_event_id"] == "401842783"
    assert payload["fixture"]["venue"] == "Studenternas IP"
    assert {item["status"] for item in payload["sources"]} == {"success"}


@pytest.mark.asyncio
async def test_world_cup_schedule_compatibility_uses_confirmed_primary_id() -> None:
    responses: dict[str, object] = {
        "search_all_leagues.php": {
            "countries": [
                {
                    "idLeague": "4429",
                    "strLeague": "FIFA World Cup",
                    "strCountry": "Worldwide",
                }
            ]
        },
        "eventsday.php": {"events": [{"strEvent": "France vs Sweden"}]},
    }
    fetcher = FixtureFetcher(responses)
    result = await FootballDataTool(fetcher, espn_fetcher=fetcher).execute(
        {
            "action": "schedule",
            "competition": "世界杯",
            "date": "2026-06-30",
        },
        context(),
    )

    assert not result.is_error
    assert json.loads(result.content)["rows"][0]["match"] == "France vs Sweden"
    assert any("l=4429" in url for url in fetcher.urls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("espn_response", "error_code"),
    [
        (
            ToolResult(
                content="forbidden",
                is_error=True,
                metadata={"errorCode": "espn_http_403"},
            ),
            "espn_http_403",
        ),
        (TimeoutError(), "espn_timeout"),
        (ToolResult(content="{not-json"), "espn_malformed_json"),
        ({"leagues": [{"slug": "fifa.world"}], "events": []}, "espn_league_mismatch"),
    ],
)
async def test_espn_failures_degrade_without_failing_primary(
    espn_response: object, error_code: str
) -> None:
    responses = fixture_responses()
    responses["scoreboard"] = espn_response
    result, _fetcher = await run_evidence(responses)
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["fixture"]["event_id"] == "21001"
    espn = next(item for item in payload["sources"] if item["source"] == "espn")
    assert espn["error_code"] == error_code
    assert payload["completeness"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_value", ["Wrong Team", "2026-08-11T17:00Z"])
async def test_espn_fixture_identity_mismatch_is_rejected(bad_value: str) -> None:
    responses = fixture_responses()
    scoreboard = responses["scoreboard"]
    assert isinstance(scoreboard, dict)
    event = scoreboard["events"][0]
    if bad_value.startswith("2026"):
        event["date"] = bad_value
    else:
        event["competitions"][0]["competitors"][0]["team"]["displayName"] = bad_value
    result, _fetcher = await run_evidence(responses)
    payload = json.loads(result.content)
    espn = next(item for item in payload["sources"] if item["source"] == "espn")

    assert not result.is_error
    assert espn["status"] == "rejected"
    assert espn["error_code"] == "espn_fixture_mismatch"


@pytest.mark.asyncio
async def test_odds_failure_does_not_call_sporttery_for_non_ev_roles() -> None:
    unavailable = SourceResult(
        "the_odds_api",
        "unavailable",
        error_code="odds_key_missing",
        safe_reason="The Odds API is not configured",
    )
    result, fetcher = await run_evidence(odds=unavailable)
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["odds"] == {"status": "unavailable"}
    assert "odds_unavailable" in payload["warnings"]
    assert not any("getMatchCalculatorV1" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_ev_role_can_use_sporttery_as_fallback() -> None:
    unavailable = SourceResult(
        "the_odds_api",
        "unavailable",
        error_code="odds_key_missing",
        safe_reason="The Odds API is not configured",
    )
    result, _fetcher = await run_evidence(odds=unavailable, allow_sporttery=True)
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["odds"]["source"] == "sporttery"
    assert payload["odds"]["match_id"] == "swe-1"


@pytest.mark.asyncio
async def test_ev_sporttery_requires_explicit_user_request() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    tool = FootballDataTool(fetcher, allow_sporttery=True, role_mode="ev")
    arguments = {
        "action": "sporttery_match",
        "team_a": "IK Sirius",
        "team_b": "IF Brommapojkarna",
    }

    blocked = await tool.execute(arguments, context())
    assert blocked.is_error
    assert "explicit user request" in blocked.content
    assert fetcher.urls == []

    explicit = context()
    explicit.shared_state.ev_requested = True
    allowed = await tool.execute(arguments, explicit)
    assert not allowed.is_error
    assert any("getMatchCalculatorV1" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_both_odds_sources_can_fail_without_failing_primary() -> None:
    responses = fixture_responses()
    responses["getMatchCalculatorV1"] = ToolResult(content="offline", is_error=True)
    result, fetcher = await run_evidence(
        responses,
        odds=SourceResult("the_odds_api", "unavailable", error_code="odds_request_failed"),
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["fixture"]["event_id"] == "21001"
    assert payload["odds"] == {"status": "unavailable"}
    assert "odds_unavailable" in payload["warnings"]
    assert not any("getMatchCalculatorV1" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_all_primary_facts_unavailable_fails_closed() -> None:
    responses = fixture_responses()
    responses["search_all_leagues.php"] = ToolResult(content="offline", is_error=True)
    result, _fetcher = await run_evidence(responses)
    payload = json.loads(result.content)

    assert result.is_error
    assert result.metadata["errorCode"] == "football_primary_unavailable"
    assert payload["fixture"] is None
    assert payload["completeness"] == "unavailable"
    assert any("do not infer facts" in item for item in payload["warnings"])


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden", ["url", "apiKey", "sport_key", "espn_slug", "league_id"])
async def test_model_cannot_supply_provider_identifiers(forbidden: str) -> None:
    fetcher = FixtureFetcher({})
    result = await FootballDataTool(fetcher, espn_fetcher=fetcher).execute(
        evidence_arguments(**{forbidden: "secret-or-provider-value"}), context()
    )

    assert result.is_error
    assert "Runtime-managed" in json.loads(result.content)["error"]
    assert fetcher.urls == []


def _load_skill_module(name: str, relative: str) -> ModuleType:
    path = Path(__file__).parents[1] / "skills" / "match-data-toolkit" / "scripts" / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_and_skill_provider_mappings_are_identical() -> None:
    skill = _load_skill_module("skill_football_competitions", "common/football_competitions.py")
    runtime = {
        (item.name, item.country, item.espn_slug, item.odds_sport_key)
        for item in FOOTBALL_COMPETITIONS
    }
    bundled = {(name, country, espn, odds) for name, country, espn, odds, _ in skill.COMPETITIONS}

    assert runtime == bundled
    assert skill.resolve_competition("世界杯")["odds_sport_key"] == "soccer_fifa_world_cup"
    assert skill.resolve_competition("瑞典超")["espn_slug"] == "swe.1"


def test_odds_key_missing_and_catalog_or_quota_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    odds = _load_skill_module("bundled_odds_data", "odds_data.py")
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    missing = odds.fetch_odds("瑞典超")
    assert missing["status"] == "unavailable"
    assert missing["error_code"] == "odds_key_missing"

    monkeypatch.setenv("ODDS_API_KEY", "fixture-value")
    calls = iter(
        [
            ([{"key": "soccer_epl", "active": True}], {"requests_remaining": "498"}),
            (
                [{"key": "soccer_sweden_allsvenskan", "active": True}],
                {"requests_remaining": "497"},
            ),
            ([], {"requests_remaining": "496", "requests_used": "4"}),
        ]
    )
    monkeypatch.setattr(odds, "_request", lambda *_args, **_kwargs: next(calls))
    rejected = odds.fetch_odds("瑞典超")
    success = odds.fetch_odds("瑞典超")

    assert rejected["error_code"] == "odds_sport_not_in_catalog"
    assert success["status"] == "empty"
    assert success["quota"]["requests_remaining"] == "496"
    assert success["quota"]["requests_used"] == "4"


@pytest.mark.asyncio
async def test_runtime_odds_catalog_confirmation_and_quota_headers() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if request.url.path == "/v4/sports":
            return httpx.Response(
                200,
                json=[{"key": "soccer_sweden_allsvenskan", "active": True}],
            )
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": "499", "x-requests-used": "1"},
        )

    source = TheOddsApiSource("fixture-value", transport=httpx.MockTransport(handler))
    competition = next(item for item in FOOTBALL_COMPETITIONS if item.key == "swedish-allsvenskan")
    result = await source.fetch(
        competition,
        regions=("eu",),
        markets=("h2h", "totals"),
        odds_format="decimal",
        commence_from="2026-08-10T00:00:00Z",
        commence_to="2026-08-11T00:00:00Z",
    )

    assert calls == 2
    assert result.status == "empty"
    assert result.quota == {
        "requests_remaining": "499",
        "requests_used": "1",
        "requests_last": None,
    }


@pytest.mark.asyncio
async def test_espn_missing_curl_is_structured_unavailable(tmp_path: Path) -> None:
    fetcher = EspnPublicFetcher()
    fetcher._executables = (tmp_path / "missing-curl",)
    result = await fetcher.execute(
        {"url": "https://site.api.espn.com/apis/site/v2/sports/soccer/swe.1/scoreboard"},
        context(),
    )

    assert result.is_error
    assert result.metadata == {"errorCode": "espn_curl_unavailable", "ready": False}


@pytest.mark.asyncio
async def test_espn_cancellation_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    executable = tmp_path / "curl"
    executable.touch()

    class Process:
        returncode: int | None = None
        killed = False
        reaped = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.reaped = True
            return -9

    process = Process()

    async def create(*_args: object, **_kwargs: object) -> Process:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    fetcher = EspnPublicFetcher()
    fetcher._executables = (executable,)
    task = asyncio.create_task(
        fetcher.execute(
            {"url": "https://site.api.espn.com/apis/site/v2/sports/soccer/swe.1/scoreboard"},
            context(),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert process.killed
    assert process.reaped

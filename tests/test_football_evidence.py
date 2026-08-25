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
from fastclaw.football_identity import TeamIdentityResolver
from fastclaw.tools import (
    FOOTBALL_COMPETITIONS,
    FootballContextTool,
    FootballDataTool,
    FootballOddsTool,
    ToolResult,
)
from fastclaw.tools.football_data import EspnPublicFetcher
from fastclaw.tools.football_evidence import SourceResult, TheOddsApiSource
from fastclaw.tools.football_shared import FootballEvidenceCache, football_event_key


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


class SlowFixtureFetcher(FixtureFetcher):
    async def execute(
        self, arguments: dict[str, object], execution: ExecutionContext
    ) -> ToolResult:
        await asyncio.sleep(0.01)
        return await super().execute(arguments, execution)


class HistoricalFixtureFetcher(FixtureFetcher):
    """Route prior-division fixtures separately from the current-season list."""

    async def execute(
        self, arguments: dict[str, object], execution: ExecutionContext
    ) -> ToolResult:
        url = str(arguments["url"])
        self.urls.append(url)
        if "eventsseason.php?id=4400&s=2025-2026" in url:
            return ToolResult(
                content=json.dumps(
                    {
                        "events": [
                            {
                                "idEvent": "2450001",
                                "strLeague": "Spanish La Liga 2",
                                "strSeason": "2025-2026",
                                "strEvent": "Racing de Santander vs Castellón",
                                "strHomeTeam": "Racing de Santander",
                                "strAwayTeam": "Castellón",
                                "idHomeTeam": "133726",
                                "idAwayTeam": "134700",
                                "dateEvent": "2025-08-16",
                                "intHomeScore": "3",
                                "intAwayScore": "1",
                                "strStatus": "FT",
                            }
                        ]
                    }
                )
            )
        if "eventsseason.php?id=4335&s=2025-2026" in url:
            return ToolResult(
                content=json.dumps(
                    {
                        "events": [
                            {
                                "idEvent": "2450002",
                                "strLeague": "Spanish La Liga",
                                "strSeason": "2025-2026",
                                "strEvent": "Villarreal vs Sevilla",
                                "strHomeTeam": "Villarreal",
                                "strAwayTeam": "Sevilla",
                                "idHomeTeam": "133740",
                                "idAwayTeam": "133735",
                                "dateEvent": "2025-08-17",
                                "intHomeScore": "2",
                                "intAwayScore": "0",
                                "strStatus": "FT",
                            }
                        ]
                    }
                )
            )
        return await super().execute(arguments, execution)


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


def test_team_identity_resolver_unifies_translations_prefixes_and_provider_ids() -> None:
    resolver = TeamIdentityResolver()

    assert resolver.resolve("西班牙人").canonical_id == "espanyol"
    assert resolver.resolve("RCD Espanyol").canonical_id == "espanyol"
    assert resolver.resolve("Levante UD").canonical_id == "levante"
    assert resolver.resolve("莱万特").canonical_id == "levante"
    assert resolver.resolve("Villarreal CF").canonical_id == "villarreal"
    assert resolver.resolve("Real Racing Club de Santander").canonical_id == "racingsantander"
    assert resolver.resolve("GNK Dinamo Zagreb").canonical_id == "dinamozagreb"
    assert resolver.resolve("Mjällby").canonical_id == "mjallby"
    assert resolver.resolve("Mjällby AIF").canonical_id == "mjallby"
    assert resolver.resolve("Red Bull Salzburg").canonical_id == "rbsalzburg"
    assert resolver.resolve("RB Salzburg").canonical_id == "rbsalzburg"
    assert resolver.resolve("Aarhus").canonical_id == "agf"
    assert resolver.resolve("Aarhus Gymnastikforening").canonical_id == "agf"
    assert resolver.resolve("Aarhus GF").canonical_id == "agf"
    assert resolver.resolve("AGF Aarhus").canonical_id == "agf"
    assert resolver.resolve("AGF").canonical_id == "agf"

    primary = resolver.resolve("RCD Espanyol", provider="thesportsdb", provider_id="100")
    same_provider_id = resolver.resolve(
        "Provider display label", provider="thesportsdb", provider_id="100"
    )
    unrelated_name = resolver.resolve(
        "Provider display label", provider="thesportsdb", provider_id="101"
    )
    assert resolver.equivalent(primary, same_provider_id)
    assert not resolver.equivalent(primary, unrelated_name)


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
        "searchteams.php": {"teams": [{"idTeam": "100", "strTeam": "IK Sirius"}]},
        "eventslast.php": {"results": [PRIMARY_EVENT]},
        "lookupevent.php": {
            "events": [
                {
                    **PRIMARY_EVENT,
                    "strHomeFormation": "4-3-3",
                    "strAwayFormation": "4-4-2",
                    "strHomeLineupGoalkeeper": "Goalkeeper",
                    "strAwayLineupGoalkeeper": "Away Goalkeeper",
                }
            ]
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


def dutch_fixture_responses() -> dict[str, object]:
    return {
        "search_all_leagues.php": {
            "countries": [
                {
                    "idLeague": "4641",
                    "strLeague": "Dutch Eerste Divisie",
                    "strCountry": "The Netherlands",
                },
                {
                    "idLeague": "4337",
                    "strLeague": "Dutch Eredivisie",
                    "strLeagueAlternate": "Eredivisie",
                    "strCountry": "The Netherlands",
                },
            ]
        },
        "eventsday.php": {
            "events": [
                {
                    "idEvent": "2489078",
                    "strLeague": "Dutch Eredivisie",
                    "strSeason": "2026-2027",
                    "strHomeTeam": "Excelsior",
                    "strAwayTeam": "PSV Eindhoven",
                    "dateEvent": "2026-08-15",
                    "strTime": "18:00:00",
                    "strVenue": "Van Donge and De Roo Stadion",
                }
            ]
        },
        "eventsseason.php": {"events": []},
        "lookuptable.php": {"table": []},
        "scoreboard": {"leagues": [{"slug": "ned.1"}], "events": []},
    }


@pytest.mark.asyncio
async def test_dutch_competition_uses_provider_country_and_team_prefix_aliases() -> None:
    fetcher = FixtureFetcher(dutch_fixture_responses())
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher)
    result = await tool.execute(
        {
            "action": "evidence",
            "competition": "荷甲",
            "date": "2026-08-15",
            "season": "2026-2027",
            "team_a": "SBV Excelsior",
            "team_b": "PSV Eindhoven",
        },
        context(),
    )
    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["fixture"]["event_id"] == "2489078"
    assert any("c=The+Netherlands" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_data_role_can_query_results_for_ledger_settlement_review() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    tool = FootballDataTool(fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "瑞典超",
            "country": "Sweden",
            "season": "2026",
            "date": "2026-08-10",
        },
        context(),
    )

    assert not result.is_error
    payload = json.loads(result.content)
    assert payload["action"] == "results"
    assert payload["rows"][0]["home"] == "IK Sirius"
    assert payload["source"] == "thesportsdb:eventsday.php"


@pytest.mark.asyncio
async def test_settlement_results_reject_date_only_queries() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    execution = context()
    execution.shared_state.football_settlement_review = True
    tool = FootballDataTool(fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "瑞典超",
            "season": "2026",
            "date": "2026-08-10",
        },
        execution,
    )

    assert result.is_error
    assert result.metadata["errorCode"] == "football_settlement_target_required"
    assert "team_a" in result.content and "team_b" in result.content
    assert fetcher.urls == []
    assert execution.shared_state.football_settlement_errors


def _espn_results_event(
    event_id: str,
    home: str,
    away: str,
    home_score: str,
    away_score: str,
) -> dict[str, object]:
    return {
        "id": event_id,
        "name": f"{home} vs {away}",
        "date": "2026-08-22T15:15Z",
        "status": {"type": {"description": "Full Time"}},
        "competitions": [
            {
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"id": "lens-espn", "displayName": home},
                        "score": home_score,
                    },
                    {
                        "homeAway": "away",
                        "team": {"id": "auxerre-espn", "displayName": away},
                        "score": away_score,
                    },
                ]
            }
        ],
    }


def _truncated_french_results_responses(
    *, espn_events: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "search_all_leagues.php": {
            "countries": [
                {
                    "idLeague": "4334",
                    "strLeague": "French Ligue 1",
                    "strLeagueAlternate": "Ligue 1",
                    "strCountry": "France",
                }
            ]
        },
        "eventsday.php": {
            "events": [
                {
                    "idEvent": "2489464",
                    "strLeague": "French Ligue 1",
                    "strSeason": "2026-2027",
                    "strEvent": "Nice vs Lorient",
                    "strHomeTeam": "Nice",
                    "strAwayTeam": "Lorient",
                    "dateEvent": "2026-08-22",
                    "strStatus": "FT",
                    "intHomeScore": "0",
                    "intAwayScore": "0",
                },
                {
                    "idEvent": "2489466",
                    "strLeague": "French Ligue 1",
                    "strSeason": "2026-2027",
                    "strEvent": "Toulouse vs Lyon",
                    "strHomeTeam": "Toulouse",
                    "strAwayTeam": "Lyon",
                    "dateEvent": "2026-08-22",
                    "strStatus": "FT",
                    "intHomeScore": "1",
                    "intAwayScore": "2",
                },
                {
                    "idEvent": "2489467",
                    "strLeague": "French Ligue 1",
                    "strSeason": "2026-2027",
                    "strEvent": "Troyes vs Paris FC",
                    "strHomeTeam": "Troyes",
                    "strAwayTeam": "Paris FC",
                    "dateEvent": "2026-08-22",
                    "strStatus": "FT",
                    "intHomeScore": "1",
                    "intAwayScore": "1",
                },
            ]
        },
        "scoreboard": {
            "leagues": [{"slug": "fra.1"}],
            "events": espn_events,
        },
    }


@pytest.mark.asyncio
async def test_results_fall_back_to_espn_when_tsdb_day_omits_requested_fixture() -> None:
    fetcher = FixtureFetcher(
        _truncated_french_results_responses(
            espn_events=[_espn_results_event("401999", "Lens", "Auxerre", "2", "1")]
        )
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "法甲",
            "season": "2026-2027",
            "date": "2026-08-22",
            "team_a": "RC Lens",
            "team_b": "AJ Auxerre",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"] == "espn:primary_results"
    assert payload["fallback"] == "thesportsdb_to_espn"
    assert payload["count"] == 1
    assert payload["rows"][0]["match"] == "Lens vs Auxerre"
    assert payload["rows"][0]["home_score"] == "2"
    assert not any(
        row["match"] == "Nice vs Lorient" for row in payload["rows"]
    )
    assert any("eventsday.php" in url for url in fetcher.urls)
    assert any("fra.1/scoreboard" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_results_can_force_espn_without_calling_tsdb_day_endpoint() -> None:
    fetcher = FixtureFetcher(
        _truncated_french_results_responses(
            espn_events=[_espn_results_event("401999", "Lens", "Auxerre", "2", "1")]
        )
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "法甲",
            "source": "espn",
            "season": "2026-2027",
            "date": "2026-08-22",
            "team_a": "Lens",
            "team_b": "Auxerre",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"] == "espn:primary_results"
    assert payload["fallback"] == "explicit_espn"
    assert payload["rows"][0]["match"] == "Lens vs Auxerre"
    assert not any("eventsday.php" in url for url in fetcher.urls)
    assert any("fra.1/scoreboard" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_uel_results_fall_back_to_reviewed_espn_qualifying_feed() -> None:
    def event(
        event_id: str,
        home: str,
        away: str,
        home_score: str,
        away_score: str,
    ) -> dict[str, object]:
        return {
            "id": event_id,
            "date": "2026-08-20T19:00Z",
            "season": {"year": 2026, "slug": "playoff-round"},
            "status": {"type": {"description": "Full Time"}},
            "competitions": [
                {
                    "venue": {"fullName": "Fixture Stadium"},
                    "competitors": [
                        {
                            "homeAway": "home",
                            "team": {"id": f"h-{event_id}", "displayName": home},
                            "score": home_score,
                        },
                        {
                            "homeAway": "away",
                            "team": {"id": f"a-{event_id}", "displayName": away},
                            "score": away_score,
                        },
                    ],
                }
            ],
        }

    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4480",
                        "strLeague": "UEFA Champions League",
                        "strCountry": "Europe",
                    }
                ]
            },
            "uefa.europa/scoreboard": {
                "leagues": [{"slug": "uefa.europa"}],
                "events": [],
            },
            "uefa.europa_qual/scoreboard": {
                "leagues": [{"slug": "uefa.europa_qual"}],
                "events": [
                    event("401", "Kairat Almaty", "Anderlecht", "1", "2"),
                    event("402", "Lech Poznań", "FC Thun", "3", "0"),
                ],
            },
        }
    )
    execution = context()
    execution.shared_state.football_settlement_review = True
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "UEFA Europa League",
            "season": "2026-2027",
            "date": "2026-08-20",
            "team_a": "Kairat Almaty",
            "team_b": "Anderlecht",
        },
        execution,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"] == "espn:qualifying_results"
    assert payload["fallback"] == "thesportsdb_to_espn"
    assert payload["count"] == 1
    assert payload["rows"][0]["status"] == "FT"
    assert payload["rows"][0]["home_score"] == "1"
    assert payload["rows"][0]["away_score"] == "2"
    assert payload["rows"][0]["home_canonical_id"] == "kairatalmaty"
    assert len(execution.shared_state.football_settlement_results) == 1
    assert any("uefa.europa/scoreboard" in url for url in fetcher.urls)
    assert any("uefa.europa_qual/scoreboard" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_explicit_uel_qualifying_results_use_qualifying_feed_first() -> None:
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {"countries": []},
            "uefa.europa_qual/scoreboard": {
                "leagues": [{"slug": "uefa.europa_qual"}],
                "events": [],
            },
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "results",
            "competition": "UEFA Europa League Qualifying",
            "season": "2026-2027",
            "date": "2026-08-20",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["status"] == "no_match"
    assert result.metadata["errorCode"] == "results_no_match"
    assert any("uefa.europa_qual/scoreboard" in url for url in fetcher.urls)
    assert not any("uefa.europa/scoreboard" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_data_role_ignores_redundant_evidence_after_base_is_published() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    execution = context()
    execution.shared_state.publish_football_base(
        "swedishallsvenskan|2026|2026-08-10|iksirius|ifbrommapojkarna",
        json.dumps(
            {
                "evidence_schema_version": 3,
                "fixture": {
                    "competition": "Swedish Allsvenskan",
                    "season": "2026",
                    "date": "2026-08-10",
                    "home": "IK Sirius",
                    "away": "IF Brommapojkarna",
                },
                "historical_context": {},
            }
        ),
    )
    tool = FootballDataTool(fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "evidence",
            "competition": "La Liga 2",
            "season": "2025/26",
            "date": "2025-08-16",
            "team_a": "Racing Santander",
            "team_b": "Villarreal",
        },
        execution,
    )

    assert not result.is_error
    assert result.metadata["status"] == "base_already_published"
    assert fetcher.urls == []


@pytest.mark.asyncio
async def test_settlement_review_forbids_base_evidence_context_and_odds() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    execution = context()
    execution.shared_state.football_settlement_review = True
    data_tool = FootballDataTool(fetcher, role_mode="data")

    base = await data_tool.execute(
        {**evidence_arguments(), "action": "base_evidence"}, execution
    )
    evidence = await data_tool.execute(
        {**evidence_arguments(), "action": "evidence"}, execution
    )
    context_result = await FootballContextTool().execute(
        {
            "competition": "瑞典超",
            "season": "2026",
            "date": "2026-08-10",
            "team_a": "IK Sirius",
            "team_b": "IF Brommapojkarna",
        },
        execution,
    )
    odds = await FootballOddsTool().execute(
        {
            "competition": "瑞典超",
            "season": "2026",
            "date": "2026-08-10",
            "team_a": "IK Sirius",
            "team_b": "IF Brommapojkarna",
        },
        execution,
    )

    assert base.is_error and "action=results" in base.content
    assert evidence.is_error and "action=results" in evidence.content
    assert context_result.is_error
    assert context_result.metadata["errorCode"] == "football_settlement_context_forbidden"
    assert odds.is_error and "settlement review" in odds.content
    assert fetcher.urls == []


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
async def test_base_evidence_discovers_missing_date_from_unique_season_fixture() -> None:
    fetcher = FixtureFetcher(fixture_responses())
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")
    arguments = evidence_arguments(action="base_evidence")
    arguments.pop("date")

    result = await tool.execute(arguments, context())

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["date"] == "2026-08-10"
    assert payload["fixture"]["requested_date"] == "2026-08-10"
    assert payload["fixture"]["date_alignment"] == "discovered_from_season_schedule"
    assert "requested_date_discovered_from_season_schedule" in payload["warnings"]
    assert not any("eventsday.php" in url for url in fetcher.urls)
    assert any("eventsseason.php" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_base_evidence_without_date_fails_as_unconfirmed_fixture_not_format_error() -> None:
    responses = fixture_responses()
    responses["eventsseason.php"] = {"events": []}
    fetcher = FixtureFetcher(responses)
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")
    arguments = evidence_arguments(action="base_evidence")
    arguments.pop("date")

    result = await tool.execute(arguments, context())

    payload = json.loads(result.content)
    assert not result.is_error
    assert result.metadata["status"] == "no_match"
    assert payload["status"] == "no_match"
    assert payload["fixture"] is None
    assert payload["requested"]["date"] is None
    assert "YYYY-MM-DD" not in result.content
    assert not any("eventsday.php" in url for url in fetcher.urls)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("team_a", "team_b", "country", "espn_home", "espn_away", "event_id", "venue"),
    [
        (
            "Levski Sofia",
            "AEK Athens",
            "Bulgaria",
            "Levski Sofia",
            "AEK Athens",
            "401909156",
            "Vasil Levski National Stadium",
        ),
        (
            "Dinamo Zagreb",
            "Viking",
            "Croatia",
            "GNK Dinamo Zagreb",
            "Viking FK",
            "401909158",
            "Maksimir Stadium",
        ),
        (
            "Fenerbahçe",
            "Lyon",
            "Turkey",
            "Fenerbahce",
            "Lyon",
            "401909204",
            "Ulker Stadyumu",
        ),
    ],
)
async def test_ucl_missing_date_falls_back_to_reviewed_qualifying_feed(
    team_a: str,
    team_b: str,
    country: str,
    espn_home: str,
    espn_away: str,
    event_id: str,
    venue: str,
) -> None:
    competitors = [
        {
            "homeAway": "home",
            "team": {"id": f"home-{event_id}", "displayName": espn_home},
        },
        {
            "homeAway": "away",
            "team": {"id": f"away-{event_id}", "displayName": espn_away},
        },
    ]
    responses = {
        "search_all_leagues.php": {
            "countries": [
                {
                    "idLeague": "4480",
                    "strLeague": "UEFA Champions League",
                    "strCountry": "Europe",
                }
            ]
        },
        "eventsseason.php": {"events": []},
        "lookuptable.php": {"table": []},
        "uefa.champions_qual/scoreboard": {
            "leagues": [{"slug": "uefa.champions_qual"}],
            "events": [
                {
                    "id": event_id,
                    "name": f"{espn_away} at {espn_home}",
                    "date": "2026-08-18T19:00Z",
                    "status": {"type": {"description": "Scheduled"}},
                    "competitions": [
                        {"venue": {"fullName": venue}, "competitors": competitors}
                    ],
                }
            ],
        },
        "uefa.champions_qual/summary": {
            "header": {
                "league": {"slug": "uefa.champions_qual"},
                "competitions": [
                    {
                        "date": "2026-08-18T19:00Z",
                        "status": {"type": {"description": "Scheduled"}},
                        "competitors": competitors,
                    }
                ],
            },
            "gameInfo": {"venue": {"fullName": venue}},
            "boxscore": {"form": []},
            "headToHeadGames": [],
        },
    }
    fetcher = FixtureFetcher(responses)
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "UEFA Champions League",
            "season": "2026-27",
            "team_a": team_a,
            "team_b": team_b,
            "country": country,
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == event_id
    assert payload["fixture"]["match"] == f"{espn_home} vs {espn_away}"
    assert payload["fixture"]["date"] == "2026-08-18"
    assert payload["fixture"]["time_utc"] == "19:00:00"
    assert payload["fixture"]["venue"] == venue
    assert payload["fixture"]["espn_slug"] == "uefa.champions_qual"
    assert payload["fixture"]["date_alignment"] == "discovered_from_espn_qualifying"
    assert "requested_date_discovered_from_espn_qualifying" in payload["warnings"]
    assert "primary_fixture_confirmed_by_espn_qualifying" in payload["warnings"]
    assert any(
        "uefa.champions_qual/scoreboard" in url and "dates=20260601-20260915" in url
        for url in fetcher.urls
    )


@pytest.mark.asyncio
async def test_ucl_missing_date_fails_closed_when_qualifying_feed_is_unavailable() -> None:
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4480",
                        "strLeague": "UEFA Champions League",
                        "strCountry": "Europe",
                    }
                ]
            },
            "eventsseason.php": {"events": []},
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "UEFA Champions League",
            "season": "2026-27",
            "team_a": "Unknown Home",
            "team_b": "Unknown Away",
        },
        context(),
    )

    assert result.is_error
    assert result.metadata["errorCode"] == "football_primary_unavailable"
    assert json.loads(result.content)["fixture"] is None


@pytest.mark.asyncio
async def test_ucl_missing_date_is_no_match_only_after_both_schedules_respond() -> None:
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4480",
                        "strLeague": "UEFA Champions League",
                        "strCountry": "Europe",
                    }
                ]
            },
            "eventsseason.php": {"events": []},
            "uefa.champions_qual/scoreboard": {
                "leagues": [{"slug": "uefa.champions_qual"}],
                "events": [],
            },
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "UEFA Champions League",
            "season": "2026-27",
            "team_a": "Unknown Home",
            "team_b": "Unknown Away",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert result.metadata["status"] == "no_match"
    assert payload["status"] == "no_match"
    assert any(
        source["source"] == "espn:qualifying_schedule"
        and source["status"] == "no_match"
        for source in payload["sources"]
    )


@pytest.mark.asyncio
async def test_ucl_qualifying_publishes_partial_base_when_tsdb_is_rate_limited() -> None:
    competitors = [
        {
            "homeAway": "home",
            "team": {"id": "490", "displayName": "Levski Sofia"},
        },
        {
            "homeAway": "away",
            "team": {"id": "887", "displayName": "AEK Athens"},
        },
    ]
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": ToolResult(
                content="rate limited",
                is_error=True,
                metadata={"errorCode": "web_http_429"},
            ),
            "uefa.champions_qual/scoreboard": {
                "leagues": [{"slug": "uefa.champions_qual"}],
                "events": [
                    {
                        "id": "401909156",
                        "date": "2026-08-18T19:00Z",
                        "status": {"type": {"description": "Scheduled"}},
                        "competitions": [
                            {
                                "venue": {
                                    "fullName": "Vasil Levski National Stadium"
                                },
                                "competitors": competitors,
                            }
                        ],
                    }
                ],
            },
            "uefa.champions_qual/summary": {
                "header": {
                    "league": {"slug": "uefa.champions_qual"},
                    "competitions": [
                        {
                            "date": "2026-08-18T19:00Z",
                            "status": {"type": {"description": "Scheduled"}},
                            "competitors": competitors,
                        }
                    ],
                },
                "gameInfo": {
                    "venue": {"fullName": "Vasil Levski National Stadium"}
                },
                "boxscore": {"form": []},
                "headToHeadGames": [],
            },
        }
    )
    execution = context()
    tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=FootballEvidenceCache(),
    )

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "UEFA Champions League",
            "season": "2026-27",
            "date": "2026-08-18",
            "team_a": "Levski Sofia",
            "team_b": "AEK Athens",
            "country": "Bulgaria",
        },
        execution,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "401909156"
    assert payload["completeness"] == "partial"
    assert execution.shared_state.football_base_evidence
    assert any(
        source["source"] == "thesportsdb:competition"
        and source["error_code"] == "thesportsdb_rate_limited"
        for source in payload["sources"]
    )
    assert any(
        source["source"] == "espn:qualifying_schedule"
        and source["status"] == "success"
        for source in payload["sources"]
    )
    assert not any("eventsday.php" in url for url in fetcher.urls)
    assert not any("eventsseason.php" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_uel_qualifying_fallback_accepts_sporttery_local_date_and_team_aliases() -> None:
    competitors = [
        {
            "homeAway": "home",
            "team": {"id": "2729", "displayName": "Mjällby AIF"},
        },
        {
            "homeAway": "away",
            "team": {"id": "2790", "displayName": "RB Salzburg"},
        },
    ]
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4480",
                        "strLeague": "UEFA Champions League",
                        "strCountry": "Europe",
                    }
                ]
            },
            "uefa.europa_qual/scoreboard": {
                "leagues": [{"slug": "uefa.europa_qual"}],
                "events": [
                    {
                        "id": "401909833",
                        "date": "2026-08-20T16:00Z",
                        "season": {"year": 2026, "slug": "playoff-round"},
                        "status": {"type": {"description": "Scheduled"}},
                        "competitions": [
                            {
                                "venue": {"fullName": "Strandvallen"},
                                "notes": [{"headline": "1st Leg"}],
                                "competitors": competitors,
                            }
                        ],
                    }
                ],
            },
            "uefa.europa_qual/summary": {
                "header": {
                    "league": {"slug": "uefa.europa_qual"},
                    "season": {
                        "name": "2026-27 UEFA Europa League Qualifying, Playoff Round"
                    },
                    "competitions": [
                        {
                            "date": "2026-08-20T16:00Z",
                            "status": {"type": {"description": "Scheduled"}},
                            "notes": [{"headline": "1st Leg"}],
                            "competitors": competitors,
                        }
                    ],
                },
                "gameInfo": {"venue": {"fullName": "Strandvallen"}},
                "boxscore": {"teams": []},
                "lastFiveGames": [
                    {
                        "team": {"displayName": "Mjällby AIF"},
                        "events": [
                            {
                                "id": "previous-1",
                                "gameDate": "2026-08-15T14:00Z",
                                "opponent": {"displayName": "IFK Göteborg"},
                                "gameResult": "W",
                                "score": "2-0",
                                "competitionName": "2026 Swedish Allsvenskan",
                            }
                        ],
                    },
                    {
                        "team": {"displayName": "RB Salzburg"},
                        "events": [
                            {
                                "id": "previous-2",
                                "gameDate": "2026-08-16T15:00Z",
                                "opponent": {"displayName": "Rapid Vienna"},
                                "gameResult": "W",
                                "score": "3-1",
                                "competitionName": "2026-27 Austrian Bundesliga",
                            }
                        ],
                    },
                ],
                "odds": [
                    {
                        "provider": {"name": "DraftKings"},
                        "details": "RBS -130",
                        "overUnder": 2.5,
                        "homeTeamOdds": {"moneyLine": 300},
                        "drawOdds": {"moneyLine": 300},
                        "awayTeamOdds": {"moneyLine": -130},
                    }
                ],
                "rosters": [
                    {"team": {"displayName": "Mjällby AIF"}},
                    {"team": {"displayName": "RB Salzburg"}},
                ],
                "headToHeadGames": [],
            },
        }
    )
    execution = context()
    tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=FootballEvidenceCache(),
    )

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "欧罗巴附加赛",
            "season": "2026-27",
            "date": "2026-08-21",
            "team_a": "Mjällby",
            "team_b": "Red Bull Salzburg",
        },
        execution,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "401909833"
    assert payload["fixture"]["date"] == "2026-08-20"
    assert payload["fixture"]["requested_date"] == "2026-08-21"
    assert payload["fixture"]["date_alignment"] == "adjacent_source_date"
    assert payload["fixture"]["espn_slug"] == "uefa.europa_qual"
    assert payload["fixture"]["stage"].endswith("Playoff Round")
    assert payload["fixture"]["leg"] == "1st Leg"
    assert payload["details"]["venue"] == "Strandvallen"
    assert payload["details"]["stage"].endswith("Playoff Round")
    assert payload["details"]["espn_odds"][0]["provider"] == "DraftKings"
    assert payload["details"]["espn_odds"][0]["decimal_odds"]["away"] == 1.769
    assert payload["form"][0]["events"][0]["result"] == "W"
    assert any(
        source["source"] == "espn:qualifying_schedule"
        and source["status"] == "success"
        for source in payload["sources"]
    )


@pytest.mark.asyncio
async def test_uel_qualifying_fallback_matches_agf_formal_name() -> None:
    competitors = [
        {
            "homeAway": "home",
            "team": {"id": "1929", "displayName": "Benfica"},
        },
        {
            "homeAway": "away",
            "team": {"id": "7853", "displayName": "AGF"},
        },
    ]
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {"countries": []},
            "uefa.europa_qual/scoreboard": {
                "leagues": [{"slug": "uefa.europa_qual"}],
                "events": [
                    {
                        "id": "401910978",
                        "date": "2026-08-20T19:00Z",
                        "status": {"type": {"description": "Scheduled"}},
                        "competitions": [
                            {
                                "venue": {"fullName": "Estadio da Luz"},
                                "competitors": competitors,
                            }
                        ],
                    }
                ],
            },
            "uefa.europa_qual/summary": {
                "header": {
                    "league": {"slug": "uefa.europa_qual"},
                    "competitions": [
                        {
                            "date": "2026-08-20T19:00Z",
                            "status": {"type": {"description": "Scheduled"}},
                            "competitors": competitors,
                        }
                    ],
                },
                "gameInfo": {"venue": {"fullName": "Estadio da Luz"}},
                "boxscore": {"form": []},
                "headToHeadGames": [],
            },
        }
    )
    tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=FootballEvidenceCache(),
    )

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "UEFA Europa League",
            "season": "2026-2027",
            "date": "2026-08-20",
            "team_a": "Benfica",
            "team_b": "Aarhus Gymnastikforening",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "401910978"
    assert payload["fixture"]["away"] == "AGF"
    assert payload["fixture"]["espn_slug"] == "uefa.europa_qual"


@pytest.mark.asyncio
async def test_source_requests_are_singleflight_within_one_root_execution() -> None:
    fetcher = SlowFixtureFetcher({"shared-schedule": {"events": []}})
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")
    execution = context()
    url = "https://example.test/shared-schedule"

    first, second, third = await asyncio.gather(
        tool._source_json(url, execution, source="fixture:first"),
        tool._source_json(url, execution, source="fixture:second"),
        tool._source_json(url, execution, source="fixture:third"),
    )

    assert fetcher.urls == [url]
    assert first.status == second.status == third.status == "success"
    assert (first.source, second.source, third.source) == (
        "fixture:first",
        "fixture:second",
        "fixture:third",
    )


@pytest.mark.asyncio
async def test_source_failure_is_shared_only_within_same_root_execution() -> None:
    fetcher = SlowFixtureFetcher(
        {
            "shared-schedule": ToolResult(
                content="rate limited",
                is_error=True,
                metadata={"errorCode": "web_http_429"},
            )
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")
    url = "https://example.test/shared-schedule"

    first, second = await asyncio.gather(
        tool._source_json(url, context(), source="thesportsdb:first"),
        tool._source_json(url, context(), source="thesportsdb:second"),
    )
    new_root = ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="run-2",
    )
    third = await tool._source_json(url, new_root, source="thesportsdb:third")

    assert len(fetcher.urls) == 2
    assert first.error_code == second.error_code == third.error_code == "thesportsdb_rate_limited"


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
async def test_context_reuses_base_evidence_across_execution_states() -> None:
    cache = FootballEvidenceCache()
    fetcher = FixtureFetcher(fixture_responses())
    data_tool = FootballDataTool(
        fetcher,
        espn_fetcher=fetcher,
        role_mode="data",
        cache=cache,
    )
    first = await data_tool.execute(
        {**evidence_arguments(), "action": "base_evidence"}, context()
    )
    assert not first.is_error

    fresh_context = ExecutionContext(
        user_id="user-1",
        agent_id="agent-2",
        session_id="new-session",
        root_execution_id="run-2",
    )
    result = await FootballContextTool(cache=cache).execute(
        {
            "competition": "瑞典超",
            "season": "2026",
            "date": "2026-08-10",
            "team_a": "IK Sirius",
            "team_b": "IF Brommapojkarna",
        },
        fresh_context,
    )

    assert not result.is_error
    assert result.metadata["cacheHit"] is True
    assert result.metadata["baseEvidenceKey"]
    assert fresh_context.shared_state.has_football_base()


@pytest.mark.asyncio
async def test_context_rejects_stale_cached_base_without_historical_context() -> None:
    cache = FootballEvidenceCache()
    key = football_event_key(
        "瑞典超",
        "2026",
        "2026-08-10",
        "IK Sirius",
        "IF Brommapojkarna",
    )
    await cache.put(
        "base",
        key,
        ToolResult(
            content=json.dumps(
                {
                    "fixture": {
                        "competition": "Swedish Allsvenskan",
                        "season": "2026",
                        "date": "2026-08-10",
                        "home": "IK Sirius",
                        "away": "IF Brommapojkarna",
                    }
                }
            )
        ),
    )

    result = await FootballContextTool(cache=cache).execute(
        {
            "competition": "瑞典超",
            "season": "2026",
            "date": "2026-08-10",
            "team_a": "IK Sirius",
            "team_b": "IF Brommapojkarna",
        },
        context(),
    )

    assert result.is_error
    assert result.metadata["errorCode"] == "football_context_not_ready"


@pytest.mark.asyncio
async def test_context_and_odds_resolve_team_aliases_for_the_same_fixture() -> None:
    shared = context()
    evidence_key = "dutcheredivisie|2026-2027|20260815|fcutrecht|azalkmaar"
    shared.shared_state.publish_football_base(
        evidence_key,
        json.dumps(
            {
                "fixture": {
                    "competition": "Dutch Eredivisie",
                    "season": "2026-2027",
                    "date": "2026-08-15",
                    "requested_date": "2026-08-15",
                    "home": "FC Utrecht",
                    "away": "AZ Alkmaar",
                },
                "base_evidence_key": evidence_key,
            }
        ),
    )

    context_result = await FootballContextTool().execute(
        {
            "competition": "荷甲",
            "season": "2026-27",
            "date": "2026-08-15",
            "team_a": "Utrecht",
            "team_b": "AZ Alkmaar",
        },
        shared,
    )
    assert not context_result.is_error
    assert context_result.metadata["baseEvidenceKey"] == evidence_key

    odds = FootballOddsTool(
        odds_source=StaticOdds(
            SourceResult(
                "the_odds_api",
                "success",
                [
                    {
                        "home_team": "Utrecht",
                        "away_team": "AZ Alkmaar",
                        "commence_time": "2026-08-15T16:45:00Z",
                    }
                ],
            )
        ),
        cache=FootballEvidenceCache(),
    )
    odds_result = await odds.execute(
        {
            "competition": "荷甲",
            "country": "Netherlands",
            "season": "2026-27",
            "date": "2026-08-15",
            "team_a": "Utrecht",
            "team_b": "AZ Alkmaar",
        },
        shared,
    )
    payload = json.loads(odds_result.content)
    assert not odds_result.is_error
    assert payload["base_evidence_key"] == evidence_key
    assert payload["source"]["status"] == "success"
    assert len(payload["odds"]) == 1


@pytest.mark.asyncio
async def test_odds_falls_back_to_confirmed_espn_summary_market() -> None:
    shared = context()
    evidence_key = "uefaeuropaleague|2026-2027|20260820|kairatalmaty|anderlecht"
    espn_market = {
        "provider": "DraftKings",
        "home_team": "Kairat Almaty",
        "away_team": "Anderlecht",
        "american_odds": {"home": 240, "draw": 240, "away": 110},
        "decimal_odds": {"home": 3.4, "draw": 3.4, "away": 2.1},
        "over_under": 2.5,
        "source": "espn:summary:odds",
    }
    shared.shared_state.publish_football_base(
        evidence_key,
        json.dumps(
            {
                "fixture": {
                    "competition": "UEFA Europa League",
                    "season": "2026-2027",
                    "date": "2026-08-20",
                    "requested_date": "2026-08-20",
                    "home": "Kairat Almaty",
                    "away": "Anderlecht",
                },
                "details": {"espn_odds": [espn_market]},
                "base_evidence_key": evidence_key,
            }
        ),
    )
    odds = FootballOddsTool(
        odds_source=StaticOdds(SourceResult("the_odds_api", "empty", [])),
        cache=FootballEvidenceCache(),
    )

    result = await odds.execute(
        {
            "competition": "UEFA Europa League",
            "season": "2026-2027",
            "date": "2026-08-20",
            "team_a": "Kairat Almaty",
            "team_b": "Anderlecht",
        },
        shared,
    )

    payload = json.loads(result.content)
    assert payload["source"]["source"] == "espn:summary:odds"
    assert payload["source"]["status"] == "success"
    assert payload["primary_source"]["status"] == "empty"
    assert payload["odds"][0]["decimal_odds"]["away"] == 2.1


@pytest.mark.asyncio
async def test_odds_match_accepts_real_racing_club_de_santander_alias() -> None:
    shared = context()
    evidence_key = "spanishlaliga|2026-2027|20260816|racingsantander|villarreal"
    shared.shared_state.publish_football_base(
        evidence_key,
        json.dumps(
            {
                "fixture": {
                    "competition": "Spanish LALIGA",
                    "season": "2026-2027",
                    "date": "2026-08-16",
                    "requested_date": "2026-08-16",
                    "home": "Racing de Santander",
                    "away": "Villarreal",
                },
                "base_evidence_key": evidence_key,
            }
        ),
    )
    odds = FootballOddsTool(
        odds_source=StaticOdds(
            SourceResult(
                "the_odds_api",
                "success",
                [
                    {
                        "home_team": "Real Racing Club de Santander",
                        "away_team": "Villarreal",
                        "commence_time": "2026-08-16T15:00:00Z",
                    }
                ],
            )
        ),
        cache=FootballEvidenceCache(),
    )

    result = await odds.execute(
        {
            "competition": "西甲",
            "country": "Spain",
            "season": "2026-27",
            "date": "2026-08-16",
            "team_a": "桑坦德竞技",
            "team_b": "比利亚雷亚尔",
        },
        shared,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"]["status"] == "success"
    assert len(payload["odds"]) == 1


@pytest.mark.asyncio
async def test_odds_match_uses_actual_date_when_request_date_is_adjacent() -> None:
    shared = context()
    evidence_key = "spanishlaliga|2026-2027|20260816|deportivoalaves|getafe"
    shared.shared_state.publish_football_base(
        evidence_key,
        json.dumps(
            {
                "fixture": {
                    "competition": "Spanish LALIGA",
                    "season": "2026-2027",
                    "date": "2026-08-15",
                    "requested_date": "2026-08-16",
                    "home": "Deportivo Alavés",
                    "away": "Getafe",
                },
                "base_evidence_key": evidence_key,
            }
        ),
    )
    odds = FootballOddsTool(
        odds_source=StaticOdds(
            SourceResult(
                "the_odds_api",
                "success",
                [
                    {
                        "home_team": "Deportivo Alaves",
                        "away_team": "Getafe",
                        "commence_time": "2026-08-15T19:00:00Z",
                    }
                ],
            )
        ),
        cache=FootballEvidenceCache(),
    )

    result = await odds.execute(
        {
            "competition": "西甲",
            "country": "Spain",
            "season": "2026-27",
            "date": "2026-08-16",
            "team_a": "Deportivo Alavés",
            "team_b": "Getafe",
        },
        shared,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"]["status"] == "success"
    assert len(payload["odds"]) == 1


@pytest.mark.asyncio
async def test_season_alias_and_full_schedule_fallback_confirm_adjacent_source_date() -> None:
    event = {
        "idEvent": "2506172",
        "strLeague": "Spanish La Liga",
        "strSeason": "2026-2027",
        "strEvent": "Sevilla vs Rayo Vallecano",
        "strHomeTeam": "Sevilla",
        "strAwayTeam": "Rayo Vallecano",
        "dateEvent": "2026-08-15",
        "strTime": "19:30:00",
        "strVenue": "Estadio Ramón Sánchez Pizjuán",
        "intRound": "1",
    }
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4335",
                        "strLeague": "Spanish La Liga",
                        "strLeagueAlternate": "La Liga",
                        "strCountry": "Spain",
                    }
                ]
            },
            "eventsday.php": {"events": []},
            "eventsseason.php": {"events": [event]},
            "lookuptable.php": {"table": []},
            "scoreboard": {"leagues": [{"slug": "esp.1"}], "events": []},
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "西甲",
            "country": "Spain",
            "season": "2026-27",
            "date": "2026-08-16",
            "team_a": "Sevilla",
            "team_b": "Rayo Vallecano",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "2506172"
    assert payload["fixture"]["date"] == "2026-08-15"
    assert payload["fixture"]["requested_date"] == "2026-08-16"
    assert payload["fixture"]["date_alignment"] == "adjacent_source_date"
    assert "source_date_adjacent_to_requested_date" in payload["warnings"]
    assert any("eventsseason.php?id=4335&s=2026-2027" in url for url in fetcher.urls)


@pytest.mark.asyncio
async def test_spanish_provider_team_aliases_match_chinese_fixture_request() -> None:
    event = {
        "idEvent": "2506200",
        "strLeague": "Spanish La Liga",
        "strSeason": "2026-2027",
        "strEvent": "RCD Espanyol vs Levante UD",
        "strHomeTeam": "RCD Espanyol",
        "strAwayTeam": "Levante UD",
        "idHomeTeam": "100",
        "idAwayTeam": "101",
        "dateEvent": "2026-08-16",
        "strTime": "23:00:00",
        "strVenue": "RCDE Stadium",
        "intRound": "1",
    }
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4335",
                        "strLeague": "Spanish La Liga",
                        "strLeagueAlternate": "La Liga",
                        "strCountry": "Spain",
                    }
                ]
            },
            "eventsday.php": {"events": []},
            "eventsseason.php": {"events": [event]},
            "lookuptable.php": {"table": []},
            "scoreboard": {"leagues": [{"slug": "esp.1"}], "events": []},
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "西甲",
            "country": "Spain",
            "season": "2026-27",
            "date": "2026-08-16",
            "team_a": "西班牙人",
            "team_b": "莱万特",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "2506200"
    assert payload["fixture"]["home"] == "RCD Espanyol"
    assert payload["fixture"]["away"] == "Levante UD"
    assert payload["fixture"]["home_team_id"] == "100"
    assert payload["fixture"]["away_team_id"] == "101"
    assert payload["fixture"]["home_canonical_id"] == "espanyol"
    assert payload["fixture"]["away_canonical_id"] == "levante"


@pytest.mark.asyncio
async def test_new_season_historical_context_separates_friendlies_and_prior_division() -> None:
    event = {
        "idEvent": "2506201",
        "strLeague": "Spanish La Liga",
        "strSeason": "2026-2027",
        "strEvent": "Racing de Santander vs Villarreal",
        "strHomeTeam": "Racing de Santander",
        "strAwayTeam": "Villarreal",
        "idHomeTeam": "133726",
        "idAwayTeam": "133740",
        "dateEvent": "2026-08-16",
        "strTime": "15:00:00",
        "strVenue": "Campos de Sport de El Sardinero",
        "intRound": "1",
    }
    fetcher = HistoricalFixtureFetcher(
        {
            "search_all_leagues.php": {
                "countries": [
                    {
                        "idLeague": "4335",
                        "strLeague": "Spanish La Liga",
                        "strLeagueAlternate": "La Liga",
                        "strCountry": "Spain",
                    }
                ]
            },
            "eventsday.php": {"events": [event]},
            "eventsseason.php?id=4335&s=2026-2027": {"events": [event]},
            "lookuptable.php": {"table": []},
            "eventslast.php": {
                "results": [
                    {
                        **event,
                        "idEvent": "2527472",
                        "strLeague": "Club Friendlies",
                        "strSeason": "2026",
                        "strEvent": "Racing de Santander vs Deportivo Alavés",
                        "strHomeTeam": "Racing de Santander",
                        "strAwayTeam": "Deportivo Alavés",
                        "idAwayTeam": "134221",
                        "dateEvent": "2026-08-07",
                        "intHomeScore": "1",
                        "intAwayScore": "1",
                        "strStatus": "PEN",
                    }
                ]
            },
            "lookupevent.php": {"events": []},
            "scoreboard": {"leagues": [{"slug": "esp.1"}], "events": []},
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "西甲",
            "country": "Spain",
            "season": "2026-27",
            "date": "2026-08-16",
            "team_a": "桑坦德竞技",
            "team_b": "比利亚雷亚尔",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    racing = payload["historical_context"]["teams"][0]
    assert racing["competition_boundary"] == "source_supported_prior_division"
    assert racing["prior_division_matches"][0]["competition"] == "Spanish La Liga 2"
    assert racing["preseason_friendlies"][0]["competition"] == "Club Friendlies"
    assert any(
        "preseason_friendlies are separate weak context" in rule
        for rule in payload["historical_context"]["interpretation_rules"]
    )


@pytest.mark.asyncio
async def test_primary_and_espn_success_preserve_primary_fixture_identity() -> None:
    result, _fetcher = await run_evidence()
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["fixture"]["event_id"] == "21001"
    assert payload["details"]["source_event_id"] == "401842783"
    assert payload["fixture"]["venue"] == "Studenternas IP"
    assert payload["form"][0]["scope"] == "cross_competition_may_cross_season"
    assert payload["head_to_head"]
    assert payload["previous_match_details"][0]["details"]["lineups"]["home"]
    assert {item["status"] for item in payload["sources"]} == {"success"}


@pytest.mark.asyncio
async def test_h2h_search_finds_prior_season_match_when_recent_events_omit_opponent() -> None:
    current = {
        **PRIMARY_EVENT,
        "idHomeTeam": "100",
        "idAwayTeam": "101",
        "strStatus": "NS",
    }
    recent = {
        **current,
        "idEvent": "21002",
        "strAwayTeam": "A different opponent",
        "idAwayTeam": "999",
        "dateEvent": "2026-08-05",
        "intHomeScore": "1",
        "intAwayScore": "0",
        "strStatus": "FT",
    }
    prior_h2h = {
        **current,
        "idEvent": "21003",
        "strSeason": "2025",
        "dateEvent": "2025-09-10",
        "intHomeScore": "2",
        "intAwayScore": "1",
        "strStatus": "FT",
    }
    responses = fixture_responses()
    responses.update(
        {
            "eventsday.php": {"events": [current]},
            "eventsseason.php": {"events": [current]},
            "eventslast.php": {"results": [recent]},
            "searchevents.php": {"event": [prior_h2h]},
        }
    )

    result, fetcher = await run_evidence(responses)
    payload = json.loads(result.content)

    assert not result.is_error
    assert [event["event_id"] for event in payload["head_to_head"]] == ["21003"]
    assert any("searchevents.php" in url and "s=2025" in url for url in fetcher.urls)
    assert any(
        source["source"] == "thesportsdb:h2h_search"
        for source in payload["sources"]
    )


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
async def test_odds_success_without_requested_fixture_is_no_match() -> None:
    result, fetcher = await run_evidence(
        odds=SourceResult(
            "the_odds_api",
            "success",
            [
                {
                    "home_team": "A different home",
                    "away_team": "A different away",
                    "commence_time": "2026-08-10T17:00:00Z",
                }
            ],
        )
    )
    payload = json.loads(result.content)

    assert not result.is_error
    assert payload["odds"] == {"status": "no_match"}
    source = next(item for item in payload["sources"] if item["source"] == "the_odds_api")
    assert source["status"] == "no_match"
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
    responses["scoreboard"] = ToolResult(content="offline", is_error=True)
    result, _fetcher = await run_evidence(responses)
    payload = json.loads(result.content)

    assert result.is_error
    assert result.metadata["errorCode"] == "football_primary_unavailable"
    assert payload["fixture"] is None
    assert payload["completeness"] == "unavailable"
    assert any("do not infer facts" in item for item in payload["warnings"])


@pytest.mark.asyncio
async def test_primary_espn_fixture_confirms_match_when_tsdb_league_is_unavailable() -> None:
    teams = [
        {
            "homeAway": "home",
            "team": {"id": "359", "displayName": "Arsenal"},
        },
        {
            "homeAway": "away",
            "team": {"id": "388", "displayName": "Coventry City"},
        },
    ]
    event = {
        "id": "401879301",
        "name": "Coventry City at Arsenal",
        "date": "2026-08-21T19:00Z",
        "season": {
            "name": "2026-27 English Premier League",
            "slug": "2026-27-english-premier-league",
        },
        "status": {"type": {"description": "Scheduled"}},
        "competitions": [
            {
                "date": "2026-08-21T19:00Z",
                "venue": {"fullName": "Emirates Stadium"},
                "competitors": teams,
            }
        ],
    }
    fetcher = FixtureFetcher(
        {
            "search_all_leagues.php": {"countries": []},
            "eng.1/scoreboard": {
                "leagues": [{"slug": "eng.1"}],
                "events": [event],
            },
            "eng.1/summary": {
                "header": {
                    "league": {"slug": "eng.1"},
                    "season": event["season"],
                    "competitions": event["competitions"],
                },
                "gameInfo": {"venue": {"fullName": "Emirates Stadium"}},
                "lastFiveGames": [],
                "headToHeadGames": [],
            },
        }
    )
    tool = FootballDataTool(fetcher, espn_fetcher=fetcher, role_mode="data")

    result = await tool.execute(
        {
            "action": "base_evidence",
            "competition": "English Premier League",
            "season": "2026-27",
            "date": "2026-08-21",
            "team_a": "Arsenal",
            "team_b": "Coventry City",
        },
        context(),
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["fixture"]["event_id"] == "401879301"
    assert payload["fixture"]["competition"] == "English Premier League"
    assert payload["fixture"]["home"] == "Arsenal"
    assert payload["fixture"]["away"] == "Coventry City"
    assert payload["fixture"]["date"] == "2026-08-21"
    assert payload["fixture"]["time_utc"] == "19:00:00"
    assert payload["fixture"]["venue"] == "Emirates Stadium"
    assert "primary_fixture_confirmed_by_espn_primary" in payload["warnings"]
    assert any(
        source["source"] == "espn:primary_schedule" and source["status"] == "success"
        for source in payload["sources"]
    )


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
    assert skill.resolve_competition("欧冠")["odds_sport_keys"] == (
        "soccer_uefa_champs_league",
        "soccer_uefa_champs_league_qualification",
    )
    assert (
        skill.resolve_competition("UEFA Champions League Qualification")["competition"]
        == "UEFA Champions League"
    )
    assert skill.resolve_competition("欧冠附加赛")["competition"] == "UEFA Champions League"
    assert (
        skill.resolve_competition("UEFA Europa League Qualifying")["competition"]
        == "UEFA Europa League"
    )
    assert skill.resolve_competition("欧罗巴附加赛")["espn_slug"] == "uefa.europa"
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
            ([], {"requests_remaining": "497", "requests_used": "3"}),
            (
                [{"key": "soccer_sweden_allsvenskan", "active": True}],
                {"requests_remaining": "497"},
            ),
            ([], {"requests_remaining": "496", "requests_used": "4"}),
        ]
    )
    monkeypatch.setattr(odds, "_request", lambda *_args, **_kwargs: next(calls))
    catalog_omitted = odds.fetch_odds("瑞典超")
    success = odds.fetch_odds("瑞典超")

    assert catalog_omitted["status"] == "empty"
    assert catalog_omitted["quota"]["requests_remaining"] == "497"
    assert success["status"] == "empty"
    assert success["quota"]["requests_remaining"] == "496"
    assert success["quota"]["requests_used"] == "4"


def test_bundled_odds_routes_ucl_to_active_qualification_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    odds = _load_skill_module("bundled_ucl_odds_data", "odds_data.py")
    monkeypatch.setenv("ODDS_API_KEY", "fixture-value")
    paths: list[str] = []

    def request(path: str, _params: dict[str, str], _key: str) -> tuple[object, dict]:
        paths.append(path)
        if path == "sports":
            return (
                [
                    {
                        "key": "soccer_uefa_champs_league_qualification",
                        "active": True,
                    }
                ],
                {"requests_remaining": "345"},
            )
        return (
            [
                {
                    "home_team": "PFC Levski Sofia",
                    "away_team": "AEK Athens",
                }
            ],
            {"requests_remaining": "344"},
        )

    monkeypatch.setattr(odds, "_request", request)

    result = odds.fetch_odds("欧冠")

    assert result["status"] == "success"
    assert paths == [
        "sports",
        "sports/soccer_uefa_champs_league_qualification/odds",
    ]


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
async def test_runtime_odds_probes_reviewed_route_when_catalog_omits_it() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v4/sports":
            return httpx.Response(
                200,
                json=[{"key": "soccer_uefa_nations_league", "active": True}],
            )
        assert request.url.path == "/v4/sports/soccer_uefa_europa_league/odds"
        return httpx.Response(
            200,
            json=[],
            headers={"x-requests-remaining": "305", "x-requests-used": "195"},
        )

    source = TheOddsApiSource("fixture-value", transport=httpx.MockTransport(handler))
    competition = next(
        item for item in FOOTBALL_COMPETITIONS if item.key == "uefa-europa-league"
    )
    result = await source.fetch(
        competition,
        regions=("eu",),
        markets=("h2h",),
        odds_format="decimal",
        commence_from="2026-08-20T00:00:00Z",
        commence_to="2026-08-21T23:59:59Z",
    )

    assert result.status == "empty"
    assert result.error_code is None
    assert requested_paths == [
        "/v4/sports",
        "/v4/sports/soccer_uefa_europa_league/odds",
    ]


@pytest.mark.asyncio
async def test_ucl_odds_routes_to_active_qualification_key_and_matches_pfc_prefix() -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == "/v4/sports":
            return httpx.Response(
                200,
                json=[
                    {
                        "key": "soccer_uefa_champs_league_qualification",
                        "active": True,
                    }
                ],
            )
        assert request.url.path == (
            "/v4/sports/soccer_uefa_champs_league_qualification/odds"
        )
        return httpx.Response(
            200,
            json=[
                {
                    "id": "9ccf2ce01c868371156182f1cdb55b10",
                    "home_team": "PFC Levski Sofia",
                    "away_team": "AEK Athens",
                    "commence_time": "2026-08-18T19:00:00Z",
                    "bookmakers": [],
                }
            ],
            headers={"x-requests-remaining": "344", "x-requests-used": "156"},
        )

    shared = context()
    evidence_key = "uefachampionsleague|2026-2027|20260818|levskisofia|aekathens"
    shared.shared_state.publish_football_base(
        evidence_key,
        json.dumps(
            {
                "fixture": {
                    "competition": "UEFA Champions League",
                    "season": "2026-2027",
                    "date": "2026-08-18",
                    "requested_date": "2026-08-18",
                    "home": "Levski Sofia",
                    "away": "AEK Athens",
                },
                "base_evidence_key": evidence_key,
            }
        ),
    )
    tool = FootballOddsTool(
        odds_source=TheOddsApiSource(
            "fixture-value", transport=httpx.MockTransport(handler)
        ),
        cache=FootballEvidenceCache(),
    )

    result = await tool.execute(
        {
            "competition": "UEFA Champions League",
            "season": "2026-27",
            "date": "2026-08-18",
            "team_a": "Levski Sofia",
            "team_b": "AEK Athens",
            "regions": ["eu"],
            "markets": ["h2h", "totals"],
        },
        shared,
    )

    payload = json.loads(result.content)
    assert not result.is_error
    assert payload["source"]["status"] == "success"
    assert payload["odds"][0]["home_team"] == "PFC Levski Sofia"
    assert requested_paths == [
        "/v4/sports",
        "/v4/sports/soccer_uefa_champs_league_qualification/odds",
    ]


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

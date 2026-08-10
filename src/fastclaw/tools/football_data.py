"""Competition-aware football data retrieval built on the pinned Web fetcher."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    FOOTBALL_COMPETITIONS,
    resolve_football_competition,
)

_SPORTS_DB = "https://www.thesportsdb.com/api/v1/json/123"
_SPORTTERY = (
    "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?channel=c"
)


class _Fetcher(Protocol):
    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult: ...


class FootballDataTool:
    """Resolve competitions and fetch fixtures without a World Cup hard-code."""

    def __init__(self, fetcher: _Fetcher) -> None:
        self._fetcher = fetcher
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_data",
                description=(
                    "Resolve a football competition and fetch dated fixtures, results, "
                    "standings, team form, head-to-head, or public Sporttery match prices. "
                    "Call competition_resolve before using an ESPN competition. Call "
                    "competition_search with competition and country when a TheSportsDB "
                    "league_id is unknown."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [
                                "competition_resolve",
                                "competition_search",
                                "schedule",
                                "results",
                                "standings",
                                "form",
                                "h2h",
                                "sporttery_match",
                            ],
                        },
                        "competition": {"type": "string"},
                        "country": {"type": "string"},
                        "league_id": {"type": "string"},
                        "date": {"type": "string"},
                        "season": {"type": "string"},
                        "team": {"type": "string"},
                        "team_a": {"type": "string"},
                        "team_b": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        action = str(arguments.get("action") or "")
        if action == "competition_resolve":
            return self._competition_resolve(arguments)
        if action == "competition_search":
            return await self._competition_search(arguments, context)
        if action in {"schedule", "results", "standings"}:
            return await self._competition_data(action, arguments, context)
        if action == "form":
            return await self._form(arguments, context)
        if action == "h2h":
            return await self._h2h(arguments, context)
        if action == "sporttery_match":
            return await self._sporttery_match(arguments, context)
        return self._error("unsupported football_data action")

    def _competition_resolve(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("competition") or "").strip()
        if not query:
            return self._error("competition_resolve requires competition")
        country = str(arguments.get("country") or "").strip()
        competition = resolve_football_competition(query, country=country)
        if competition is None:
            return self._error(
                "competition is not in the trusted runtime catalog; ask for a supported "
                "competition or add a reviewed mapping"
            )
        return self._success(
            {
                "source": "fastclaw:football_competitions",
                "mapping": competition.public_mapping(),
                "provider_identifiers_are_trusted": True,
                "catalog_size": len(FOOTBALL_COMPETITIONS),
            }
        )

    async def _competition_search(
        self, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        query = str(arguments.get("competition") or "").strip()
        if not query:
            return self._error("competition_search requires competition")
        country = str(arguments.get("country") or "").strip()
        params = {"s": "Soccer", **({"c": country} if country else {})}
        data, error = await self._get_json(
            f"{_SPORTS_DB}/search_all_leagues.php?{urlencode(params)}", context
        )
        if error is not None:
            return error
        candidates = data.get("countries") or data.get("leagues") or []
        needle = query.casefold()
        rows = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            haystack = " ".join(
                str(item.get(key) or "")
                for key in ("strLeague", "strLeagueAlternate", "strCountry")
            ).casefold()
            if needle not in haystack:
                continue
            rows.append(
                {
                    "league_id": item.get("idLeague"),
                    "competition": item.get("strLeague"),
                    "alternate": item.get("strLeagueAlternate"),
                    "country": item.get("strCountry"),
                }
            )
        return self._success(
            {
                "source": "thesportsdb:search_all_leagues",
                "query": query,
                "country": country,
                "count": len(rows[:20]),
                "competitions": rows[:20],
                "coverage": (
                    "country-scoped free API result"
                    if country
                    else "free API result may be truncated; retry with country"
                ),
            }
        )

    async def _competition_data(
        self, action: str, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        league_id = str(arguments.get("league_id") or "").strip()
        if not league_id:
            return self._error(f"{action} requires league_id from competition_search")
        limit = self._limit(arguments, 80)
        if action == "schedule":
            date = str(arguments.get("date") or "").strip()
            if not date:
                return self._error("schedule requires date in YYYY-MM-DD format")
            path = "eventsday.php?" + urlencode({"d": date, "l": league_id})
            key = "events"
        elif action == "results":
            season = str(arguments.get("season") or "").strip()
            if not season:
                return self._error("results requires season")
            path = "eventsseason.php?" + urlencode({"id": league_id, "s": season})
            key = "events"
        else:
            season = str(arguments.get("season") or "").strip()
            if not season:
                return self._error("standings requires season")
            path = "lookuptable.php?" + urlencode({"l": league_id, "s": season})
            key = "table"
        data, error = await self._get_json(f"{_SPORTS_DB}/{path}", context)
        if error is not None:
            return error
        raw_rows = data.get(key) or []
        rows = [
            self._standing_row(row) if action == "standings" else self._event_row(row)
            for row in raw_rows[:limit]
            if isinstance(row, dict)
        ]
        return self._success(
            {
                "source": f"thesportsdb:{path.split('?', 1)[0]}",
                "action": action,
                "league_id": league_id,
                "count": len(rows),
                "rows": rows,
            }
        )

    async def _form(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        team = str(arguments.get("team") or "").strip()
        if not team:
            return self._error("form requires team")
        team_id, error = await self._team_id(team, context)
        if error is not None:
            return error
        data, error = await self._get_json(
            f"{_SPORTS_DB}/eventslast.php?{urlencode({'id': team_id})}", context
        )
        if error is not None:
            return error
        rows = [
            self._event_row(row) for row in (data.get("results") or []) if isinstance(row, dict)
        ]
        limit = self._limit(arguments, 8)
        return self._success(
            {
                "source": "thesportsdb:eventslast",
                "team": team,
                "team_id": team_id,
                "count": len(rows[:limit]),
                "matches": rows[:limit],
                "coverage": "recent matches may span multiple competitions",
            }
        )

    async def _h2h(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not team_a or not team_b:
            return self._error("h2h requires team_a and team_b")
        team_id, error = await self._team_id(team_a, context)
        if error is not None:
            return error
        data, error = await self._get_json(
            f"{_SPORTS_DB}/eventslast.php?{urlencode({'id': team_id})}", context
        )
        if error is not None:
            return error
        needle = team_b.casefold()
        rows = [
            self._event_row(row)
            for row in data.get("results") or []
            if isinstance(row, dict) and needle in str(row.get("strEvent") or "").casefold()
        ]
        limit = self._limit(arguments, 10)
        return self._success(
            {
                "source": "thesportsdb:eventslast",
                "team_a": team_a,
                "team_b": team_b,
                "count": len(rows[:limit]),
                "matches": rows[:limit],
                "coverage": "free endpoint exposes only recent events; an empty result is unknown",
            }
        )

    async def _sporttery_match(
        self, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not team_a or not team_b:
            return self._error("sporttery_match requires team_a and team_b")
        data, error = await self._get_json(_SPORTTERY, context)
        if error is not None:
            return error
        needles = (team_a.casefold(), team_b.casefold())
        for day in (data.get("value") or {}).get("matchInfoList") or []:
            for match in day.get("subMatchList") or []:
                haystack = " ".join(
                    str(match.get(key) or "")
                    for key in (
                        "homeTeamAllName",
                        "homeTeamAbbName",
                        "homeTeamAbbEnName",
                        "awayTeamAllName",
                        "awayTeamAbbName",
                        "awayTeamAbbEnName",
                    )
                ).casefold()
                if not all(needle in haystack for needle in needles):
                    continue
                return self._success(
                    {
                        "source": "sporttery:getMatchCalculatorV1",
                        "match_id": match.get("matchId"),
                        "competition": match.get("leagueAllName") or match.get("leagueAbbName"),
                        "home": match.get("homeTeamAllName"),
                        "away": match.get("awayTeamAllName"),
                        "match_date": match.get("matchDate"),
                        "match_time": match.get("matchTime"),
                        "had": match.get("had"),
                        "hhad": match.get("hhad"),
                    }
                )
        return self._success(
            {
                "source": "sporttery:getMatchCalculatorV1",
                "match": f"{team_a} vs {team_b}",
                "found": False,
                "coverage": "not found in the currently offered Sporttery fixture list",
            }
        )

    async def _team_id(self, team: str, context: ExecutionContext) -> tuple[str, ToolResult | None]:
        data, error = await self._get_json(
            f"{_SPORTS_DB}/searchteams.php?{urlencode({'t': team})}", context
        )
        if error is not None:
            return "", error
        teams = data.get("teams") or []
        if not teams or not teams[0].get("idTeam"):
            return "", self._error(f"team not found: {team}")
        return str(teams[0]["idTeam"]), None

    async def _get_json(
        self, url: str, context: ExecutionContext
    ) -> tuple[dict[str, Any], ToolResult | None]:
        fetched = await self._fetcher.execute({"url": url}, context)
        if fetched.is_error:
            return {}, fetched
        try:
            data = json.loads(fetched.content)
        except json.JSONDecodeError:
            return {}, self._error("football data source returned malformed JSON")
        if not isinstance(data, dict):
            return {}, self._error("football data source returned an unexpected payload")
        return data, None

    @staticmethod
    def _limit(arguments: dict[str, Any], default: int) -> int:
        value = arguments.get("limit")
        return max(1, min(int(value), 100)) if isinstance(value, int) else default

    @staticmethod
    def _event_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "event_id": row.get("idEvent"),
            "competition": row.get("strLeague"),
            "season": row.get("strSeason"),
            "match": row.get("strEvent"),
            "home": row.get("strHomeTeam"),
            "away": row.get("strAwayTeam"),
            "home_score": row.get("intHomeScore"),
            "away_score": row.get("intAwayScore"),
            "date": row.get("dateEvent"),
            "time_utc": row.get("strTime"),
            "round": row.get("intRound"),
            "venue": row.get("strVenue"),
            "status": row.get("strStatus"),
        }

    @staticmethod
    def _standing_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "rank": row.get("intRank"),
            "team": row.get("strTeam"),
            "played": row.get("intPlayed"),
            "win": row.get("intWin"),
            "draw": row.get("intDraw"),
            "loss": row.get("intLoss"),
            "goal_difference": row.get("intGoalDifference"),
            "points": row.get("intPoints"),
        }

    @staticmethod
    def _success(data: dict[str, Any]) -> ToolResult:
        payload = {"as_of": datetime.now(UTC).isoformat(), **data}
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _error(message: str) -> ToolResult:
        return ToolResult(
            content=json.dumps({"error": message}, ensure_ascii=False),
            is_error=True,
            metadata={"errorCode": "football_data_error"},
        )

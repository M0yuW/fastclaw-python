"""Competition-aware football data retrieval built on the pinned Web fetcher."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    FOOTBALL_COMPETITIONS,
    FootballCompetition,
    normalize_competition_name,
    resolve_football_competition,
)
from fastclaw.tools.football_evidence import (
    OddsSource,
    SourceResult,
    TheOddsApiSource,
    utc_now,
)
from fastclaw.tools.football_shared import (
    FootballEvidenceCache,
    football_event_key,
    get_football_evidence_cache,
)

_SPORTS_DB = "https://www.thesportsdb.com/api/v1/json/123"
_ESPN_SOCCER = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_SPORTTERY = (
    "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?channel=c"
)


class _Fetcher(Protocol):
    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult: ...


class EspnPublicFetcher:
    """Fetch only Runtime-constructed ESPN soccer URLs with trusted system curl."""

    _executables: tuple[Path, ...] = (Path("/usr/bin/curl"), Path("/bin/curl"))

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        url = str(arguments.get("url") or "")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            port = -1
        trusted_prefix = "/apis/site/v2/sports/soccer/"
        if (
            parsed.scheme != "https"
            or parsed.hostname != "site.api.espn.com"
            or port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.path.startswith(trusted_prefix)
        ):
            return ToolResult(
                content="ESPN URL is outside the fixed soccer API origin",
                is_error=True,
                metadata={"errorCode": "espn_url_rejected"},
            )
        executable = next((path for path in self._executables if path.is_file()), None)
        if executable is None:
            return ToolResult(
                content="ESPN supplemental source is not ready",
                is_error=True,
                metadata={"errorCode": "espn_curl_unavailable", "ready": False},
            )
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "25",
            "--max-filesize",
            "2000000",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-redirs",
            "0",
            url,
            env={"PATH": "/usr/bin:/bin"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            return ToolResult(
                content="ESPN supplemental source timed out",
                is_error=True,
                metadata={"errorCode": "espn_timeout"},
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        except BaseException:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            raise
        if process.returncode != 0:
            return ToolResult(
                content="ESPN request failed",
                is_error=True,
                metadata={"errorCode": "espn_request_failed"},
            )
        if len(stdout) > 2_000_000:
            return ToolResult(
                content="ESPN response exceeded the safety limit",
                is_error=True,
                metadata={"errorCode": "espn_response_too_large"},
            )
        return ToolResult(content=stdout.decode("utf-8", errors="replace"))


class FootballDataTool:
    """Resolve competitions and fetch fixtures without a World Cup hard-code."""

    def __init__(
        self,
        fetcher: _Fetcher,
        *,
        espn_fetcher: _Fetcher | None = None,
        odds_source: OddsSource | None = None,
        allow_sporttery: bool = False,
        role_mode: str = "full",
        cache: FootballEvidenceCache | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._espn_fetcher = espn_fetcher or EspnPublicFetcher()
        self._odds_source = odds_source or TheOddsApiSource.from_environment()
        self._allow_sporttery = allow_sporttery
        self._role_mode = role_mode
        self._cache = cache or get_football_evidence_cache()
        description = (
            "Resolve a football competition and fetch dated fixtures, results, standings, "
            "team form, head-to-head, or an evidence bundle. Provider identifiers and URLs "
            "are selected only by the Runtime."
        )
        if role_mode == "data":
            description = (
                "Fetch one cached base evidence bundle for the confirmed football fixture. "
                "This data-only action uses TheSportsDB and ESPN supplements and never calls "
                "The Odds API or Sporttery."
            )
        elif role_mode == "ev":
            description = (
                "Fetch official Sporttery prices only when the user explicitly requests EV or "
                "market analysis for the confirmed fixture."
            )
        actions = [
            "competition_resolve",
            "competition_search",
            "espn_schedule",
            "espn_summary",
            "schedule",
            "results",
            "standings",
            "form",
            "h2h",
        ]
        if role_mode == "data":
            actions = ["base_evidence"]
            # Existing persisted prompts may still name the old action. Keep
            # it as a compatibility alias routed to the same data-only path.
            actions.append("evidence")
        elif role_mode == "ev":
            actions = ["sporttery_match"]
        else:
            actions.append("evidence")
        if allow_sporttery and "sporttery_match" not in actions:
            actions.append("sporttery_match")
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_data",
                description=description,
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": actions,
                        },
                        "competition": {"type": "string"},
                        "country": {"type": "string"},
                        "event_id": {"type": "string"},
                        "date": {"type": "string"},
                        "season": {"type": "string"},
                        "team": {"type": "string"},
                        "team_a": {"type": "string"},
                        "team_b": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "regions": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["us", "uk", "eu", "au"]},
                            "maxItems": 4,
                        },
                        "markets": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["h2h", "totals"]},
                            "maxItems": 2,
                        },
                        "odds_format": {
                            "type": "string",
                            "enum": ["decimal", "american"],
                        },
                        "commence_from": {"type": "string"},
                        "commence_to": {"type": "string"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        forbidden = {"url", "apiKey", "apikey", "sport_key", "espn_slug", "league_id"}
        if forbidden.intersection(arguments):
            return self._error("provider URLs, credentials, and identifiers are Runtime-managed")
        action = str(arguments.get("action") or "")
        if self._role_mode == "data" and action not in {"base_evidence", "evidence"}:
            return self._error("data analyst may only request the shared base_evidence bundle")
        if self._role_mode == "ev" and not context.shared_state.ev_requested:
            return self._error("EV market lookup requires an explicit user request")
        if self._role_mode == "ev" and action != "sporttery_match":
            return self._error("EV analyst may only request Sporttery market prices")
        if action == "competition_resolve":
            return self._competition_resolve(arguments)
        if action == "competition_search":
            return await self._competition_search(arguments, context)
        if action == "espn_schedule":
            return await self._espn_schedule(arguments, context)
        if action == "espn_summary":
            return await self._espn_summary(arguments, context)
        if action in {"schedule", "results", "standings"}:
            return await self._competition_data(action, arguments, context)
        if action == "form":
            return await self._form(arguments, context)
        if action == "h2h":
            return await self._h2h(arguments, context)
        if action == "sporttery_match":
            if not self._allow_sporttery:
                return self._error("sporttery_match is restricted to the EV analyst role")
            return await self._sporttery_match(arguments, context)
        if action == "base_evidence":
            return await self._evidence(arguments, context, include_odds=False)
        if action == "evidence":
            return await self._evidence(arguments, context, include_odds=self._role_mode != "data")
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

    async def _espn_schedule(
        self, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        competition, error = self._trusted_competition(arguments)
        if error is not None:
            return error
        assert competition is not None
        date = str(arguments.get("date") or "").strip()
        try:
            date_key = datetime.strptime(date, "%Y-%m-%d").strftime("%Y%m%d")
        except ValueError:
            return self._error("espn_schedule requires date in YYYY-MM-DD format")
        data, error = await self._get_json(
            f"{_ESPN_SOCCER}/{competition.espn_slug}/scoreboard?" + urlencode({"dates": date_key}),
            context,
            fetcher=self._espn_fetcher,
        )
        if error is not None:
            return error
        mismatch = self._validate_espn_league(data, competition.espn_slug)
        if mismatch is not None:
            return mismatch
        events = data.get("events") or []
        rows = [
            self._espn_event_row(event)
            for event in events[: self._limit(arguments, 80)]
            if isinstance(event, dict)
        ]
        return self._success(
            {
                "source": "espn:scoreboard",
                "mapping": competition.public_mapping(),
                "date": date,
                "count": len(rows),
                "matches": rows,
            }
        )

    async def _espn_summary(
        self, arguments: dict[str, Any], context: ExecutionContext
    ) -> ToolResult:
        competition, error = self._trusted_competition(arguments)
        if error is not None:
            return error
        assert competition is not None
        event_id = str(arguments.get("event_id") or "").strip()
        if not event_id or not event_id.isdigit():
            return self._error("espn_summary requires a numeric event_id")
        data, error = await self._get_json(
            f"{_ESPN_SOCCER}/{competition.espn_slug}/summary?" + urlencode({"event": event_id}),
            context,
            fetcher=self._espn_fetcher,
        )
        if error is not None:
            return error
        mismatch = self._validate_espn_league(data, competition.espn_slug)
        if mismatch is not None:
            return mismatch
        header = data.get("header") or {}
        event = (header.get("competitions") or [{}])[0]
        teams = self._espn_competitors(event)
        game_info = data.get("gameInfo") or {}
        return self._success(
            {
                "source": "espn:summary",
                "mapping": competition.public_mapping(),
                "event_id": event_id,
                "date_utc": event.get("date"),
                "status": ((event.get("status") or {}).get("type") or {}).get("description"),
                "venue": (game_info.get("venue") or {}).get("fullName"),
                "attendance": game_info.get("attendance"),
                "teams": teams,
                "form": self._espn_form(data),
                "odds": self._espn_odds(data),
                "head_to_head": self._espn_head_to_head(data),
            }
        )

    @staticmethod
    def _trusted_competition(
        arguments: dict[str, Any],
    ) -> tuple[FootballCompetition | None, ToolResult | None]:
        query = str(arguments.get("competition") or "").strip()
        country = str(arguments.get("country") or "").strip()
        competition = resolve_football_competition(query, country=country)
        if competition is None:
            return None, FootballDataTool._error(
                "ESPN action requires a competition from the trusted runtime catalog"
            )
        return competition, None

    @staticmethod
    def _validate_espn_league(data: dict[str, Any], expected: str) -> ToolResult | None:
        header = data.get("header") or {}
        league = header.get("league") or ((data.get("leagues") or [{}])[0])
        actual = str(league.get("slug") or "") if isinstance(league, dict) else ""
        if actual != expected:
            return FootballDataTool._error(
                "ESPN response competition does not match the trusted runtime mapping"
            )
        return None

    @staticmethod
    def _espn_competitors(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
        competitors = event.get("competitors") or []
        result: dict[str, dict[str, Any]] = {}
        for item in competitors:
            if not isinstance(item, dict):
                continue
            side = str(item.get("homeAway") or "")
            if side not in {"home", "away"}:
                continue
            team = item.get("team") or {}
            result[side] = {
                "id": team.get("id"),
                "name": team.get("displayName"),
                "score": item.get("score"),
                "winner": item.get("winner"),
            }
        return result

    @staticmethod
    def _espn_event_row(event: dict[str, Any]) -> dict[str, Any]:
        competition = (event.get("competitions") or [{}])[0]
        teams = FootballDataTool._espn_competitors(competition)
        return {
            "event_id": event.get("id"),
            "match": event.get("name"),
            "date_utc": event.get("date"),
            "status": ((event.get("status") or {}).get("type") or {}).get("description"),
            "venue": (competition.get("venue") or {}).get("fullName"),
            "teams": teams,
        }

    @staticmethod
    def _espn_form(data: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for item in (data.get("boxscore") or {}).get("form") or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "team": (item.get("team") or {}).get("displayName"),
                    "events": [
                        {
                            "event_id": event.get("id"),
                            "date_utc": event.get("gameDate"),
                            "opponent": (event.get("opponent") or {}).get("displayName"),
                            "result": event.get("gameResult"),
                            "score": event.get("score"),
                            "competition": event.get("competitionName"),
                        }
                        for event in (item.get("events") or [])[:5]
                        if isinstance(event, dict)
                    ],
                }
            )
        return rows

    @staticmethod
    def _espn_odds(data: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "provider": (item.get("provider") or {}).get("name"),
                "details": item.get("details"),
                "over_under": item.get("overUnder"),
            }
            for item in (data.get("odds") or [])[:5]
            if isinstance(item, dict)
        ]

    @staticmethod
    def _espn_head_to_head(data: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "event_id": item.get("id"),
                "date_utc": item.get("date"),
                "name": item.get("name"),
            }
            for item in (data.get("headToHeadGames") or [])[:10]
            if isinstance(item, dict)
        ]

    async def _evidence(
        self,
        arguments: dict[str, Any],
        context: ExecutionContext,
        *,
        include_odds: bool = True,
    ) -> ToolResult:
        competition, trusted_error = self._trusted_competition(arguments)
        if trusted_error is not None:
            return trusted_error
        assert competition is not None
        date = str(arguments.get("date") or "").strip()
        season = str(arguments.get("season") or "").strip()
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not season or not team_a or not team_b:
            return self._error("evidence requires competition, date, season, team_a, and team_b")
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return self._error("evidence requires date in YYYY-MM-DD format")
        options_error = self._validate_evidence_options(arguments)
        if options_error is not None:
            return options_error
        cache_key = football_event_key(competition.name, season, date, team_a, team_b)
        if not include_odds:
            cached = await self._cache.get("base", cache_key)
            if cached is not None:
                if not cached.is_error:
                    context.shared_state.publish_football_base(cache_key.value, cached.content)
                return cached

        sources: list[SourceResult] = []
        warnings: list[str] = []
        league_id, league_source = await self._resolve_tsdb_league(competition, context)
        sources.append(league_source)
        if not league_id:
            return self._fail_closed(competition, sources, warnings)

        schedule = await self._source_json(
            f"{_SPORTS_DB}/eventsday.php?{urlencode({'d': date, 'l': league_id})}",
            context,
            source="thesportsdb:schedule",
        )
        sources.append(schedule)
        if schedule.status != "success" or not isinstance(schedule.data, dict):
            return self._fail_closed(competition, sources, warnings)
        fixture = self._select_primary_fixture(
            schedule.data, competition, season=season, date=date, team_a=team_a, team_b=team_b
        )
        if fixture is None:
            sources[-1] = SourceResult(
                "thesportsdb:schedule",
                "rejected",
                error_code="primary_fixture_not_confirmed",
                safe_reason="The primary source did not confirm one matching fixture identity",
            )
            return self._fail_closed(competition, sources, warnings)

        results_source = await self._source_json(
            f"{_SPORTS_DB}/eventsseason.php?{urlencode({'id': league_id, 's': season})}",
            context,
            source="thesportsdb:results",
        )
        standings_source = await self._source_json(
            f"{_SPORTS_DB}/lookuptable.php?{urlencode({'l': league_id, 's': season})}",
            context,
            source="thesportsdb:standings",
        )
        sources.extend((results_source, standings_source))
        results = self._provider_rows(results_source, "events", self._event_row)
        standings = self._provider_rows(standings_source, "table", self._standing_row)
        self._warn_if_degraded(results_source, warnings)
        self._warn_if_degraded(standings_source, warnings)

        espn, details, form, head_to_head = await self._espn_evidence(
            competition, fixture, date=date, context=context
        )
        sources.append(espn)
        self._warn_if_degraded(espn, warnings)

        if not include_odds:
            result = ToolResult(
                content=json.dumps(
                    {
                        "fixture": fixture,
                        "results": results,
                        "standings": standings,
                        "details": details,
                        "form": form,
                        "head_to_head": head_to_head,
                        "odds": {"status": "not_requested"},
                        "sources": [source.report() for source in sources],
                        "warnings": list(dict.fromkeys(warnings)),
                        "completeness": "partial"
                        if any(source.status != "success" for source in sources)
                        else "complete",
                        "base_evidence_key": cache_key.value,
                        "as_of": utc_now(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            await self._cache.put("base", cache_key, result)
            context.shared_state.publish_football_base(cache_key.value, result.content)
            return result

        regions = tuple(str(value) for value in arguments.get("regions") or ("eu",))
        markets = tuple(str(value) for value in arguments.get("markets") or ("h2h", "totals"))
        odds_format = str(arguments.get("odds_format") or "decimal")
        try:
            odds_source = await self._odds_source.fetch(
                competition,
                regions=regions,
                markets=markets,
                odds_format=odds_format,
                commence_from=str(arguments.get("commence_from") or ""),
                commence_to=str(arguments.get("commence_to") or ""),
            )
        except Exception:
            odds_source = SourceResult(
                "the_odds_api",
                "unavailable",
                error_code="odds_request_failed",
                safe_reason="The Odds API request did not complete",
            )
        odds: Any = self._matching_odds(odds_source, fixture, date)
        if odds_source.status == "success" and not odds:
            odds_source = SourceResult(
                "the_odds_api",
                "empty",
                quota=odds_source.quota,
                safe_reason="No matching odds fixture was returned",
            )
        sources.append(odds_source)
        if odds_source.status != "success" and self._allow_sporttery:
            sporttery = await self._sporttery_evidence(team_a, team_b, context)
            sources.append(sporttery)
            if sporttery.status == "success":
                odds = sporttery.data
            else:
                odds = {"status": "unavailable"}
                warnings.append("odds_unavailable")
        elif odds_source.status != "success":
            odds = {"status": "unavailable"}
            warnings.append("odds_unavailable")

        degraded = any(source.status != "success" for source in sources)
        payload = {
            "fixture": fixture,
            "results": results,
            "standings": standings,
            "details": details,
            "form": form,
            "head_to_head": head_to_head,
            "odds": odds,
            "sources": [source.report() for source in sources],
            "warnings": list(dict.fromkeys(warnings)),
            "completeness": "partial" if degraded else "complete",
            "as_of": utc_now(),
        }
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _resolve_tsdb_league(
        self, competition: FootballCompetition, context: ExecutionContext
    ) -> tuple[str, SourceResult]:
        data = await self._source_json(
            f"{_SPORTS_DB}/search_all_leagues.php?"
            + urlencode({"s": "Soccer", "c": competition.country}),
            context,
            source="thesportsdb:competition",
        )
        if data.status != "success" or not isinstance(data.data, dict):
            return "", data
        rows = data.data.get("countries") or data.data.get("leagues") or []
        expected_names = {
            normalize_competition_name(competition.name),
            *(normalize_competition_name(alias) for alias in competition.aliases),
        }
        matches = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            names = {
                normalize_competition_name(str(row.get("strLeague") or "")),
                *(
                    normalize_competition_name(label)
                    for label in str(row.get("strLeagueAlternate") or "").split(",")
                ),
            }
            country = normalize_competition_name(str(row.get("strCountry") or ""))
            if names.isdisjoint(expected_names):
                continue
            if country != normalize_competition_name(competition.country):
                continue
            league_id = str(row.get("idLeague") or "")
            if league_id.isdigit():
                matches.append(league_id)
        if len(set(matches)) != 1:
            return "", SourceResult(
                "thesportsdb:competition",
                "rejected",
                error_code="primary_competition_not_confirmed",
                safe_reason="The primary source did not confirm one reviewed competition identity",
            )
        return matches[0], SourceResult("thesportsdb:competition", "success")

    @classmethod
    def _select_primary_fixture(
        cls,
        data: dict[str, Any],
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
    ) -> dict[str, Any] | None:
        wanted_teams = {normalize_competition_name(team_a), normalize_competition_name(team_b)}
        rows = []
        for row in data.get("events") or []:
            if not isinstance(row, dict):
                continue
            actual_teams = {
                normalize_competition_name(str(row.get("strHomeTeam") or "")),
                normalize_competition_name(str(row.get("strAwayTeam") or "")),
            }
            actual_season = str(row.get("strSeason") or "")
            actual_league = normalize_competition_name(str(row.get("strLeague") or ""))
            if actual_teams != wanted_teams or str(row.get("dateEvent") or "") != date:
                continue
            if actual_season and actual_season != season:
                continue
            if actual_league != normalize_competition_name(competition.name):
                continue
            if not str(row.get("idEvent") or "").isdigit():
                continue
            rows.append(cls._event_row(row))
        return rows[0] if len(rows) == 1 else None

    async def _espn_evidence(
        self,
        competition: FootballCompetition,
        fixture: dict[str, Any],
        *,
        date: str,
        context: ExecutionContext,
    ) -> tuple[SourceResult, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        empty: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] = ({}, [], [])
        schedule = await self._source_json(
            f"{_ESPN_SOCCER}/{competition.espn_slug}/scoreboard?"
            + urlencode({"dates": date.replace("-", "")}),
            context,
            source="espn",
            fetcher=self._espn_fetcher,
        )
        if schedule.status != "success" or not isinstance(schedule.data, dict):
            return schedule, *empty
        if self._espn_league_slug(schedule.data) != competition.espn_slug:
            return self._espn_rejected("espn_league_mismatch"), *empty
        candidates = []
        wanted = {
            normalize_competition_name(str(fixture.get("home") or "")),
            normalize_competition_name(str(fixture.get("away") or "")),
        }
        for raw in schedule.data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            row = self._espn_event_row(raw)
            teams = row.get("teams") or {}
            actual = {
                normalize_competition_name(str((teams.get(side) or {}).get("name") or ""))
                for side in ("home", "away")
            }
            event_id = str(row.get("event_id") or "")
            if (
                actual == wanted
                and str(row.get("date_utc") or "")[:10] == date
                and event_id.isdigit()
            ):
                candidates.append(event_id)
        if len(candidates) != 1:
            return self._espn_rejected("espn_fixture_mismatch"), *empty
        summary = await self._source_json(
            f"{_ESPN_SOCCER}/{competition.espn_slug}/summary?"
            + urlencode({"event": candidates[0]}),
            context,
            source="espn",
            fetcher=self._espn_fetcher,
        )
        if summary.status != "success" or not isinstance(summary.data, dict):
            return summary, *empty
        if self._espn_league_slug(summary.data) != competition.espn_slug:
            return self._espn_rejected("espn_league_mismatch"), *empty
        header = summary.data.get("header") or {}
        events = header.get("competitions") or []
        if not events or not isinstance(events[0], dict):
            return self._espn_rejected("espn_unexpected_payload"), *empty
        event = events[0]
        teams = self._espn_competitors(event)
        actual = {
            normalize_competition_name(str((teams.get(side) or {}).get("name") or ""))
            for side in ("home", "away")
        }
        if actual != wanted or str(event.get("date") or "")[:10] != date:
            return self._espn_rejected("espn_fixture_mismatch"), *empty
        game_info = summary.data.get("gameInfo") or {}
        details = {
            "source_event_id": candidates[0],
            "venue": (game_info.get("venue") or {}).get("fullName"),
            "attendance": game_info.get("attendance"),
            "status": ((event.get("status") or {}).get("type") or {}).get("description"),
        }
        return (
            SourceResult("espn", "success"),
            details,
            self._espn_form(summary.data),
            self._espn_head_to_head(summary.data),
        )

    async def _sporttery_evidence(
        self, team_a: str, team_b: str, context: ExecutionContext
    ) -> SourceResult:
        source = await self._source_json(
            _SPORTTERY, context, source="sporttery:getMatchCalculatorV1"
        )
        if source.status != "success" or not isinstance(source.data, dict):
            return source
        needles = (normalize_competition_name(team_a), normalize_competition_name(team_b))
        matches = []
        for day in (source.data.get("value") or {}).get("matchInfoList") or []:
            if not isinstance(day, dict):
                continue
            for match in day.get("subMatchList") or []:
                if not isinstance(match, dict):
                    continue
                haystack = normalize_competition_name(
                    " ".join(
                        str(match.get(key) or "")
                        for key in (
                            "homeTeamAllName",
                            "homeTeamAbbName",
                            "homeTeamAbbEnName",
                            "awayTeamAllName",
                            "awayTeamAbbName",
                            "awayTeamAbbEnName",
                        )
                    )
                )
                if all(needle in haystack for needle in needles):
                    matches.append(
                        {
                            "source": "sporttery",
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
        if len(matches) != 1:
            return SourceResult(
                "sporttery:getMatchCalculatorV1",
                "empty",
                error_code="sporttery_fixture_not_found",
                safe_reason="No matching currently offered Sporttery fixture was found",
            )
        return SourceResult("sporttery:getMatchCalculatorV1", "success", matches[0])

    async def _source_json(
        self,
        url: str,
        context: ExecutionContext,
        *,
        source: str,
        fetcher: _Fetcher | None = None,
    ) -> SourceResult:
        try:
            fetched = await (fetcher or self._fetcher).execute({"url": url}, context)
        except TimeoutError:
            return SourceResult(
                source,
                "unavailable",
                error_code=f"{source.split(':', 1)[0]}_timeout",
                safe_reason="The source request timed out",
            )
        except Exception:
            return SourceResult(
                source,
                "unavailable",
                error_code=f"{source.split(':', 1)[0]}_request_failed",
                safe_reason="The source request did not complete",
            )
        if fetched.is_error:
            raw_code = str(fetched.metadata.get("errorCode") or "")
            allowed = {
                "espn_http_403",
                "espn_curl_unavailable",
                "espn_timeout",
                "espn_response_too_large",
            }
            code = raw_code if raw_code in allowed else f"{source.split(':', 1)[0]}_request_failed"
            return SourceResult(
                source,
                "unavailable",
                error_code=code,
                safe_reason="The source request did not complete",
            )
        try:
            payload = json.loads(fetched.content)
        except json.JSONDecodeError:
            return SourceResult(
                source,
                "rejected",
                error_code=f"{source.split(':', 1)[0]}_malformed_json",
                safe_reason="The source returned malformed JSON",
            )
        if not isinstance(payload, dict):
            return SourceResult(
                source,
                "rejected",
                error_code=f"{source.split(':', 1)[0]}_unexpected_payload",
                safe_reason="The source response structure was not recognized",
            )
        return SourceResult(source, "success", payload)

    @staticmethod
    def _provider_rows(
        source: SourceResult,
        key: str,
        transform: Any,
    ) -> list[dict[str, Any]]:
        if source.status != "success" or not isinstance(source.data, dict):
            return []
        raw = source.data.get(key) or []
        if not isinstance(raw, list):
            source.status = "rejected"
            source.error_code = f"{source.source.split(':', 1)[0]}_unexpected_payload"
            source.safe_reason = "The source response structure was not recognized"
            return []
        rows = [transform(row) for row in raw if isinstance(row, dict)]
        if not rows:
            source.status = "empty"
            source.safe_reason = "The source returned no rows for this scope"
        return rows

    @staticmethod
    def _matching_odds(
        source: SourceResult, fixture: dict[str, Any], date: str
    ) -> list[dict[str, Any]]:
        if source.status != "success" or not isinstance(source.data, list):
            return []
        wanted = {
            normalize_competition_name(str(fixture.get("home") or "")),
            normalize_competition_name(str(fixture.get("away") or "")),
        }
        return [
            row
            for row in source.data
            if isinstance(row, dict)
            and {
                normalize_competition_name(str(row.get("home_team") or "")),
                normalize_competition_name(str(row.get("away_team") or "")),
            }
            == wanted
            and (not row.get("commence_time") or str(row.get("commence_time"))[:10] == date)
        ]

    @staticmethod
    def _espn_league_slug(data: dict[str, Any]) -> str:
        header = data.get("header") or {}
        league = header.get("league") or ((data.get("leagues") or [{}])[0])
        return str(league.get("slug") or "") if isinstance(league, dict) else ""

    @staticmethod
    def _espn_rejected(code: str) -> SourceResult:
        return SourceResult(
            "espn",
            "rejected",
            error_code=code,
            safe_reason="ESPN supplemental data did not match the confirmed primary fixture",
        )

    @staticmethod
    def _warn_if_degraded(source: SourceResult, warnings: list[str]) -> None:
        if source.status != "success":
            warnings.append(source.error_code or f"{source.source}_empty")

    @staticmethod
    def _validate_evidence_options(arguments: dict[str, Any]) -> ToolResult | None:
        regions = arguments.get("regions") or ["eu"]
        markets = arguments.get("markets") or ["h2h", "totals"]
        if not isinstance(regions, list) or not set(regions).issubset({"us", "uk", "eu", "au"}):
            return FootballDataTool._error("regions contains an unsupported value")
        if not isinstance(markets, list) or not set(markets).issubset({"h2h", "totals"}):
            return FootballDataTool._error("markets contains an unsupported value")
        if str(arguments.get("odds_format") or "decimal") not in {"decimal", "american"}:
            return FootballDataTool._error("odds_format is unsupported")
        for key in ("commence_from", "commence_to"):
            value = str(arguments.get(key) or "")
            if value:
                try:
                    datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    return FootballDataTool._error(f"{key} must be an ISO-8601 timestamp")
        return None

    @staticmethod
    def _fail_closed(
        competition: FootballCompetition,
        sources: list[SourceResult],
        warnings: list[str],
    ) -> ToolResult:
        payload = {
            "fixture": None,
            "results": [],
            "standings": [],
            "details": {},
            "form": [],
            "head_to_head": [],
            "odds": {"status": "unavailable"},
            "sources": [source.report() for source in sources],
            "warnings": [*warnings, "primary_fixture_unavailable; do not infer facts"],
            "completeness": "unavailable",
            "as_of": utc_now(),
            "competition": competition.public_mapping(),
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            is_error=True,
            metadata={"errorCode": "football_primary_unavailable"},
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
        competition, trusted_error = self._trusted_competition(arguments)
        if trusted_error is not None:
            return trusted_error
        assert competition is not None
        league_id, source = await self._resolve_tsdb_league(competition, context)
        if not league_id:
            return ToolResult(
                content=json.dumps(
                    {
                        "error": "TheSportsDB competition identity could not be confirmed",
                        "source": source.report(),
                    },
                    ensure_ascii=False,
                ),
                is_error=True,
                metadata={"errorCode": "football_primary_unavailable"},
            )
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
        self,
        url: str,
        context: ExecutionContext,
        *,
        fetcher: _Fetcher | None = None,
    ) -> tuple[dict[str, Any], ToolResult | None]:
        fetched = await (fetcher or self._fetcher).execute({"url": url}, context)
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

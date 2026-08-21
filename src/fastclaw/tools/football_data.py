"""Competition-aware football data retrieval built on the pinned Web fetcher."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime, timedelta
from datetime import date as CalendarDate
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from fastclaw.execution import ExecutionContext, football_teams_match
from fastclaw.football_identity import (
    DEFAULT_TEAM_IDENTITY_RESOLVER,
    resolve_team_identity,
)
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
    FOOTBALL_EVIDENCE_SCHEMA_VERSION,
    FootballEvidenceCache,
    football_event_key,
    get_football_evidence_cache,
    normalize_football_season,
)

_SPORTS_DB = "https://www.thesportsdb.com/api/v1/json/123"
_ESPN_SOCCER = "https://site.api.espn.com/apis/site/v2/sports/soccer"
_SPORTTERY = (
    "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?channel=c"
)


def _provider_team_names(match: dict[str, Any], side: str) -> tuple[str, ...]:
    """Return every provider label before resolving a team identity.

    Some feeds publish a Chinese display label and an English abbreviation for
    the same club. Matching only the first label makes an English user query
    fail even though the provider has supplied a usable equivalent name.
    """

    keys = (
        ("homeTeamAllName", "homeTeamAbbName", "homeTeamAbbEnName")
        if side == "home"
        else ("awayTeamAllName", "awayTeamAbbName", "awayTeamAbbEnName")
    )
    return tuple(dict.fromkeys(str(match[key]).strip() for key in keys if match.get(key)))


def _normalize_team_identity(value: str) -> str:
    """Use the shared alias table for every provider/team join."""

    return resolve_team_identity(value).canonical_id


def _previous_season(value: str) -> str:
    """Return the immediately preceding provider season label."""

    normalized = normalize_football_season(value)
    parts = normalized.split("-", 1)
    if len(parts) == 2 and all(part.isdigit() for part in parts):
        return f"{int(parts[0]) - 1:04d}-{int(parts[1]) - 1:04d}"
    if normalized.isdigit() and len(normalized) == 4:
        return str(int(normalized) - 1)
    return ""


def _is_friendlies_competition(value: str) -> bool:
    normalized = normalize_competition_name(value)
    return "friendly" in normalized or "friendlies" in normalized


def _is_completed_event(event: dict[str, Any]) -> bool:
    status = str(event.get("status") or "").casefold()
    if status in {"ft", "aet", "pen", "finished", "complete"}:
        return True
    return event.get("home_score") is not None and event.get("away_score") is not None


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


class SportteryPublicFetcher:
    """Fetch the fixed Sporttery calculator endpoint with trusted system curl.

    The provider currently rejects the Runtime's generic HTTPX client with a
    non-standard HTTP 567 response while accepting curl and the reviewed Go
    toolkit transport.  Keep this compatibility path limited to the one fixed
    public endpoint instead of weakening the generic WebFetch policy.
    """

    _executables: tuple[Path, ...] = (Path("/usr/bin/curl"), Path("/bin/curl"))

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        url = str(arguments.get("url") or "")
        if url != _SPORTTERY:
            return ToolResult(
                content="Sporttery URL is outside the fixed calculator endpoint",
                is_error=True,
                metadata={"errorCode": "sporttery_url_rejected"},
            )
        executable = next((path for path in self._executables if path.is_file()), None)
        if executable is None:
            return ToolResult(
                content="Sporttery source is not ready",
                is_error=True,
                metadata={"errorCode": "sporttery_curl_unavailable", "ready": False},
            )
        process = await asyncio.create_subprocess_exec(
            str(executable),
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "20",
            "--max-filesize",
            "2000000",
            "--proto",
            "=https",
            "--proto-redir",
            "=https",
            "--max-redirs",
            "0",
            "--header",
            "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X) sporttery-ev/1.0",
            "--header",
            "Referer: https://m.sporttery.cn/mjc/jsq/zqspf/",
            "--header",
            "Accept: application/json,text/plain,*/*",
            url,
            env={"PATH": "/usr/bin:/bin"},
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=25)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
                await asyncio.shield(process.wait())
            return ToolResult(
                content="Sporttery source timed out",
                is_error=True,
                metadata={"errorCode": "sporttery_timeout"},
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
                content="Sporttery request failed",
                is_error=True,
                metadata={"errorCode": "sporttery_request_failed"},
            )
        if len(stdout) > 2_000_000:
            return ToolResult(
                content="Sporttery response exceeded the safety limit",
                is_error=True,
                metadata={"errorCode": "sporttery_response_too_large"},
            )
        return ToolResult(content=stdout.decode("utf-8", errors="replace"))


class FootballDataTool:
    """Resolve competitions and fetch fixtures without a World Cup hard-code."""

    def __init__(
        self,
        fetcher: _Fetcher,
        *,
        espn_fetcher: _Fetcher | None = None,
        sporttery_fetcher: _Fetcher | None = None,
        odds_source: OddsSource | None = None,
        allow_sporttery: bool = False,
        role_mode: str = "full",
        cache: FootballEvidenceCache | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._espn_fetcher = espn_fetcher or EspnPublicFetcher()
        self._sporttery_fetcher = sporttery_fetcher or fetcher
        self._odds_source = odds_source or TheOddsApiSource.from_environment()
        self._allow_sporttery = allow_sporttery
        self._role_mode = role_mode
        self._cache = cache or get_football_evidence_cache()
        # One prediction run often asks for several fixtures from the same
        # competition and date. Share provider outcomes inside that root
        # execution so parallel tool calls do not burst the same schedule
        # endpoint and trigger avoidable rate limits.
        self._source_cache: dict[tuple[str, int, str], SourceResult] = {}
        self._source_locks: dict[tuple[str, int, str], asyncio.Lock] = {}
        description = (
            "Resolve a football competition and fetch dated fixtures, results, standings, "
            "team form, head-to-head, or an evidence bundle. Provider identifiers and URLs "
            "are selected only by the Runtime."
        )
        if role_mode == "data":
            description = (
                "Fetch one cached base evidence bundle for a new confirmed fixture, or query "
                "competition results during a Runtime-managed ledger settlement review. This "
                "data-only action uses TheSportsDB and ESPN supplements and never calls The "
                "Odds API or Sporttery. The base bundle includes explicitly separated "
                "historical_context for prior-season competitive matches, reviewed adjacent "
                "division evidence, and preseason friendlies."
            )
        elif role_mode == "ev":
            description = (
                "Fetch official Sporttery prices only inside explicitly requested EV analysis "
                "for the confirmed fixture."
            )
        elif role_mode == "odds":
            description = (
                "Fetch official Sporttery prices only when the original user explicitly names "
                "Sporttery, official SP, 竞彩, or 体彩 for the confirmed fixture."
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
            # Ledger review uses the same trusted competition/results endpoint,
            # but must not be forced through the upcoming-fixture base gate.
            actions.append("results")
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
                        "country": {
                            "type": "string",
                            "description": (
                                "Optional competition country or region; omit for international "
                                "competitions and never use a participating team's country"
                            ),
                        },
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
        if context.shared_state.football_settlement_review and self._role_mode == "data":
            if action != "results":
                result = self._error(
                    "settlement review only permits football_data action=results; "
                    "base_evidence and evidence are forbidden"
                )
                context.shared_state.football_settlement_errors.append(result.content)
                return result
        if self._role_mode == "data" and action == "evidence":
            # Older persisted prompts may still attempt the full evidence action
            # after base_evidence has already published the bundle. Do not let a
            # redundant historical-date request overwrite a successful run.
            if context.shared_state.has_football_base():
                return ToolResult(
                    content=(
                        "base_evidence is already published; reuse its historical_context "
                        "for H2H, prior-season, and preseason evidence. No second fixture "
                        "lookup is needed."
                    ),
                    metadata={"status": "base_already_published"},
                )
            return self._error(
                "data analyst must call football_data action=base_evidence; "
                "do not use evidence for a separate H2H lookup"
            )
        if self._role_mode == "data" and action not in {"base_evidence", "evidence", "results"}:
            return self._error(
                "data analyst may request base_evidence or competition results for ledger review"
            )
        if self._role_mode == "ev" and not context.shared_state.ev_requested:
            return self._error("EV market lookup requires an explicit user request for EV")
        if self._role_mode == "ev" and action != "sporttery_match":
            return self._error("EV analyst may only request Sporttery market prices")
        if self._role_mode == "odds" and not context.shared_state.sporttery_requested:
            return self._error("Sporttery lookup requires an explicit Sporttery request")
        if self._role_mode == "odds" and action != "sporttery_match":
            return self._error("odds analyst may only use football_data for Sporttery prices")
        if action == "competition_resolve":
            return self._competition_resolve(arguments)
        if action == "competition_search":
            return await self._competition_search(arguments, context)
        if action == "espn_schedule":
            return await self._espn_schedule(arguments, context)
        if action == "espn_summary":
            return await self._espn_summary(arguments, context)
        if action in {"schedule", "results", "standings"}:
            result = await self._competition_data(action, arguments, context)
            if action == "results":
                if result.is_error:
                    context.shared_state.football_settlement_errors.append(result.content)
                else:
                    context.shared_state.football_settlement_results.append(result.content)
            return result
        if action == "form":
            return await self._form(arguments, context)
        if action == "h2h":
            return await self._h2h(arguments, context)
        if action == "sporttery_match":
            if not self._allow_sporttery:
                return self._error(
                    "sporttery_match is restricted to the odds and EV analyst roles"
                )
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
        competition = self._resolve_tool_competition(query, country=country)
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
    def _resolve_tool_competition(
        query: str, *, country: str = ""
    ) -> FootballCompetition | None:
        """Resolve a reviewed competition while treating model country as advisory.

        Agents sometimes supply a participating club's country rather than the
        competition's region. A unique reviewed name is still authoritative;
        the country hint is only needed to disambiguate duplicate names.
        """

        competition = resolve_football_competition(query, country=country)
        if competition is None and country:
            competition = resolve_football_competition(query)
        return competition

    @staticmethod
    def _trusted_competition(
        arguments: dict[str, Any],
    ) -> tuple[FootballCompetition | None, ToolResult | None]:
        query = str(arguments.get("competition") or "").strip()
        country = str(arguments.get("country") or "").strip()
        competition = FootballDataTool._resolve_tool_competition(query, country=country)
        if competition is None:
            return None, FootballDataTool._error(
                "football_data requires a competition from the trusted runtime catalog"
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
        season = event.get("season") or {}
        notes = competition.get("notes") or []
        leg = next(
            (
                str(note.get("headline") or note.get("text") or "").strip()
                for note in notes
                if isinstance(note, dict)
                and str(note.get("headline") or note.get("text") or "").strip()
            ),
            "",
        )
        return {
            "event_id": event.get("id"),
            "match": event.get("name"),
            "date_utc": event.get("date"),
            "status": ((event.get("status") or {}).get("type") or {}).get("description"),
            "venue": (competition.get("venue") or {}).get("fullName"),
            "teams": teams,
            "stage": season.get("name") or season.get("slug"),
            "leg": leg or None,
        }

    @staticmethod
    def _espn_form(data: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        # ESPN moved pre-match soccer form from ``boxscore.form`` to the
        # top-level ``lastFiveGames`` collection. Accept both reviewed shapes
        # so a provider schema transition cannot silently erase team form.
        form_items = (data.get("boxscore") or {}).get("form") or data.get(
            "lastFiveGames"
        ) or []
        for item in form_items:
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
        header_event = ((data.get("header") or {}).get("competitions") or [{}])[0]
        teams = FootballDataTool._espn_competitors(header_event)

        def american_to_decimal(value: Any) -> float | None:
            try:
                odds = float(value)
            except (TypeError, ValueError):
                return None
            if odds == 0:
                return None
            return round(1 + odds / 100, 3) if odds > 0 else round(1 + 100 / abs(odds), 3)

        rows: list[dict[str, Any]] = []
        for item in (data.get("odds") or data.get("pickcenter") or [])[:5]:
            if not isinstance(item, dict):
                continue
            moneyline = item.get("moneyline") or {}
            home_american = (item.get("homeTeamOdds") or {}).get("moneyLine")
            draw_american = (item.get("drawOdds") or {}).get("moneyLine")
            away_american = (item.get("awayTeamOdds") or {}).get("moneyLine")
            if home_american is None:
                home_american = ((moneyline.get("home") or {}).get("close") or {}).get(
                    "odds"
                )
            if draw_american is None:
                draw_american = ((moneyline.get("draw") or {}).get("close") or {}).get(
                    "odds"
                )
            if away_american is None:
                away_american = ((moneyline.get("away") or {}).get("close") or {}).get(
                    "odds"
                )
            rows.append(
                {
                    "provider": (item.get("provider") or {}).get("name"),
                    "details": item.get("details"),
                    "commence_time": header_event.get("date"),
                    "home_team": (teams.get("home") or {}).get("name"),
                    "away_team": (teams.get("away") or {}).get("name"),
                    "american_odds": {
                        "home": home_american,
                        "draw": draw_american,
                        "away": away_american,
                    },
                    "decimal_odds": {
                        "home": american_to_decimal(home_american),
                        "draw": american_to_decimal(draw_american),
                        "away": american_to_decimal(away_american),
                    },
                    "over_under": item.get("overUnder"),
                    "source": "espn:summary:odds",
                }
            )
        return rows

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
        season = normalize_football_season(str(arguments.get("season") or ""))
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not season or not team_a or not team_b:
            return self._error("evidence requires competition, season, team_a, and team_b")
        if date:
            try:
                datetime.strptime(date, "%Y-%m-%d")
            except ValueError:
                return self._error("evidence date must use YYYY-MM-DD format when provided")
        options_error = self._validate_evidence_options(arguments)
        if options_error is not None:
            return options_error
        cache_key = (
            football_event_key(competition.name, season, date, team_a, team_b)
            if date
            else None
        )
        if not include_odds and cache_key is not None:
            cached = await self._cache.get("base", cache_key)
            if cached is None:
                cached_match = await self._cache.find_fixture(
                    "base",
                    competition=competition.name,
                    season=season,
                    date=date,
                    team_a=team_a,
                    team_b=team_b,
                )
                if cached_match is not None:
                    _cached_key, cached = cached_match
            if cached is not None:
                if not cached.is_error:
                    context.shared_state.publish_football_base(cache_key.value, cached.content)
                    await self._cache.put_session_base(context.session_id, cache_key, cached)
                return cached

        sources: list[SourceResult] = []
        warnings: list[str] = []
        league_id, league_source = await self._resolve_tsdb_league(competition, context)
        sources.append(league_source)
        season_schedule: SourceResult | None = None
        fixture: dict[str, Any] | None = None
        if not league_id:
            if competition.espn_qualifying_slug:
                fallback_fixture, fallback_source, fallback_match = (
                    await self._espn_qualifying_fixture(
                        competition,
                        season=season,
                        date=date,
                        team_a=team_a,
                        team_b=team_b,
                        context=context,
                    )
                )
                fallback_label = "espn_qualifying"
            else:
                fallback_fixture, fallback_source, fallback_match = (
                    await self._espn_primary_fixture(
                        competition,
                        season=season,
                        date=date,
                        team_a=team_a,
                        team_b=team_b,
                        context=context,
                    )
                )
                fallback_label = "espn_primary"
            sources.append(fallback_source)
            if fallback_fixture is None:
                if fallback_match == "no_match":
                    result = self._fixture_no_match(
                        competition,
                        season=season,
                        date=date,
                        team_a=team_a,
                        team_b=team_b,
                        sources=sources,
                        warnings=warnings,
                    )
                    context.shared_state.publish_football_negative(result.content)
                    return result
                return self._fail_closed(competition, sources, warnings)
            fixture = fallback_fixture
            if not date:
                date = str(fixture.get("date") or "")
                warnings.append(f"requested_date_discovered_from_{fallback_label}")
            elif fixture.get("date_alignment") != "exact":
                warnings.append("source_date_adjacent_to_requested_date")
            warnings.extend(
                (
                    f"primary_fixture_confirmed_by_{fallback_label}",
                    "thesportsdb_competition_unavailable",
                )
            )

        if fixture is None and date:
            schedule = await self._source_json(
                f"{_SPORTS_DB}/eventsday.php?{urlencode({'d': date, 'l': league_id})}",
                context,
                source="thesportsdb:schedule",
            )
            sources.append(schedule)
            if (
                schedule.status != "success"
                or not isinstance(schedule.data, dict)
            ) and not competition.espn_qualifying_slug:
                return self._fail_closed(competition, sources, warnings)
            if schedule.status == "success" and isinstance(schedule.data, dict):
                fixture = self._select_primary_fixture(
                    schedule.data,
                    competition,
                    season=season,
                    date=date,
                    team_a=team_a,
                    team_b=team_b,
                )
            if fixture is None:
                sources[-1] = SourceResult(
                    "thesportsdb:schedule",
                    "rejected",
                    error_code="primary_fixture_not_on_requested_date",
                    safe_reason="The day endpoint did not contain the requested fixture",
                )
        if fixture is None and league_id:
            season_schedule = await self._source_json(
                f"{_SPORTS_DB}/eventsseason.php?{urlencode({'id': league_id, 's': season})}",
                context,
                source="thesportsdb:season_schedule",
            )
            sources.append(season_schedule)
            if season_schedule.status == "success" and isinstance(season_schedule.data, dict):
                fixture = self._select_primary_fixture(
                    season_schedule.data,
                    competition,
                    season=season,
                    date=date,
                    team_a=team_a,
                    team_b=team_b,
                    allow_adjacent_date=bool(date),
                )
                if fixture is not None:
                    if not date:
                        date = str(fixture.get("date") or "")
                        warnings.append("requested_date_discovered_from_season_schedule")
                    elif fixture.get("date_alignment") != "exact":
                        warnings.append("source_date_adjacent_to_requested_date")
            if fixture is None and competition.espn_qualifying_slug:
                qualifier_fixture, qualifier_source, qualifier_match = (
                    await self._espn_qualifying_fixture(
                        competition,
                        season=season,
                        date=date,
                        team_a=team_a,
                        team_b=team_b,
                        context=context,
                    )
                )
                sources.append(qualifier_source)
                if qualifier_fixture is not None:
                    fixture = qualifier_fixture
                    if not date:
                        date = str(fixture.get("date") or "")
                        warnings.append("requested_date_discovered_from_espn_qualifying")
                    elif fixture.get("date_alignment") != "exact":
                        warnings.append("source_date_adjacent_to_requested_date")
                    warnings.append("primary_fixture_confirmed_by_espn_qualifying")
                elif qualifier_match == "ambiguous":
                    return self._fail_closed(competition, sources, warnings)
                elif qualifier_match == "unavailable":
                    return self._fail_closed(competition, sources, warnings)
            if fixture is None:
                if season_schedule.status == "success" and isinstance(
                    season_schedule.data, dict
                ):
                    result = self._fixture_no_match(
                        competition,
                        season=season,
                        date=date,
                        team_a=team_a,
                        team_b=team_b,
                        sources=sources,
                        warnings=warnings,
                    )
                    context.shared_state.publish_football_negative(result.content)
                    return result
                return self._fail_closed(competition, sources, warnings)
        cache_key = football_event_key(competition.name, season, date, team_a, team_b)

        if not league_id:
            results_source = SourceResult(
                "thesportsdb:results",
                "unavailable",
                error_code=league_source.error_code or "thesportsdb_competition_unavailable",
                safe_reason="Competition results are unavailable because league resolution failed",
            )
            standings_source = SourceResult(
                "thesportsdb:standings",
                "unavailable",
                error_code=league_source.error_code or "thesportsdb_competition_unavailable",
                safe_reason="Standings are unavailable because league resolution failed",
            )
        elif season_schedule is not None and season_schedule.status == "success":
            results_source = SourceResult(
                "thesportsdb:results", "success", data=season_schedule.data
            )
            standings_source = await self._source_json(
                f"{_SPORTS_DB}/lookuptable.php?{urlencode({'l': league_id, 's': season})}",
                context,
                source="thesportsdb:standings",
            )
        else:
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

        # A user may supply the local Sporttery calendar date while ESPN stores
        # the same kickoff under the previous UTC date. Once the fixture is
        # confirmed, use its provider date for supplemental schedule/summary
        # validation instead of reusing the adjacent requested date.
        fixture_date = str(fixture.get("date") or date)
        espn, details, form, head_to_head = await self._espn_evidence(
            competition, fixture, date=fixture_date, context=context
        )
        sources.append(espn)
        self._warn_if_degraded(espn, warnings)
        previous_match_details: list[dict[str, Any]] = []
        if league_id and (not form or not head_to_head):
            fallback_sources, fallback_form, fallback_h2h, previous_match_details = (
                await self._thesportsdb_historical_fallback(
                    team_a,
                    team_b,
                    context,
                    fixture=fixture,
                )
            )
            sources.extend(fallback_sources)
            if not form and fallback_form:
                form = fallback_form
                warnings.append("form_cross_season_fallback")
            if not head_to_head and fallback_h2h:
                head_to_head = fallback_h2h
                warnings.append("h2h_thesportsdb_fallback")

        if league_id:
            historical_sources, historical_context = await self._historical_context(
                competition,
                league_id,
                season=season,
                team_a=team_a,
                team_b=team_b,
                fixture=fixture,
                form=form,
                head_to_head=head_to_head,
                context=context,
            )
        else:
            historical_sources = [
                SourceResult(
                    "thesportsdb:historical_context",
                    "unavailable",
                    error_code=league_source.error_code
                    or "thesportsdb_competition_unavailable",
                    safe_reason=(
                        "Historical context is unavailable because league resolution failed"
                    ),
                )
            ]
            historical_context = {
                "status": "unavailable",
                "current_season": season,
                "prior_season": _previous_season(season),
                "fixture": {
                    "competition": competition.name,
                    "home": str(fixture.get("home") or team_a),
                    "away": str(fixture.get("away") or team_b),
                },
                "head_to_head": head_to_head,
                "teams": [],
                "coverage": "TheSportsDB competition identity was unavailable",
                "interpretation_rules": [
                    "unavailable historical data must not be converted into a directional claim"
                ],
            }
        sources.extend(historical_sources)
        if any(source.status != "success" for source in historical_sources):
            warnings.append("historical_context_degraded")

        if not include_odds:
            result = ToolResult(
                content=json.dumps(
                    {
                        "fixture": fixture,
                        "evidence_schema_version": FOOTBALL_EVIDENCE_SCHEMA_VERSION,
                        "results": results,
                        "standings": standings,
                        "details": details,
                        "form": form,
                        "head_to_head": head_to_head,
                        "previous_match_details": previous_match_details,
                        "historical_context": historical_context,
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
            await self._cache.put_session_base(context.session_id, cache_key, result)
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
                "no_match",
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
                odds = {"status": sporttery.status}
                warnings.append("odds_unavailable")
        elif odds_source.status != "success":
            odds = {"status": odds_source.status}
            warnings.append("odds_unavailable")

        degraded = any(source.status != "success" for source in sources)
        payload = {
            "evidence_schema_version": FOOTBALL_EVIDENCE_SCHEMA_VERSION,
            "fixture": fixture,
            "results": results,
            "standings": standings,
            "details": details,
            "form": form,
            "head_to_head": head_to_head,
            "previous_match_details": previous_match_details,
            "historical_context": historical_context,
            "odds": odds,
            "sources": [source.report() for source in sources],
            "warnings": list(dict.fromkeys(warnings)),
            "completeness": "partial" if degraded else "complete",
            "as_of": utc_now(),
        }
        return ToolResult(content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    async def _historical_context(
        self,
        competition: FootballCompetition,
        league_id: str,
        *,
        season: str,
        team_a: str,
        team_b: str,
        fixture: dict[str, Any],
        form: list[dict[str, Any]],
        head_to_head: list[dict[str, Any]],
        context: ExecutionContext,
    ) -> tuple[list[SourceResult], dict[str, Any]]:
        """Build explicit historical buckets for first-round analysis.

        ``eventslast`` is intentionally not treated as a complete prior-season
        form source: on a new-season opener it often returns only a preseason
        friendly. Query the reviewed previous-season league schedule separately
        and, for competitions with a reviewed adjacent tier, query that tier as
        well. The result exposes promotion/relegation context only when the
        provider actually returns matches for the requested team.
        """

        previous_season = _previous_season(season)
        source_specs: list[tuple[str, str, str]] = [
            ("same_competition", competition.name, league_id)
        ]
        source_specs.extend(
            ("prior_division", name, provider_id)
            for name, provider_id in competition.historical_league_ids
            if provider_id != league_id
        )
        sources: list[SourceResult] = []
        rows_by_scope: dict[str, list[dict[str, Any]]] = {}
        if previous_season:
            for scope, _provider_name, provider_id in source_specs:
                source = await self._source_json(
                    f"{_SPORTS_DB}/eventsseason.php?"
                    + urlencode({"id": provider_id, "s": previous_season}),
                    context,
                    source=f"thesportsdb:historical_{scope}",
                )
                sources.append(source)
                rows_by_scope[scope] = (
                    self._provider_rows(source, "events", self._event_row)
                    if source.status == "success" and isinstance(source.data, dict)
                    else []
                )

        team_specs = (
            (
                team_a,
                str(fixture.get("home") or team_a),
                str(fixture.get("home_team_id") or ""),
            ),
            (
                team_b,
                str(fixture.get("away") or team_b),
                str(fixture.get("away_team_id") or ""),
            ),
        )

        def matches_team(
            event: dict[str, Any], requested: str, provider_id: str
        ) -> bool:
            for side, side_id in (("home", "home_team_id"), ("away", "away_team_id")):
                candidate_id = str(event.get(side_id) or "")
                if provider_id and candidate_id and provider_id == candidate_id:
                    return True
                if football_teams_match(
                    requested,
                    str(event.get(side) or ""),
                    right_provider="thesportsdb",
                    right_provider_id=candidate_id,
                ):
                    return True
            return False

        teams: list[dict[str, Any]] = []
        for requested, provider_name, provider_id in team_specs:
            same_competition = [
                event
                for event in rows_by_scope.get("same_competition", [])
                if matches_team(event, requested, provider_id) and _is_completed_event(event)
            ][:10]
            prior_division = [
                event
                for event in rows_by_scope.get("prior_division", [])
                if matches_team(event, requested, provider_id) and _is_completed_event(event)
            ][:10]
            preseason_friendlies = [
                match
                for item in form
                if football_teams_match(requested, str(item.get("team") or ""))
                for match in item.get("matches") or []
                if isinstance(match, dict) and _is_friendlies_competition(
                    str(match.get("competition") or "")
                )
            ][:10]
            if prior_division:
                boundary_status = "source_supported_prior_division"
                prior_competition = sorted(
                    {
                        str(event.get("competition") or "")
                        for event in prior_division
                        if event.get("competition")
                    }
                )
            elif same_competition:
                boundary_status = "source_supported_same_competition"
                prior_competition = sorted(
                    {
                        str(event.get("competition") or "")
                        for event in same_competition
                        if event.get("competition")
                    }
                )
            else:
                boundary_status = "not_confirmed_by_available_schedule"
                prior_competition = []
            teams.append(
                {
                    "team": requested,
                    "provider_name": provider_name,
                    "provider_team_id": provider_id or None,
                    "current_competition": competition.name,
                    "prior_season": previous_season,
                    "prior_competitions": prior_competition,
                    "competition_boundary": boundary_status,
                    "same_competition_prior_matches": same_competition,
                    "prior_division_matches": prior_division,
                    "preseason_friendlies": preseason_friendlies,
                }
            )

        has_prior = any(
            team["same_competition_prior_matches"] or team["prior_division_matches"]
            for team in teams
        )
        has_friendlies = any(team["preseason_friendlies"] for team in teams)
        if has_prior:
            status = "success"
        elif has_friendlies:
            status = "partial"
        else:
            status = "empty"
        return sources, {
            "status": status,
            "current_season": season,
            "prior_season": previous_season,
            "fixture": {
                "competition": competition.name,
                "home": str(fixture.get("home") or team_a),
                "away": str(fixture.get("away") or team_b),
            },
            "head_to_head": head_to_head,
            "teams": teams,
            "coverage": "TheSportsDB free season schedules may be truncated",
            "interpretation_rules": [
                "prior_division_matches are the only source-backed "
                "promotion/relegation boundary evidence",
                "same_competition_prior_matches are prior-season competitive evidence",
                "preseason_friendlies are separate weak context and are not current-season form",
                "empty or unavailable historical data must not be converted into a "
                "directional claim",
            ],
        }

    async def _thesportsdb_historical_fallback(
        self,
        team_a: str,
        team_b: str,
        context: ExecutionContext,
        *,
        fixture: dict[str, Any] | None = None,
    ) -> tuple[
        list[SourceResult],
        list[dict[str, Any]],
        list[dict[str, Any]],
        list[dict[str, Any]],
    ]:
        """Provide cross-season form, H2H, and prior-event lineup evidence.

        A new season has no current-season form. TheSportsDB's team event history
        is therefore used as an explicitly labeled fallback, never as current-
        season evidence. Event details are best-effort because the free endpoint
        may omit lineup fields.
        """

        sources: list[SourceResult] = []
        team_events: dict[str, list[dict[str, Any]]] = {}
        form: list[dict[str, Any]] = []
        requested_teams = (
            (
                team_a,
                str((fixture or {}).get("home") or team_a),
                str((fixture or {}).get("home_team_id") or ""),
            ),
            (
                team_b,
                str((fixture or {}).get("away") or team_b),
                str((fixture or {}).get("away_team_id") or ""),
            ),
        )
        for team, provider_name, provider_id in requested_teams:
            team_query = provider_name or team
            if provider_id:
                team_id = provider_id
                search = None
            else:
                search = await self._source_json(
                    f"{_SPORTS_DB}/searchteams.php?{urlencode({'t': team_query})}",
                    context,
                    source="thesportsdb:team_search",
                )
                sources.append(search)
                rows = search.data.get("teams") if isinstance(search.data, dict) else None
                if search.status != "success" or not isinstance(rows, list) or not rows:
                    if search.status == "success":
                        search.status = "empty"
                        search.safe_reason = "The source returned no matching team"
                    team_events[team] = []
                    form.append(
                        {
                            "team": team,
                            "matches": [],
                            "scope": "cross_competition_may_cross_season",
                            "status": "unavailable",
                        }
                    )
                    continue
                first_team = rows[0] if isinstance(rows[0], dict) else {}
                team_id = str(first_team.get("idTeam") or "")
            if not team_id:
                team_events[team] = []
                form.append(
                    {
                        "team": team,
                        "matches": [],
                        "scope": "cross_competition_may_cross_season",
                        "status": "unavailable",
                    }
                )
                continue
            recent = await self._source_json(
                f"{_SPORTS_DB}/eventslast.php?{urlencode({'id': team_id})}",
                context,
                source="thesportsdb:eventslast",
            )
            sources.append(recent)
            events = self._provider_rows(recent, "results", self._event_row)
            team_events[team] = events[:8]
            form.append(
                {
                    "team": team,
                    "matches": events[:8],
                    "scope": "cross_competition_may_cross_season",
                    "status": "success" if events else "empty",
                }
            )

        h2h = [
            event
            for event in team_events.get(team_a, [])
            if _normalize_team_identity(str(event.get("home") or ""))
            == _normalize_team_identity(team_b)
            or _normalize_team_identity(str(event.get("away") or ""))
            == _normalize_team_identity(team_b)
        ][:10]
        if not h2h:
            # ``eventslast`` is a deliberately small recent-events feed and
            # cannot establish H2H just because the opponent is absent from
            # those latest rows. The free TheSportsDB API has no dedicated
            # H2H endpoint, but its documented event search can find the
            # exact fixture in the preceding season in both home/away orders.
            prior_season = _previous_season(
                str((fixture or {}).get("season") or "")
            )
            competition_name = normalize_competition_name(
                str((fixture or {}).get("competition") or "")
            )
            home_provider = str(requested_teams[0][1] or team_a)
            away_provider = str(requested_teams[1][1] or team_b)
            team_ids = (str(requested_teams[0][2] or ""), str(requested_teams[1][2] or ""))

            def side_matches(
                event: dict[str, Any],
                requested: str,
                requested_id: str,
                side: str,
            ) -> bool:
                candidate = str(event.get(side) or "")
                candidate_id = str(event.get(f"{side}_team_id") or "")
                return bool(
                    requested_id
                    and candidate_id
                    and requested_id == candidate_id
                ) or football_teams_match(
                    requested,
                    candidate,
                    right_provider="thesportsdb",
                    right_provider_id=candidate_id,
                )

            if prior_season and home_provider and away_provider:
                searched: list[dict[str, Any]] = []
                seen_event_ids: set[str] = set()
                for search_home, search_away in (
                    (home_provider, away_provider),
                    (away_provider, home_provider),
                ):
                    source = await self._source_json(
                        f"{_SPORTS_DB}/searchevents.php?"
                        + urlencode(
                            {
                                "e": f"{search_home}_vs_{search_away}",
                                "s": prior_season,
                            }
                        ),
                        context,
                        source="thesportsdb:h2h_search",
                    )
                    sources.append(source)
                    for event in self._provider_rows(source, "event", self._event_row):
                        if (
                            str(event.get("season") or "") != prior_season
                            or normalize_competition_name(
                                str(event.get("competition") or "")
                            )
                            != competition_name
                            or not _is_completed_event(event)
                        ):
                            continue
                        has_home = side_matches(event, team_a, team_ids[0], "home")
                        has_away = side_matches(event, team_b, team_ids[1], "away")
                        reverse_home = side_matches(event, team_a, team_ids[0], "away")
                        reverse_away = side_matches(event, team_b, team_ids[1], "home")
                        if not ((has_home and has_away) or (reverse_home and reverse_away)):
                            continue
                        event_id = str(event.get("event_id") or "")
                        if event_id and event_id in seen_event_ids:
                            continue
                        if event_id:
                            seen_event_ids.add(event_id)
                        searched.append(event)
                h2h = searched[:10]

        previous_match_details: list[dict[str, Any]] = []
        detail_cache: dict[str, SourceResult] = {}
        for team in (team_a, team_b):
            previous = next(iter(team_events.get(team, [])), None)
            if previous is None or not str(previous.get("event_id") or "").isdigit():
                continue
            event_id = str(previous["event_id"])
            detail = detail_cache.get(event_id)
            if detail is None:
                detail = await self._source_json(
                    f"{_SPORTS_DB}/lookupevent.php?{urlencode({'id': event_id})}",
                    context,
                    source="thesportsdb:event_details",
                )
                detail_cache[event_id] = detail
                sources.append(detail)
            parsed = self._event_detail(detail)
            previous_match_details.append(
                {
                    "team": team,
                    "match": previous,
                    "details": parsed,
                    "lineup_status": "available"
                    if parsed.get("lineups")
                    else "not_provided_by_source",
                }
            )
        return sources, form, h2h, previous_match_details

    @staticmethod
    def _event_detail(source: SourceResult) -> dict[str, Any]:
        if source.status != "success" or not isinstance(source.data, dict):
            return {}
        rows = source.data.get("events") or []
        if not rows or not isinstance(rows[0], dict):
            return {}
        event = rows[0]
        lineup_fields = (
            "LineupGoalkeeper",
            "LineupDefense",
            "LineupMidfield",
            "LineupForward",
            "LineupSubstitutes",
        )
        lineups: dict[str, dict[str, str]] = {}
        for side in ("Home", "Away"):
            values = {
                field.removeprefix("Lineup").lower(): str(event.get(f"str{side}{field}") or "")
                for field in lineup_fields
                if str(event.get(f"str{side}{field}") or "").strip()
            }
            if values:
                lineups[side.casefold()] = values
        return {
            "event_id": event.get("idEvent"),
            "match": event.get("strEvent"),
            "date": event.get("dateEvent"),
            "home": event.get("strHomeTeam"),
            "away": event.get("strAwayTeam"),
            "formations": {
                "home": event.get("strHomeFormation"),
                "away": event.get("strAwayFormation"),
            },
            "lineups": lineups,
        }

    async def _resolve_tsdb_league(
        self, competition: FootballCompetition, context: ExecutionContext
    ) -> tuple[str, SourceResult]:
        provider_country = competition.provider_country or competition.country
        data = await self._source_json(
            f"{_SPORTS_DB}/search_all_leagues.php?"
            + urlencode({"s": "Soccer", "c": provider_country}),
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
            expected_country = normalize_competition_name(provider_country)
            if country != expected_country:
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
        allow_adjacent_date: bool = False,
    ) -> dict[str, Any] | None:
        requested_date = CalendarDate.fromisoformat(date) if date else None
        allowed_dates = {requested_date} if requested_date is not None else set()
        if allow_adjacent_date and requested_date is not None:
            allowed_dates.update(
                (requested_date - timedelta(days=1), requested_date + timedelta(days=1))
            )
        normalized_season = normalize_football_season(season)
        rows = []
        for row in data.get("events") or []:
            if not isinstance(row, dict):
                continue
            home_name = str(row.get("strHomeTeam") or "")
            away_name = str(row.get("strAwayTeam") or "")
            home_id = str(row.get("idHomeTeam") or "")
            away_id = str(row.get("idAwayTeam") or "")
            actual_season = normalize_football_season(str(row.get("strSeason") or ""))
            actual_league = normalize_competition_name(str(row.get("strLeague") or ""))
            try:
                actual_date = CalendarDate.fromisoformat(str(row.get("dateEvent") or ""))
            except ValueError:
                continue
            if (
                not football_teams_match(
                    team_a,
                    home_name,
                    right_provider="thesportsdb",
                    right_provider_id=home_id,
                )
                or not football_teams_match(
                    team_b,
                    away_name,
                    right_provider="thesportsdb",
                    right_provider_id=away_id,
                )
                or (allowed_dates and actual_date not in allowed_dates)
            ):
                continue
            if actual_season and actual_season != normalized_season:
                continue
            if actual_league != normalize_competition_name(competition.name):
                continue
            if not str(row.get("idEvent") or "").isdigit():
                continue
            fixture = cls._event_row(row)
            fixture["requested_date"] = date or actual_date.isoformat()
            fixture["date_alignment"] = (
                "discovered_from_season_schedule"
                if requested_date is None
                else "exact"
                if actual_date == requested_date
                else "adjacent_source_date"
            )
            rows.append(fixture)
        return rows[0] if len(rows) == 1 else None

    async def _espn_qualifying_fixture(
        self,
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
        context: ExecutionContext,
    ) -> tuple[dict[str, Any] | None, SourceResult, str]:
        """Find a fixture in a reviewed, competition-specific qualifying feed."""

        slug = competition.espn_qualifying_slug
        if not slug:
            return (
                None,
                SourceResult("espn:qualifying_schedule", "unavailable"),
                "unavailable",
            )
        if date:
            requested = CalendarDate.fromisoformat(date)
            start = requested - timedelta(days=1)
            end = requested + timedelta(days=1)
        else:
            try:
                start_year = int(normalize_football_season(season)[:4])
            except ValueError:
                return (
                    None,
                    SourceResult(
                        "espn:qualifying_schedule",
                        "rejected",
                        error_code="qualifying_season_invalid",
                    ),
                    "unavailable",
                )
            start = CalendarDate(start_year, 6, 1)
            end = CalendarDate(start_year, 9, 15)
        date_scope = f"{start:%Y%m%d}-{end:%Y%m%d}"
        schedule = await self._source_json(
            f"{_ESPN_SOCCER}/{slug}/scoreboard?"
            + urlencode({"dates": date_scope, "limit": 100}),
            context,
            source="espn:qualifying_schedule",
            fetcher=self._espn_fetcher,
        )
        if schedule.status != "success" or not isinstance(schedule.data, dict):
            return None, schedule, "unavailable"
        if self._espn_league_slug(schedule.data) != slug:
            return (
                None,
                SourceResult(
                    "espn:qualifying_schedule",
                    "rejected",
                    error_code="espn_qualifying_league_mismatch",
                    safe_reason="ESPN qualifying data did not match the reviewed mapping",
                ),
                "unavailable",
            )

        candidates = self._select_espn_qualifying_fixtures(
            schedule.data,
            competition,
            season=season,
            date=date,
            team_a=team_a,
            team_b=team_b,
            espn_slug=slug,
        )
        if len(candidates) == 1:
            return (
                candidates[0],
                SourceResult("espn:qualifying_schedule", "success"),
                "confirmed",
            )
        if not candidates:
            return (
                None,
                SourceResult(
                    "espn:qualifying_schedule",
                    "no_match",
                    error_code="qualifying_fixture_not_found",
                    safe_reason=(
                        "The reviewed qualifying feed did not contain the requested fixture"
                    ),
                ),
                "no_match",
            )
        return (
            None,
            SourceResult(
                "espn:qualifying_schedule",
                "rejected",
                error_code="qualifying_fixture_ambiguous",
                safe_reason="The reviewed qualifying feed returned multiple matching fixtures",
            ),
            "ambiguous",
        )

    async def _espn_primary_fixture(
        self,
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
        context: ExecutionContext,
    ) -> tuple[dict[str, Any] | None, SourceResult, str]:
        """Confirm a domestic or main-stage fixture from its reviewed ESPN feed.

        TheSportsDB may temporarily omit a reviewed league from its country
        catalog. That provider failure must not suppress a fixture that ESPN
        confirms under the Runtime-owned competition slug.
        """

        if not date:
            return (
                None,
                SourceResult(
                    "espn:primary_schedule",
                    "unavailable",
                    error_code="espn_primary_date_required",
                    safe_reason="A date is required for the bounded ESPN fixture fallback",
                ),
                "unavailable",
            )
        requested = CalendarDate.fromisoformat(date)
        date_scope = (
            f"{requested - timedelta(days=1):%Y%m%d}-"
            f"{requested + timedelta(days=1):%Y%m%d}"
        )
        slug = competition.espn_slug
        schedule = await self._source_json(
            f"{_ESPN_SOCCER}/{slug}/scoreboard?"
            + urlencode({"dates": date_scope, "limit": 100}),
            context,
            source="espn:primary_schedule",
            fetcher=self._espn_fetcher,
        )
        if schedule.status != "success" or not isinstance(schedule.data, dict):
            return None, schedule, "unavailable"
        if self._espn_league_slug(schedule.data) != slug:
            return (
                None,
                SourceResult(
                    "espn:primary_schedule",
                    "rejected",
                    error_code="espn_primary_league_mismatch",
                    safe_reason="ESPN data did not match the reviewed competition mapping",
                ),
                "unavailable",
            )
        candidates = self._select_espn_qualifying_fixtures(
            schedule.data,
            competition,
            season=season,
            date=date,
            team_a=team_a,
            team_b=team_b,
            espn_slug=slug,
        )
        if len(candidates) == 1:
            return candidates[0], SourceResult("espn:primary_schedule", "success"), "confirmed"
        if not candidates:
            return (
                None,
                SourceResult(
                    "espn:primary_schedule",
                    "no_match",
                    error_code="espn_primary_fixture_not_found",
                    safe_reason="The reviewed ESPN feed did not contain the requested fixture",
                ),
                "no_match",
            )
        return (
            None,
            SourceResult(
                "espn:primary_schedule",
                "rejected",
                error_code="espn_primary_fixture_ambiguous",
                safe_reason="The reviewed ESPN feed returned multiple matching fixtures",
            ),
            "ambiguous",
        )

    @classmethod
    def _select_espn_qualifying_fixtures(
        cls,
        data: dict[str, Any],
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
        espn_slug: str,
    ) -> list[dict[str, Any]]:
        requested_date = CalendarDate.fromisoformat(date) if date else None
        allowed_dates = set()
        if requested_date is not None:
            allowed_dates = {
                requested_date - timedelta(days=1),
                requested_date,
                requested_date + timedelta(days=1),
            }
        rows: list[dict[str, Any]] = []
        for raw in data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            row = cls._espn_event_row(raw)
            teams = row.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            event_id = str(row.get("event_id") or "")
            try:
                actual_datetime = datetime.fromisoformat(
                    str(row.get("date_utc") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            actual_date = actual_datetime.date()
            if (
                not event_id.isdigit()
                or (allowed_dates and actual_date not in allowed_dates)
                or not football_teams_match(
                    team_a,
                    str(home.get("name") or ""),
                    right_provider="espn",
                    right_provider_id=home.get("id"),
                )
                or not football_teams_match(
                    team_b,
                    str(away.get("name") or ""),
                    right_provider="espn",
                    right_provider_id=away.get("id"),
                )
            ):
                continue
            home_name = str(home.get("name") or "")
            away_name = str(away.get("name") or "")
            home_identity = resolve_team_identity(
                home_name, provider="espn", provider_id=str(home.get("id") or "")
            )
            away_identity = resolve_team_identity(
                away_name, provider="espn", provider_id=str(away.get("id") or "")
            )
            rows.append(
                {
                    "event_id": event_id,
                    "competition": competition.name,
                    "season": normalize_football_season(season),
                    "match": f"{home_name} vs {away_name}",
                    "home": home_name,
                    "away": away_name,
                    "home_team_id": str(home.get("id") or "") or None,
                    "away_team_id": str(away.get("id") or "") or None,
                    "home_canonical_id": home_identity.canonical_id,
                    "away_canonical_id": away_identity.canonical_id,
                    "home_score": home.get("score"),
                    "away_score": away.get("score"),
                    "date": actual_date.isoformat(),
                    "time_utc": actual_datetime.astimezone(UTC).strftime("%H:%M:%S"),
                    "round": row.get("stage"),
                    "stage": row.get("stage"),
                    "leg": row.get("leg"),
                    "venue": row.get("venue"),
                    "status": row.get("status"),
                    "requested_date": date or actual_date.isoformat(),
                    "date_alignment": (
                        "discovered_from_espn_qualifying"
                        if requested_date is None
                        else "exact"
                        if actual_date == requested_date
                        else "adjacent_source_date"
                    ),
                    "espn_slug": espn_slug,
                }
            )
        return rows

    async def _espn_evidence(
        self,
        competition: FootballCompetition,
        fixture: dict[str, Any],
        *,
        date: str,
        context: ExecutionContext,
    ) -> tuple[SourceResult, dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        empty: tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]] = ({}, [], [])
        espn_slug = str(fixture.get("espn_slug") or competition.espn_slug)
        schedule = await self._source_json(
            f"{_ESPN_SOCCER}/{espn_slug}/scoreboard?"
            + urlencode({"dates": date.replace("-", "")}),
            context,
            source="espn",
            fetcher=self._espn_fetcher,
        )
        if schedule.status != "success" or not isinstance(schedule.data, dict):
            return schedule, *empty
        if self._espn_league_slug(schedule.data) != espn_slug:
            return self._espn_rejected("espn_league_mismatch"), *empty
        candidates = []
        wanted_home = str(fixture.get("home") or "")
        wanted_away = str(fixture.get("away") or "")
        for raw in schedule.data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            row = self._espn_event_row(raw)
            teams = row.get("teams") or {}
            actual_home = str((teams.get("home") or {}).get("name") or "")
            actual_away = str((teams.get("away") or {}).get("name") or "")
            event_id = str(row.get("event_id") or "")
            if (
                football_teams_match(
                    wanted_home,
                    actual_home,
                    right_provider="espn",
                    right_provider_id=(teams.get("home") or {}).get("id"),
                )
                and football_teams_match(
                    wanted_away,
                    actual_away,
                    right_provider="espn",
                    right_provider_id=(teams.get("away") or {}).get("id"),
                )
                and str(row.get("date_utc") or "")[:10] == date
                and event_id.isdigit()
            ):
                candidates.append(event_id)
        if len(candidates) != 1:
            return self._espn_rejected("espn_fixture_mismatch"), *empty
        summary = await self._source_json(
            f"{_ESPN_SOCCER}/{espn_slug}/summary?"
            + urlencode({"event": candidates[0]}),
            context,
            source="espn",
            fetcher=self._espn_fetcher,
        )
        if summary.status != "success" or not isinstance(summary.data, dict):
            return summary, *empty
        if self._espn_league_slug(summary.data) != espn_slug:
            return self._espn_rejected("espn_league_mismatch"), *empty
        header = summary.data.get("header") or {}
        events = header.get("competitions") or []
        if not events or not isinstance(events[0], dict):
            return self._espn_rejected("espn_unexpected_payload"), *empty
        event = events[0]
        teams = self._espn_competitors(event)
        if (
            not football_teams_match(
                wanted_home,
                str((teams.get("home") or {}).get("name") or ""),
                right_provider="espn",
                right_provider_id=(teams.get("home") or {}).get("id"),
            )
            or not football_teams_match(
                wanted_away,
                str((teams.get("away") or {}).get("name") or ""),
                right_provider="espn",
                right_provider_id=(teams.get("away") or {}).get("id"),
            )
            or str(event.get("date") or "")[:10] != date
        ):
            return self._espn_rejected("espn_fixture_mismatch"), *empty
        game_info = summary.data.get("gameInfo") or {}
        header_season = header.get("season") or {}
        notes = event.get("notes") or []
        leg = next(
            (
                str(note.get("headline") or note.get("text") or "").strip()
                for note in notes
                if isinstance(note, dict)
                and str(note.get("headline") or note.get("text") or "").strip()
            ),
            "",
        )
        stage = str(
            header_season.get("name")
            or header_season.get("slug")
            or fixture.get("stage")
            or ""
        ).strip()
        if stage:
            fixture["stage"] = stage
            if not fixture.get("round"):
                fixture["round"] = stage
        if leg:
            fixture["leg"] = leg
        news_payload = summary.data.get("news") or []
        news_items = (
            news_payload.get("articles") or []
            if isinstance(news_payload, dict)
            else news_payload
            if isinstance(news_payload, list)
            else []
        )
        details = {
            "source_event_id": candidates[0],
            "venue": (game_info.get("venue") or {}).get("fullName"),
            "attendance": game_info.get("attendance"),
            "status": ((event.get("status") or {}).get("type") or {}).get("description"),
            "stage": stage or None,
            "leg": leg or fixture.get("leg"),
            "espn_odds": self._espn_odds(summary.data),
            "roster_teams": [
                (roster.get("team") or {}).get("displayName")
                for roster in (summary.data.get("rosters") or [])
                if isinstance(roster, dict)
                and (roster.get("team") or {}).get("displayName")
            ],
            "news": [
                {
                    "headline": item.get("headline"),
                    "published": item.get("published"),
                    "link": (
                        ((item.get("links") or {}).get("web") or {}).get("href")
                    ),
                }
                for item in news_items[:3]
                if isinstance(item, dict)
            ],
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
            _SPORTTERY,
            context,
            source="sporttery:getMatchCalculatorV1",
            fetcher=self._sporttery_fetcher,
        )
        if source.status != "success" or not isinstance(source.data, dict):
            return source
        if source.data.get("errorCode") not in (0, "0", None):
            return SourceResult(
                "sporttery:getMatchCalculatorV1",
                "rejected",
                error_code="sporttery_business_rejected",
                safe_reason="Sporttery rejected the calculator request",
            )
        matches = []
        for day in (source.data.get("value") or {}).get("matchInfoList") or []:
            if not isinstance(day, dict):
                continue
            for match in day.get("subMatchList") or []:
                if not isinstance(match, dict):
                    continue
                home_names = _provider_team_names(match, "home")
                away_names = _provider_team_names(match, "away")
                if any(football_teams_match(team_a, name) for name in home_names) and any(
                    football_teams_match(team_b, name) for name in away_names
                ):
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
                "no_match",
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
        selected_fetcher = fetcher or self._fetcher
        cache_key = (context.root_execution_id, id(selected_fetcher), url)
        cached = self._source_cache.get(cache_key)
        if cached is not None:
            return self._relabeled_source(cached, source)
        lock = self._source_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._source_cache.get(cache_key)
            if cached is not None:
                return self._relabeled_source(cached, source)
            result = await self._fetch_source_json(
                selected_fetcher,
                url,
                context,
                source=source,
            )
            # The underlying Web fetcher already performs bounded retries.
            # Reuse failures too within this root run so three parallel
            # fixtures do not turn one provider 429 into three retry storms;
            # each fixture can immediately proceed to its reviewed fallback.
            self._source_cache[cache_key] = result
            while len(self._source_cache) > 256:
                oldest = next(iter(self._source_cache))
                self._source_cache.pop(oldest, None)
                self._source_locks.pop(oldest, None)
            return result

    async def _fetch_source_json(
        self,
        fetcher: _Fetcher,
        url: str,
        context: ExecutionContext,
        *,
        source: str,
    ) -> SourceResult:
        try:
            fetched = await fetcher.execute({"url": url}, context)
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
            provider = source.split(":", 1)[0]
            allowed = {
                "espn_http_403",
                "espn_curl_unavailable",
                "espn_timeout",
                "espn_response_too_large",
            }
            if raw_code == "web_http_429":
                code = f"{provider}_rate_limited"
                reason = "The source rate-limited this request after bounded retries"
            elif raw_code.startswith("web_http_"):
                code = f"{provider}_{raw_code.removeprefix('web_')}"
                reason = "The source returned an unsuccessful HTTP status"
            else:
                code = raw_code if raw_code in allowed else f"{provider}_request_failed"
                reason = "The source request did not complete"
            return SourceResult(
                source,
                "unavailable",
                error_code=code,
                safe_reason=reason,
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
    def _relabeled_source(result: SourceResult, source: str) -> SourceResult:
        return SourceResult(
            source=source,
            status=result.status,
            data=result.data,
            as_of=result.as_of,
            error_code=result.error_code,
            safe_reason=result.safe_reason,
            quota=result.quota,
        )

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
        return [
            row
            for row in source.data
            if isinstance(row, dict)
            and football_teams_match(
                str(row.get("home_team") or ""), str(fixture.get("home") or "")
            )
            and football_teams_match(
                str(row.get("away_team") or ""), str(fixture.get("away") or "")
            )
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
            "previous_match_details": [],
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

    @staticmethod
    def _fixture_no_match(
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
        sources: list[SourceResult],
        warnings: list[str],
    ) -> ToolResult:
        payload = {
            "status": "no_match",
            "fixture": None,
            "requested": {
                "competition": competition.name,
                "season": season,
                "date": date or None,
                "home": team_a,
                "away": team_b,
            },
            "sources": [source.report() for source in sources],
            "warnings": [*warnings, "requested_fixture_not_confirmed_in_season_schedule"],
            "completeness": "unavailable",
            "as_of": utc_now(),
        }
        return ToolResult(
            content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            metadata={"status": "no_match", "evidenceGate": "fixture_unconfirmed"},
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
            if action == "results":
                return await self._espn_results(
                    competition,
                    arguments=arguments,
                    context=context,
                    primary_source=source,
                )
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
            date = str(arguments.get("date") or "").strip()
            if date:
                day_data, day_error = await self._get_json(
                    f"{_SPORTS_DB}/eventsday.php?"
                    f"{urlencode({'d': date, 'l': league_id})}",
                    context,
                )
                day_rows = day_data.get("events") if day_error is None else None
                if isinstance(day_rows, list) and day_rows:
                    rows = [
                        self._event_row(row)
                        for row in day_rows[:limit]
                        if isinstance(row, dict)
                    ]
                    return self._success(
                        {
                            "source": "thesportsdb:eventsday.php",
                            "action": action,
                            "league_id": league_id,
                            "date": date,
                            "count": len(rows),
                            "rows": rows,
                        }
                    )
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
        if action == "results" and not rows:
            return await self._espn_results(
                competition,
                arguments=arguments,
                context=context,
                primary_source=SourceResult(
                    f"thesportsdb:{path.split('?', 1)[0]}",
                    "no_match",
                    error_code="results_empty",
                    safe_reason="The primary results source returned no rows",
                ),
            )
        return self._success(
            {
                "source": f"thesportsdb:{path.split('?', 1)[0]}",
                "action": action,
                "league_id": league_id,
                "count": len(rows),
                "rows": rows,
            }
        )

    async def _espn_results(
        self,
        competition: FootballCompetition,
        *,
        arguments: dict[str, Any],
        context: ExecutionContext,
        primary_source: SourceResult,
    ) -> ToolResult:
        """Fetch dated results from reviewed ESPN feeds after a TSDB miss.

        Qualification feeds are Runtime-owned mappings. Models cannot provide a
        slug, and a plain competition label checks the main feed before the
        qualification feed. Explicit qualifying labels reverse that order.
        """

        date = str(arguments.get("date") or "").strip()
        season = str(arguments.get("season") or "").strip()
        if not season:
            return self._error("results requires season")
        try:
            requested_date = CalendarDate.fromisoformat(date)
        except ValueError:
            return self._error("results requires date in YYYY-MM-DD format")

        query = str(arguments.get("competition") or "")
        qualifying_requested = bool(
            re.search(
                r"qualif|play.?off|资格赛|附加赛",
                query,
                re.IGNORECASE,
            )
        )
        feed_candidates: list[tuple[str, str]] = []
        if qualifying_requested and competition.espn_qualifying_slug:
            feed_candidates.append((competition.espn_qualifying_slug, "qualifying"))
        feed_candidates.append((competition.espn_slug, "primary"))
        if not qualifying_requested and competition.espn_qualifying_slug:
            feed_candidates.append((competition.espn_qualifying_slug, "qualifying"))

        unique_feeds: list[tuple[str, str]] = []
        seen_slugs: set[str] = set()
        for slug, kind in feed_candidates:
            if slug and slug not in seen_slugs:
                seen_slugs.add(slug)
                unique_feeds.append((slug, kind))

        sources = [primary_source.report()]
        successful_feed = False
        for slug, kind in unique_feeds:
            source_name = f"espn:{kind}_results"
            schedule = await self._source_json(
                f"{_ESPN_SOCCER}/{slug}/scoreboard?"
                + urlencode({"dates": f"{requested_date:%Y%m%d}", "limit": 100}),
                context,
                source=source_name,
                fetcher=self._espn_fetcher,
            )
            sources.append(schedule.report())
            if schedule.status != "success" or not isinstance(schedule.data, dict):
                continue
            if self._espn_league_slug(schedule.data) != slug:
                sources.append(
                    SourceResult(
                        source_name,
                        "rejected",
                        error_code="espn_results_league_mismatch",
                        safe_reason=(
                            "ESPN results did not match the reviewed competition mapping"
                        ),
                    ).report()
                )
                continue
            successful_feed = True
            rows = self._espn_result_rows(
                schedule.data,
                competition,
                season=season,
                date=date,
                espn_slug=slug,
            )
            if rows:
                return self._success(
                    {
                        "source": source_name,
                        "action": "results",
                        "date": date,
                        "count": len(rows),
                        "rows": rows,
                        "sources": sources,
                        "fallback": "thesportsdb_to_espn",
                    }
                )
            if qualifying_requested and kind == "qualifying":
                break

        if successful_feed:
            result = self._success(
                {
                    "source": "espn:results",
                    "action": "results",
                    "status": "no_match",
                    "date": date,
                    "count": 0,
                    "rows": [],
                    "sources": sources,
                    "fallback": "thesportsdb_to_espn",
                }
            )
            return result.model_copy(
                update={"metadata": {"status": "no_match", "errorCode": "results_no_match"}}
            )

        return ToolResult(
            content=json.dumps(
                {
                    "error": "competition results unavailable from reviewed sources",
                    "action": "results",
                    "date": date,
                    "sources": sources,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            is_error=True,
            metadata={"errorCode": "football_results_unavailable"},
        )

    @classmethod
    def _espn_result_rows(
        cls,
        data: dict[str, Any],
        competition: FootballCompetition,
        *,
        season: str,
        date: str,
        espn_slug: str,
    ) -> list[dict[str, Any]]:
        requested_date = CalendarDate.fromisoformat(date)
        rows: list[dict[str, Any]] = []
        for raw in data.get("events") or []:
            if not isinstance(raw, dict):
                continue
            event = cls._espn_event_row(raw)
            try:
                actual_datetime = datetime.fromisoformat(
                    str(event.get("date_utc") or "").replace("Z", "+00:00")
                )
            except ValueError:
                continue
            if actual_datetime.date() != requested_date:
                continue
            teams = event.get("teams") or {}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            home_name = str(home.get("name") or "").strip()
            away_name = str(away.get("name") or "").strip()
            event_id = str(event.get("event_id") or "")
            if not event_id.isdigit() or not home_name or not away_name:
                continue
            home_identity = resolve_team_identity(
                home_name,
                provider="espn",
                provider_id=str(home.get("id") or ""),
            )
            away_identity = resolve_team_identity(
                away_name,
                provider="espn",
                provider_id=str(away.get("id") or ""),
            )
            raw_status = str(event.get("status") or "").strip()
            status = (
                "FT"
                if re.search(r"\bft\b|full\s*time|finished|final", raw_status, re.IGNORECASE)
                else "NS"
                if re.search(r"scheduled|not\s*started|pre", raw_status, re.IGNORECASE)
                else raw_status
            )
            rows.append(
                {
                    "event_id": event_id,
                    "competition": competition.name,
                    "season": normalize_football_season(season),
                    "match": f"{home_name} vs {away_name}",
                    "home": home_name,
                    "away": away_name,
                    "home_team_id": str(home.get("id") or "") or None,
                    "away_team_id": str(away.get("id") or "") or None,
                    "home_canonical_id": home_identity.canonical_id,
                    "away_canonical_id": away_identity.canonical_id,
                    "home_score": cls._espn_score(home.get("score")),
                    "away_score": cls._espn_score(away.get("score")),
                    "date": requested_date.isoformat(),
                    "time_utc": actual_datetime.astimezone(UTC).strftime("%H:%M:%S"),
                    "round": event.get("stage"),
                    "stage": event.get("stage"),
                    "leg": event.get("leg"),
                    "venue": event.get("venue"),
                    "status": status,
                    "status_detail": raw_status,
                    "espn_slug": espn_slug,
                }
            )
        return rows

    @staticmethod
    def _espn_score(value: Any) -> str | None:
        if isinstance(value, dict):
            value = value.get("value") or value.get("displayValue")
        text = str(value).strip() if value is not None else ""
        return text if re.fullmatch(r"\d+", text) else None

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
        rows = [
            self._event_row(row)
            for row in data.get("results") or []
            if isinstance(row, dict)
            and (
                _normalize_team_identity(str(row.get("strHomeTeam") or ""))
                == _normalize_team_identity(team_b)
                or _normalize_team_identity(str(row.get("strAwayTeam") or ""))
                == _normalize_team_identity(team_b)
            )
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
        data, error = await self._get_json(
            _SPORTTERY, context, fetcher=self._sporttery_fetcher
        )
        if error is not None:
            return error
        if data.get("errorCode") not in (0, "0", None):
            return ToolResult(
                content=json.dumps(
                    {
                        "source": "sporttery:getMatchCalculatorV1",
                        "status": "rejected",
                        "error_code": "sporttery_business_rejected",
                        "safe_reason": "Sporttery rejected the calculator request",
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                is_error=True,
                metadata={"errorCode": "sporttery_business_rejected"},
            )
        for day in (data.get("value") or {}).get("matchInfoList") or []:
            for match in day.get("subMatchList") or []:
                home_names = _provider_team_names(match, "home")
                away_names = _provider_team_names(match, "away")
                if not (
                    any(football_teams_match(team_a, name) for name in home_names)
                    and any(football_teams_match(team_b, name) for name in away_names)
                ):
                    continue
                return self._success(
                    {
                        "source": "sporttery:getMatchCalculatorV1",
                        "status": "success",
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
        return ToolResult(
            content=json.dumps(
                {
                    "source": "sporttery:getMatchCalculatorV1",
                    "status": "no_match",
                    "match": f"{team_a} vs {team_b}",
                    "found": False,
                    "coverage": "not found in the currently offered Sporttery fixture list",
                    "as_of": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            metadata={"status": "no_match"},
        )

    async def _team_id(self, team: str, context: ExecutionContext) -> tuple[str, ToolResult | None]:
        canonical_id = resolve_team_identity(team).canonical_id
        known_id = DEFAULT_TEAM_IDENTITY_RESOLVER.provider_id(
            canonical_id, provider="thesportsdb"
        )
        if known_id:
            return known_id, None
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
        home = str(row.get("strHomeTeam") or "")
        away = str(row.get("strAwayTeam") or "")
        home_id = str(row.get("idHomeTeam") or "")
        away_id = str(row.get("idAwayTeam") or "")
        home_identity = resolve_team_identity(
            home, provider="thesportsdb", provider_id=home_id
        )
        away_identity = resolve_team_identity(
            away, provider="thesportsdb", provider_id=away_id
        )
        return {
            "event_id": row.get("idEvent"),
            "competition": row.get("strLeague"),
            "season": row.get("strSeason"),
            "match": row.get("strEvent"),
            "home": home,
            "away": away,
            "home_team_id": home_id or None,
            "away_team_id": away_id or None,
            "home_canonical_id": home_identity.canonical_id,
            "away_canonical_id": away_identity.canonical_id,
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

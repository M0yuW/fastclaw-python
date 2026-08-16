"""Shared football evidence and odds tools for the competition team."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastclaw.execution import ExecutionContext, football_teams_match
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    resolve_football_competition,
)
from fastclaw.tools.football_evidence import OddsSource, SourceResult, TheOddsApiSource
from fastclaw.tools.football_shared import (
    FootballEvidenceCache,
    football_event_key,
    get_football_evidence_cache,
)


class FootballContextTool:
    """Read the base evidence published by the data Agent for this root run."""

    def __init__(self, *, cache: FootballEvidenceCache | None = None) -> None:
        self._cache = cache or get_football_evidence_cache()
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_context",
                description=(
                    "Read the already-confirmed football fixture and base evidence published "
                    "by the data analyst. This tool never performs network requests."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "competition": {"type": "string"},
                        "season": {"type": "string"},
                        "date": {"type": "string"},
                        "team_a": {"type": "string"},
                        "team_b": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        if context.shared_state.football_settlement_review:
            result = ToolResult(
                content=(
                    "football_context is forbidden during settlement review; use "
                    "football_data action=results"
                ),
                is_error=True,
                metadata={"errorCode": "football_settlement_context_forbidden"},
            )
            context.shared_state.football_settlement_errors.append(result.content)
            return result
        competition_name = str(arguments.get("competition") or "")
        trusted_competition = resolve_football_competition(competition_name)
        key = football_event_key(
            trusted_competition.name if trusted_competition is not None else competition_name,
            str(arguments.get("season") or ""),
            str(arguments.get("date") or ""),
            str(arguments.get("team_a") or ""),
            str(arguments.get("team_b") or ""),
        )
        resolved = context.shared_state.football_base_for_fixture(
            competition=key.competition,
            season=key.season,
            date=key.date,
            team_a=str(arguments.get("team_a") or ""),
            team_b=str(arguments.get("team_b") or ""),
        )
        if resolved is None:
            cached = await self._cache.find_fixture(
                "base",
                competition=key.competition,
                season=key.season,
                date=key.date,
                team_a=str(arguments.get("team_a") or ""),
                team_b=str(arguments.get("team_b") or ""),
            )
            if cached is not None:
                cached_key, cached_result = cached
                resolved = (cached_key.value, cached_result.content)
                context.shared_state.publish_football_base(
                    cached_key.value, cached_result.content
                )
                await self._cache.put_session_base(
                    context.session_id, cached_key, cached_result
                )
        if resolved is None:
            return ToolResult(
                content=json.dumps(
                    {"error": "base football evidence is not ready; ask the data analyst first"},
                    ensure_ascii=False,
                ),
                is_error=True,
                metadata={"errorCode": "football_context_not_ready"},
            )
        resolved_key, content = resolved
        return ToolResult(
            content=content,
            metadata={"shared": True, "cacheHit": True, "baseEvidenceKey": resolved_key},
        )


class FootballOddsTool:
    """Fetch only market odds after the data Agent has confirmed the fixture."""

    def __init__(
        self,
        odds_source: OddsSource | None = None,
        *,
        cache: FootballEvidenceCache | None = None,
    ) -> None:
        self._odds_source = odds_source or TheOddsApiSource.from_environment()
        self._cache = cache or get_football_evidence_cache()
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_odds",
                description=(
                    "Fetch timestamped market odds for the fixture already confirmed in "
                    "football_context. This is the only football market tool and never calls "
                    "TheSportsDB or Sporttery."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "competition": {"type": "string"},
                        "country": {"type": "string"},
                        "season": {"type": "string"},
                        "date": {"type": "string"},
                        "team_a": {"type": "string"},
                        "team_b": {"type": "string"},
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
                    "required": ["competition", "season", "date", "team_a", "team_b"],
                    "additionalProperties": False,
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        if context.shared_state.football_settlement_review:
            result = self._error(
                "football_odds is forbidden during settlement review; use "
                "football_data action=results"
            )
            context.shared_state.football_settlement_errors.append(result.content)
            return result
        competition_name = str(arguments.get("competition") or "").strip()
        country = str(arguments.get("country") or "").strip()
        season = str(arguments.get("season") or "").strip()
        date = str(arguments.get("date") or "").strip()
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not all((competition_name, season, date, team_a, team_b)):
            return self._error(
                "football_odds requires competition, season, date, team_a, and team_b"
            )
        competition = resolve_football_competition(competition_name, country=country)
        if competition is None:
            return self._error("competition is not in the trusted Runtime catalog")
        key = football_event_key(competition.name, season, date, team_a, team_b)
        resolved = context.shared_state.football_base_for_fixture(
            competition=competition.name,
            season=key.season,
            date=key.date,
            team_a=team_a,
            team_b=team_b,
        )
        if resolved is None:
            cached = await self._cache.find_fixture(
                "base",
                competition=competition.name,
                season=key.season,
                date=key.date,
                team_a=team_a,
                team_b=team_b,
            )
            if cached is not None:
                cached_key, cached_result = cached
                resolved = (cached_key.value, cached_result.content)
                context.shared_state.publish_football_base(
                    cached_key.value, cached_result.content
                )
                await self._cache.put_session_base(
                    context.session_id, cached_key, cached_result
                )
        if resolved is None:
            return self._error("football_context must confirm the fixture before football_odds")
        base_key, base_content = resolved
        cached = await self._cache.get("odds", key)
        if cached is not None:
            return cached
        try:
            base_payload = json.loads(base_content)
        except json.JSONDecodeError:
            return self._error("shared football evidence is malformed")
        fixture = base_payload.get("fixture")
        if not isinstance(fixture, dict):
            return self._error("shared football evidence has no confirmed fixture")

        regions = tuple(str(value) for value in arguments.get("regions") or ("eu",))
        markets = tuple(str(value) for value in arguments.get("markets") or ("h2h", "totals"))
        odds_format = str(arguments.get("odds_format") or "decimal")
        try:
            source = await self._odds_source.fetch(
                competition,
                regions=regions,
                markets=markets,
                odds_format=odds_format,
                commence_from=str(arguments.get("commence_from") or ""),
                commence_to=str(arguments.get("commence_to") or ""),
            )
        except Exception:
            source = SourceResult(
                "the_odds_api",
                "unavailable",
                error_code="odds_request_failed",
                safe_reason="The Odds API request did not complete",
            )
        matched = self._matching_odds(source, fixture, date)
        if source.status == "success" and not matched:
            source = SourceResult(
                "the_odds_api",
                "no_match",
                quota=source.quota,
                safe_reason="No matching odds fixture was returned",
            )
        result = ToolResult(
            content=json.dumps(
                {
                    "fixture": fixture,
                    "odds": matched if source.status == "success" else {"status": source.status},
                    "source": source.report(),
                    "base_evidence_key": base_key,
                    "as_of": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        await self._cache.put("odds", key, result, ttl_seconds=60)
        return result

    @staticmethod
    def _matching_odds(
        source: SourceResult, fixture: dict[str, Any], date: str
    ) -> list[dict[str, Any]]:
        if source.status != "success" or not isinstance(source.data, list):
            return []
        wanted_home = str(fixture.get("home") or "")
        wanted_away = str(fixture.get("away") or "")
        return [
            row
            for row in source.data
            if isinstance(row, dict)
            and football_teams_match(str(row.get("home_team") or ""), wanted_home)
            and football_teams_match(str(row.get("away_team") or ""), wanted_away)
            and (
                not row.get("commence_time")
                or str(row.get("commence_time"))[:10]
                in {
                    date,
                    str(fixture.get("date") or ""),
                    str(fixture.get("requested_date") or ""),
                }
            )
        ]

    @staticmethod
    def _error(message: str) -> ToolResult:
        return ToolResult(
            content=json.dumps({"error": message}, ensure_ascii=False),
            is_error=True,
            metadata={"errorCode": "football_odds_error"},
        )

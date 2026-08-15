"""Shared football evidence and odds tools for the competition team."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    normalize_competition_name,
    resolve_football_competition,
)
from fastclaw.tools.football_evidence import OddsSource, SourceResult, TheOddsApiSource, utc_now
from fastclaw.tools.football_shared import (
    FootballEvidenceCache,
    football_event_key,
    get_football_evidence_cache,
)


class FootballContextTool:
    """Read the base evidence published by the data Agent for this root run."""

    def __init__(self) -> None:
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
        key = football_event_key(
            str(arguments.get("competition") or ""),
            str(arguments.get("season") or ""),
            str(arguments.get("date") or ""),
            str(arguments.get("team_a") or ""),
            str(arguments.get("team_b") or ""),
        )
        content = context.shared_state.football_base(key.value)
        if content is None:
            return ToolResult(
                content=json.dumps(
                    {"error": "base football evidence is not ready; ask the data analyst first"},
                    ensure_ascii=False,
                ),
                is_error=True,
                metadata={"errorCode": "football_context_not_ready"},
            )
        return ToolResult(content=content, metadata={"shared": True, "cacheHit": True})


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
        competition_name = str(arguments.get("competition") or "").strip()
        country = str(arguments.get("country") or "").strip()
        season = str(arguments.get("season") or "").strip()
        date = str(arguments.get("date") or "").strip()
        team_a = str(arguments.get("team_a") or "").strip()
        team_b = str(arguments.get("team_b") or "").strip()
        if not all((competition_name, season, date, team_a, team_b)):
            return self._error("football_odds requires competition, season, date, team_a, and team_b")
        key = football_event_key(competition_name, season, date, team_a, team_b)
        base_content = context.shared_state.football_base(key.value)
        if base_content is None:
            return self._error("football_context must confirm the fixture before football_odds")
        cached = await self._cache.get("odds", key)
        if cached is not None:
            return cached
        competition = resolve_football_competition(competition_name, country=country)
        if competition is None:
            return self._error("competition is not in the trusted Runtime catalog")
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
                "empty",
                quota=source.quota,
                safe_reason="No matching odds fixture was returned",
            )
        result = ToolResult(
            content=json.dumps(
                {
                    "fixture": fixture,
                    "odds": matched if source.status == "success" else {"status": "unavailable"},
                    "source": source.report(),
                    "base_evidence_key": key.value,
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
    def _error(message: str) -> ToolResult:
        return ToolResult(
            content=json.dumps({"error": message}, ensure_ascii=False),
            is_error=True,
            metadata={"errorCode": "football_odds_error"},
        )

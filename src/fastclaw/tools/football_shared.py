"""Shared, short-lived football evidence cache used inside the Runtime."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from fastclaw.execution import football_team_identity, football_teams_match
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    canonical_competition_identity,
    normalize_competition_name,
)

# Version 4 adds ESPN qualifying stage/leg, current ``lastFiveGames`` form, and
# supplemental market evidence. Older persisted bundles must refresh instead
# of reusing the schedule-only qualifying payload.
FOOTBALL_EVIDENCE_SCHEMA_VERSION = 4


def football_evidence_is_current(content: str) -> bool:
    """Return whether persisted base evidence has the current history contract."""

    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("evidence_schema_version") == FOOTBALL_EVIDENCE_SCHEMA_VERSION
        and isinstance(payload.get("historical_context"), dict)
    )


@dataclass(frozen=True, slots=True)
class FootballEventKey:
    competition: str
    season: str
    date: str
    team_a: str
    team_b: str

    @property
    def value(self) -> str:
        return "|".join(
            (self.competition, self.season, self.date, self.team_a, self.team_b)
        )


def football_event_key(
    competition: str, season: str, date: str, team_a: str, team_b: str
) -> FootballEventKey:
    return FootballEventKey(
        competition=canonical_competition_identity(competition),
        season=normalize_football_season(season),
        date=normalize_competition_name(date),
        team_a=football_team_identity(team_a),
        team_b=football_team_identity(team_b),
    )


def normalize_football_season(value: str) -> str:
    """Normalize compact and expanded season labels to the provider form."""

    normalized = value.strip().replace("\u2013", "-").replace("\u2014", "-")
    match = re.fullmatch(r"(\d{4})\s*[-/]\s*(\d{2}|\d{4})", normalized)
    if match is None:
        return normalize_competition_name(normalized)
    start = int(match.group(1))
    end_value = match.group(2)
    if len(end_value) == 2:
        end = (start // 100) * 100 + int(end_value)
        if end < start:
            end += 100
    else:
        end = int(end_value)
    return f"{start:04d}-{end:04d}"


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    result: ToolResult
    expires_at: float


class FootballEvidenceCache:
    """In-process TTL cache; keys include the complete fixture identity."""

    def __init__(self, *, base_ttl_seconds: int = 300, max_entries: int = 256) -> None:
        self._base_ttl_seconds = base_ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[tuple[str, str], _CacheEntry] = {}

    async def get(self, namespace: str, key: FootballEventKey) -> ToolResult | None:
        entry = self._entries.get((namespace, key.value))
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop((namespace, key.value), None)
            return None
        return ToolResult(
            content=entry.result.content,
            is_error=entry.result.is_error,
            direct_return=entry.result.direct_return,
            metadata={**entry.result.metadata, "cacheHit": True},
        )

    async def find_fixture(
        self,
        namespace: str,
        *,
        competition: str,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
    ) -> tuple[FootballEventKey, ToolResult] | None:
        """Find a cached fixture across equivalent provider/team spellings.

        A new session has a new ``SharedExecutionState`` but the gateway keeps
        this short-lived cache.  Reusing a previously confirmed fixture avoids
        turning a transient provider 429 into a false "fixture missing" result.
        The cache is still TTL-bound, so this is not a permanent source of truth.
        """

        requested = football_event_key(competition, season, date, team_a, team_b)
        for (cached_namespace, raw_key), entry in tuple(self._entries.items()):
            if cached_namespace != namespace:
                continue
            if entry.expires_at <= time.monotonic():
                self._entries.pop((cached_namespace, raw_key), None)
                continue
            if namespace == "base" and not football_evidence_is_current(entry.result.content):
                # A gateway upgrade may leave an old in-memory entry alive until
                # its TTL expires. Never let specialists silently consume a
                # bundle that lacks the current historical-context contract.
                self._entries.pop((cached_namespace, raw_key), None)
                continue
            payload = _json_payload(entry.result.content)
            fixture = payload.get("fixture") if isinstance(payload, dict) else None
            if not _fixture_matches(
                fixture,
                competition=requested.competition,
                season=requested.season,
                date=requested.date,
                team_a=requested.team_a,
                team_b=requested.team_b,
            ):
                continue
            return (
                FootballEventKey(*raw_key.split("|", 4)),
                ToolResult(
                    content=entry.result.content,
                    is_error=entry.result.is_error,
                    direct_return=entry.result.direct_return,
                    metadata={**entry.result.metadata, "cacheHit": True, "crossSession": True},
                ),
            )
        return None

    async def get_session_base(
        self, session_id: str
    ) -> tuple[tuple[FootballEventKey, ToolResult], ...]:
        """Return confirmed base bundles previously published in this session.

        ``SharedExecutionState`` is intentionally scoped to one root run, but a
        user can continue the same session in a later run.  Keep that boundary
        while allowing the orchestration gate to rehydrate only evidence that
        was confirmed for this exact session.
        """

        namespace = _session_base_namespace(session_id)
        found: list[tuple[FootballEventKey, ToolResult]] = []
        for (cached_namespace, raw_key), entry in tuple(self._entries.items()):
            if cached_namespace != namespace:
                continue
            if entry.expires_at <= time.monotonic():
                self._entries.pop((cached_namespace, raw_key), None)
                continue
            try:
                key = FootballEventKey(*raw_key.split("|", 4))
            except ValueError:
                continue
            found.append(
                (
                    key,
                    ToolResult(
                        content=entry.result.content,
                        is_error=entry.result.is_error,
                        direct_return=entry.result.direct_return,
                        metadata={
                            **entry.result.metadata,
                            "cacheHit": True,
                            "sessionCache": True,
                        },
                    ),
                )
            )
        return tuple(found)

    async def put_session_base(
        self,
        session_id: str,
        key: FootballEventKey,
        result: ToolResult,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        """Remember a successful base bundle for continuation of one session."""

        await self.put(
            _session_base_namespace(session_id),
            key,
            result,
            ttl_seconds=ttl_seconds,
        )

    async def put(
        self,
        namespace: str,
        key: FootballEventKey,
        result: ToolResult,
        *,
        ttl_seconds: int | None = None,
    ) -> None:
        self._entries[(namespace, key.value)] = _CacheEntry(
            result=ToolResult(
                content=result.content,
                is_error=result.is_error,
                direct_return=result.direct_return,
                metadata=dict(result.metadata),
            ),
            expires_at=time.monotonic() + (ttl_seconds or self._base_ttl_seconds),
        )
        while len(self._entries) > self._max_entries:
            self._entries.pop(next(iter(self._entries)))


_DEFAULT_FOOTBALL_EVIDENCE_CACHE = FootballEvidenceCache()


def get_football_evidence_cache() -> FootballEvidenceCache:
    return _DEFAULT_FOOTBALL_EVIDENCE_CACHE


def _session_base_namespace(session_id: str) -> str:
    return f"base-session:{session_id}"


def _json_payload(content: str) -> dict[str, object]:
    import json

    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fixture_matches(
    fixture: object,
    *,
    competition: str,
    season: str,
    date: str,
    team_a: str,
    team_b: str,
) -> bool:
    if not isinstance(fixture, dict):
        return False
    return (
        normalize_competition_name(str(fixture.get("competition") or ""))
        == normalize_competition_name(competition)
        and normalize_football_season(str(fixture.get("season") or ""))
        == normalize_football_season(season)
        and normalize_competition_name(
            str(fixture.get("requested_date") or fixture.get("date") or "")
        )
        == normalize_competition_name(date)
        and _team_match(
            str(fixture.get("home") or ""),
            team_a,
            left_provider="thesportsdb",
            left_provider_id=fixture.get("home_team_id"),
        )
        and _team_match(
            str(fixture.get("away") or ""),
            team_b,
            left_provider="thesportsdb",
            left_provider_id=fixture.get("away_team_id"),
        )
    )


def _team_match(
    left: str,
    right: str,
    *,
    left_provider: str = "",
    left_provider_id: object = None,
) -> bool:
    return football_teams_match(
        left,
        right,
        left_provider=left_provider,
        left_provider_id=left_provider_id,
    )

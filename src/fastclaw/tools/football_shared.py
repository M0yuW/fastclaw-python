"""Shared, short-lived football evidence cache used inside the Runtime."""

from __future__ import annotations

import time
from dataclasses import dataclass

from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import normalize_competition_name


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
        competition=normalize_competition_name(competition),
        season=normalize_competition_name(season),
        date=normalize_competition_name(date),
        team_a=normalize_competition_name(team_a),
        team_b=normalize_competition_name(team_b),
    )


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

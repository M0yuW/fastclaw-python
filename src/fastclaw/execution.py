"""Trusted execution metadata propagated independently of model arguments."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from fastclaw.football_identity import football_team_identity, football_teams_match

__all__ = ("football_team_identity", "football_teams_match")


def _normalize_identifier(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^\w]+", "", normalized)


def _fixture_matches(
    content: str,
    *,
    competition: str,
    season: str,
    date: str,
    team_a: str,
    team_b: str,
) -> bool:
    try:
        payload: Any = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    fixture = payload.get("fixture") if isinstance(payload, dict) else None
    if not isinstance(fixture, dict):
        return False
    fixture_competition = str(fixture.get("competition") or "")
    fixture_season = str(fixture.get("season") or "")
    fixture_date = str(fixture.get("requested_date") or fixture.get("date") or "")
    return (
        _normalize_identifier(fixture_competition)
        == _normalize_identifier(competition)
        and fixture_season == season
        and _normalize_identifier(fixture_date) == _normalize_identifier(date)
        and football_teams_match(
            str(fixture.get("home") or ""),
            team_a,
            left_provider="thesportsdb",
            left_provider_id=fixture.get("home_team_id"),
        )
        and football_teams_match(
            str(fixture.get("away") or ""),
            team_b,
            left_provider="thesportsdb",
            left_provider_id=fixture.get("away_team_id"),
        )
    )


@dataclass(slots=True)
class SharedExecutionState:
    """Small mutable state shared by one root run and its delegated Agents."""

    football_base_evidence: dict[str, str] = field(default_factory=dict)
    football_negative_evidence: list[str] = field(default_factory=list)
    ev_requested: bool = False
    sporttery_requested: bool = False
    football_settlement_attempted: bool = False
    football_settlement_review: bool = False
    football_settlement_results: list[str] = field(default_factory=list)
    football_settlement_errors: list[str] = field(default_factory=list)

    def publish_football_base(self, key: str, content: str) -> None:
        self.football_base_evidence[key] = content

    def football_base(self, key: str) -> str | None:
        return self.football_base_evidence.get(key)

    def football_base_for_fixture(
        self,
        *,
        competition: str,
        season: str,
        date: str,
        team_a: str,
        team_b: str,
    ) -> tuple[str, str] | None:
        """Resolve shared evidence by the confirmed fixture, not raw aliases."""

        for key, content in self.football_base_evidence.items():
            if _fixture_matches(
                content,
                competition=competition,
                season=season,
                date=date,
                team_a=team_a,
                team_b=team_b,
            ):
                return key, content
        return None

    def has_football_base(self) -> bool:
        return bool(self.football_base_evidence)

    def publish_football_negative(self, content: str) -> None:
        self.football_negative_evidence.append(content)

    def has_football_negative(self) -> bool:
        return bool(self.football_negative_evidence)

    def render_football_base(self) -> str:
        if not self.football_base_evidence:
            return ""
        return next(reversed(self.football_base_evidence.values()))


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    user_id: str
    agent_id: str
    session_id: str
    root_execution_id: str
    call_path: tuple[str, ...] = ()
    task_id: str = ""
    shared_state: SharedExecutionState = field(default_factory=SharedExecutionState)


_execution: ContextVar[ExecutionContext | None] = ContextVar("fastclaw_execution", default=None)


@contextmanager
def use_execution(context: ExecutionContext) -> Iterator[ExecutionContext]:
    token: Token[ExecutionContext | None] = _execution.set(context)
    try:
        yield context
    finally:
        _execution.reset(token)


def require_execution() -> ExecutionContext:
    context = _execution.get()
    if context is None:
        raise RuntimeError("trusted execution context is missing")
    return context


def current_execution() -> ExecutionContext | None:
    return _execution.get()

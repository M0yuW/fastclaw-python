"""Trusted execution metadata propagated independently of model arguments."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from fastclaw.football_identity import football_team_identity, football_teams_match

__all__ = ("football_team_identity", "football_teams_match")

_FOOTBALL_EVIDENCE_STATUSES = frozenset(
    {"unknown", "unavailable", "no_match", "confirmed"}
)


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
    fixture_dates = tuple(
        str(fixture.get(name) or "") for name in ("requested_date", "date")
    )
    return (
        _normalize_identifier(fixture_competition)
        == _normalize_identifier(competition)
        and fixture_season == season
        and any(
            _normalize_identifier(fixture_date) == _normalize_identifier(date)
            for fixture_date in fixture_dates
            if fixture_date
        )
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
class FootballEvidenceState:
    """Current evidence observation for one canonical fixture."""

    fixture_key: str
    status: str = "unknown"
    attempt: int = 0
    content: str = ""
    requested_identity: dict[str, str] = field(default_factory=dict)
    confirmed_identity: dict[str, str] = field(default_factory=dict)
    source_statuses: tuple[str, ...] = ()
    updated_at: str = ""
    audit: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class SharedExecutionState:
    """Small mutable state shared by one root run and its delegated Agents."""

    football_base_evidence: dict[str, str] = field(default_factory=dict)
    football_negative_evidence: list[str] = field(default_factory=list)
    football_evidence_states: dict[str, FootballEvidenceState] = field(default_factory=dict)
    ev_requested: bool = False
    sporttery_requested: bool = False
    football_settlement_attempted: bool = False
    football_settlement_review: bool = False
    football_settlement_results: list[str] = field(default_factory=list)
    football_settlement_errors: list[str] = field(default_factory=list)

    def publish_football_observation(
        self,
        fixture_key: str,
        status: str,
        content: str = "",
        *,
        requested_identity: dict[str, str] | None = None,
        confirmed_identity: dict[str, str] | None = None,
        source_statuses: tuple[str, ...] = (),
    ) -> FootballEvidenceState:
        """Publish one keyed fixture observation with monotonic confirmation.

        A failed retry is still retained in ``audit`` but cannot replace a
        confirmed observation from the same root execution.  This is the
        runtime source of truth; the legacy negative list is retained only as
        a compatibility/audit surface for older callers.
        """

        if status not in _FOOTBALL_EVIDENCE_STATUSES:
            raise ValueError(f"unsupported football evidence status: {status}")
        state = self.football_evidence_states.setdefault(
            fixture_key,
            FootballEvidenceState(fixture_key=fixture_key),
        )
        previous_status = state.status
        state.attempt += 1
        now = datetime.now(UTC).isoformat()
        state.audit.append(
            {
                "attempt": state.attempt,
                "status": status,
                "previous_status": previous_status,
                "content": content,
                "updated_at": now,
            }
        )
        if previous_status == "confirmed" and status != "confirmed":
            state.updated_at = now
            return state
        state.status = status
        state.content = content
        payload = _json_object(content)
        if status == "confirmed" and content:
            evidence_key = str(payload.get("base_evidence_key") or fixture_key)
            self.football_base_evidence.setdefault(evidence_key, content)
        requested_from_payload = _identity_from_payload(
            payload.get("requested") if payload else None
        )
        confirmed_from_payload = _identity_from_payload(
            payload.get("fixture") if payload else None
        )
        source_statuses_from_payload = tuple(
            f"{item.get('source', '')}:{item.get('status', '')}"
            for item in (payload.get("sources") or () if payload else ())
            if isinstance(item, dict) and item.get("source")
        )
        state.requested_identity = dict(
            requested_identity or requested_from_payload or state.requested_identity
        )
        state.confirmed_identity = dict(
            confirmed_identity or confirmed_from_payload or state.confirmed_identity
        )
        state.source_statuses = tuple(source_statuses or source_statuses_from_payload)
        state.updated_at = now
        if status in {"unavailable", "no_match"} and content:
            self.football_negative_evidence.append(content)
        return state

    def publish_football_base(
        self,
        key: str,
        content: str,
        *,
        fixture_key: str | None = None,
    ) -> None:
        self.football_base_evidence[key] = content
        self.publish_football_observation(
            fixture_key or _legacy_fixture_key(content) or key,
            "confirmed",
            content,
        )

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

    def publish_football_negative(self, content: str, *, fixture_key: str = "") -> None:
        """Compatibility wrapper for older data-tool callers.

        New code should call ``publish_football_observation`` with its
        canonical fixture key.  The fallback key keeps legacy tests and
        persisted child implementations from bypassing the state machine.
        """

        derived_key = fixture_key or _legacy_fixture_key(content)
        if derived_key:
            self.publish_football_observation(derived_key, "no_match", content)
        else:
            # Older callers may omit an identity when reporting no_match. Keep
            # that compatibility path inside the keyed state machine so a
            # current legacy result can still be scoped by its attempt without
            # resurrecting every negative from the session.
            self.publish_football_observation(
                f"legacy:{len(self.football_negative_evidence)}",
                "no_match",
                content,
            )

    def publish_football_unavailable(self, content: str, *, fixture_key: str) -> None:
        self.publish_football_observation(fixture_key, "unavailable", content)

    def football_fixture_state(self, fixture_key: str) -> FootballEvidenceState | None:
        return self.football_evidence_states.get(fixture_key)

    def unresolved_football_fixtures(self) -> tuple[FootballEvidenceState, ...]:
        return tuple(
            state
            for state in self.football_evidence_states.values()
            if state.status in {"unknown", "unavailable", "no_match"}
        )

    def football_delegation_summary(
        self, *, fixture_keys: Collection[str] | None = None
    ) -> dict[str, Any]:
        """Return a compact per-fixture state summary for orchestration.

        A continued session may rehydrate confirmed evidence from several
        earlier user requests.  Callers handling one new data delegation must
        pass the fixture keys touched by that delegation; otherwise an old
        confirmed fixture can incorrectly make a new failed delegation look
        successful.
        """

        selected_keys = set(fixture_keys) if fixture_keys is not None else None
        states = [
            state
            for key, state in self.football_evidence_states.items()
            if selected_keys is None or key in selected_keys
        ]
        if selected_keys is None and not states and self.football_negative_evidence:
            # Read-only compatibility for older child handlers that still call
            # publish_football_negative without a key.
            states = [
                FootballEvidenceState(
                    fixture_key=_legacy_fixture_key(content) or f"legacy:{index}",
                    status="no_match",
                    content=content,
                )
                for index, content in enumerate(self.football_negative_evidence)
            ]
        confirmed = [self._state_summary(state) for state in states if state.status == "confirmed"]
        unresolved = [
            self._state_summary(state)
            for state in states
            if state.status in {"unknown", "unavailable", "no_match"}
        ]
        source_failures = [
            item
            for item in unresolved
            if item["status"] in {"unknown", "unavailable"}
        ]
        if confirmed and not unresolved:
            status = "confirmed"
        elif confirmed:
            status = "partial"
        elif states and all(state.status == "no_match" for state in states):
            status = "no_match"
        elif source_failures and any(state.status == "no_match" for state in states):
            status = "partial_failure"
        elif source_failures:
            status = "unavailable"
        else:
            status = "unknown"
        next_action = (
            "delegate_specialists"
            if confirmed
            else "retry_unavailable"
            if source_failures
            else "stop"
        )
        return {
            "status": status,
            "confirmed": confirmed,
            "unresolved": unresolved,
            "source_failures": source_failures,
            "next_action": next_action,
        }

    @staticmethod
    def _state_summary(state: FootballEvidenceState) -> dict[str, Any]:
        payload: Any = {}
        try:
            payload = json.loads(state.content) if state.content else {}
        except (TypeError, json.JSONDecodeError):
            payload = {}
        fixture = payload.get("fixture") if isinstance(payload, dict) else None
        if not isinstance(fixture, dict):
            fixture = payload.get("requested") if isinstance(payload, dict) else None
        compact_fixture = {}
        if isinstance(fixture, dict):
            for field_name in (
                "competition",
                "season",
                "requested_date",
                "date",
                "home",
                "away",
                "home_team_id",
                "away_team_id",
            ):
                if fixture.get(field_name) not in (None, ""):
                    compact_fixture[field_name] = fixture[field_name]
        return {
            "fixture_key": state.fixture_key,
            "status": state.status,
            "attempt": state.attempt,
            "fixture": compact_fixture,
            "evidence_key": payload.get("base_evidence_key") if isinstance(payload, dict) else None,
            "requested_identity": dict(state.requested_identity),
            "confirmed_identity": dict(state.confirmed_identity),
            "source_statuses": list(state.source_statuses),
            "audit_count": len(state.audit),
        }

    def has_football_negative(self) -> bool:
        return bool(self.unresolved_football_fixtures()) or bool(
            self.football_negative_evidence
            and not self.football_evidence_states
        )

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


def _legacy_fixture_key(content: str) -> str:
    payload = _json_object(content)
    if not isinstance(payload, dict):
        return ""
    fixture = payload.get("fixture") or payload.get("requested")
    if not isinstance(fixture, dict):
        return ""
    # Import lazily to keep the low-level execution module independent from
    # the ``fastclaw.tools`` package during module initialization.
    from fastclaw.tools.football_competitions import canonical_competition_identity

    competition = canonical_competition_identity(str(fixture.get("competition") or ""))
    season = _normalize_identifier(str(fixture.get("season") or ""))
    raw_date = str(fixture.get("requested_date") or fixture.get("date") or "")
    date_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw_date)
    date = (
        f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-"
        f"{int(date_match.group(3)):02d}"
        if date_match is not None
        else _normalize_identifier(raw_date)
    )
    home = football_team_identity(str(fixture.get("home") or ""))
    away = football_team_identity(str(fixture.get("away") or ""))
    if not all((competition, season, date, home, away)):
        return ""
    return "|".join((competition, season, date, home, away))


def _json_object(content: str) -> dict[str, Any]:
    try:
        payload: Any = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _identity_from_payload(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    fields = {
        name: str(value[name])
        for name in (
            "competition",
            "season",
            "requested_date",
            "date",
            "home",
            "away",
            "home_team_id",
            "away_team_id",
        )
        if value.get(name) not in (None, "")
    }
    return fields

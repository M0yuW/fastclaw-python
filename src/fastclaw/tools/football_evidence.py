"""Provider-neutral source results and the constrained The Odds API client."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

import httpx

from fastclaw.tools.football_competitions import FootballCompetition

SourceState = Literal["success", "empty", "unavailable", "rejected", "no_match"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(slots=True)
class SourceResult:
    """Sanitized provider outcome safe to expose to an Agent."""

    source: str
    status: SourceState
    data: Any = None
    as_of: str = field(default_factory=utc_now)
    error_code: str | None = None
    safe_reason: str | None = None
    quota: dict[str, str | None] | None = None

    def report(self) -> dict[str, Any]:
        report: dict[str, Any] = {
            "source": self.source,
            "status": self.status,
            "as_of": self.as_of,
        }
        if self.error_code:
            report["error_code"] = self.error_code
        if self.safe_reason:
            report["safe_reason"] = self.safe_reason
        if self.quota is not None:
            report["quota"] = self.quota
        return report


class OddsSource(Protocol):
    async def fetch(
        self,
        competition: FootballCompetition,
        *,
        regions: tuple[str, ...],
        markets: tuple[str, ...],
        odds_format: str,
        commence_from: str,
        commence_to: str,
    ) -> SourceResult: ...


class TheOddsApiSource:
    """Call only fixed Odds API endpoints and reviewed sport keys."""

    _BASE = "https://api.the-odds-api.com"
    _MAX_BYTES = 2_000_000

    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key.strip()
        self._transport = transport

    @classmethod
    def from_environment(cls) -> TheOddsApiSource:
        return cls(os.environ.get("ODDS_API_KEY", ""))

    async def fetch(
        self,
        competition: FootballCompetition,
        *,
        regions: tuple[str, ...],
        markets: tuple[str, ...],
        odds_format: str,
        commence_from: str,
        commence_to: str,
    ) -> SourceResult:
        if not self._api_key:
            return SourceResult(
                "the_odds_api",
                "unavailable",
                error_code="odds_key_missing",
                safe_reason="The Odds API is not configured",
            )
        try:
            async with httpx.AsyncClient(
                base_url=self._BASE,
                transport=self._transport,
                timeout=httpx.Timeout(25.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                catalog = await client.get("/v4/sports", params={"apiKey": self._api_key})
                catalog_result = self._decode(catalog, source="the_odds_api:catalog")
                if catalog_result.status != "success":
                    return catalog_result
                catalog_rows = catalog_result.data
                assert isinstance(catalog_rows, list)
                available = {
                    str(row.get("key"))
                    for row in catalog_rows
                    if isinstance(row, dict) and row.get("active") is not False
                }
                active_keys = [key for key in competition.odds_sport_keys if key in available]
                # The provider occasionally omits a stable, directly callable
                # sport key from /sports (Europa League is one observed case).
                # The Runtime-owned allowlist remains the security boundary, so
                # probe only those reviewed keys before declaring the route
                # unavailable.  Prefer catalog-advertised alternates when they
                # exist (for example Champions League qualification).
                request_keys = active_keys or list(competition.odds_sport_keys)
                params = {
                    "apiKey": self._api_key,
                    "regions": ",".join(regions),
                    "markets": ",".join(markets),
                    "oddsFormat": odds_format,
                }
                if commence_from:
                    params["commenceTimeFrom"] = commence_from
                if commence_to:
                    params["commenceTimeTo"] = commence_to
                rows: list[dict[str, Any]] = []
                failures: list[SourceResult] = []
                completed_request = False
                latest_quota = self._quota(catalog.headers)
                for sport_key in request_keys:
                    response = await client.get(
                        f"/v4/sports/{sport_key}/odds", params=params
                    )
                    decoded = self._decode(response, source="the_odds_api")
                    latest_quota = decoded.quota
                    if decoded.status in {"success", "empty"}:
                        completed_request = True
                        if isinstance(decoded.data, list):
                            rows.extend(row for row in decoded.data if isinstance(row, dict))
                    else:
                        failures.append(decoded)
                if rows:
                    return SourceResult("the_odds_api", "success", rows, quota=latest_quota)
                if completed_request:
                    return SourceResult("the_odds_api", "empty", [], quota=latest_quota)
                if not active_keys:
                    return SourceResult(
                        "the_odds_api",
                        "rejected",
                        error_code="odds_sport_not_in_catalog",
                        safe_reason=(
                            "The reviewed competition was absent from the current sports "
                            "catalog and its stable route was not callable"
                        ),
                        quota=latest_quota,
                    )
                if failures:
                    return failures[0]
                return SourceResult("the_odds_api", "empty", [], quota=latest_quota)
        except (httpx.HTTPError, TimeoutError):
            return SourceResult(
                "the_odds_api",
                "unavailable",
                error_code="odds_request_failed",
                safe_reason="The Odds API request did not complete",
            )

    @classmethod
    def _decode(cls, response: httpx.Response, *, source: str) -> SourceResult:
        quota = cls._quota(response.headers)
        if response.status_code != 200:
            return SourceResult(
                source,
                "unavailable",
                error_code="odds_http_error",
                safe_reason="The Odds API returned an unsuccessful status",
                quota=quota,
            )
        if len(response.content) > cls._MAX_BYTES:
            return SourceResult(
                source,
                "rejected",
                error_code="odds_response_too_large",
                safe_reason="The Odds API response exceeded the safety limit",
                quota=quota,
            )
        try:
            payload = response.json()
        except ValueError:
            return SourceResult(
                source,
                "rejected",
                error_code="odds_malformed_json",
                safe_reason="The Odds API returned malformed JSON",
                quota=quota,
            )
        if not isinstance(payload, list):
            return SourceResult(
                source,
                "rejected",
                error_code="odds_unexpected_payload",
                safe_reason="The Odds API response structure was not recognized",
                quota=quota,
            )
        return SourceResult(source, "success" if payload else "empty", payload, quota=quota)

    @staticmethod
    def _quota(headers: httpx.Headers) -> dict[str, str | None]:
        return {
            "requests_remaining": headers.get("x-requests-remaining"),
            "requests_used": headers.get("x-requests-used"),
            "requests_last": headers.get("x-requests-last"),
        }

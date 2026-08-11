#!/usr/bin/env python3
"""Constrained The Odds API client for reviewed football competitions."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.football_competitions import COMPETITIONS, resolve_competition
from common.utils import output_json

BASE = "https://api.the-odds-api.com/v4"
MAX_BYTES = 2_000_000
ALLOWED_REGIONS = frozenset({"us", "uk", "eu", "au"})
ALLOWED_MARKETS = frozenset({"h2h", "totals"})
ALLOWED_FORMATS = frozenset({"decimal", "american"})


def _status(
    state: str,
    *,
    error_code: str | None = None,
    safe_reason: str | None = None,
    data: object = None,
    quota: dict[str, str | None] | None = None,
) -> dict:
    result = {
        "source": "the_odds_api",
        "status": state,
        "as_of": datetime.now(timezone.utc).isoformat(),
    }
    if error_code:
        result["error_code"] = error_code
    if safe_reason:
        result["safe_reason"] = safe_reason
    if data is not None:
        result["data"] = data
    if quota is not None:
        result["quota"] = quota
    return result


def _request(path: str, params: dict[str, str], api_key: str) -> tuple[object, dict]:
    query = urllib.parse.urlencode({**params, "apiKey": api_key})
    request = urllib.request.Request(
        f"{BASE}/{path.lstrip('/')}?{query}",
        headers={"Accept": "application/json", "User-Agent": "fastclaw-football/1.0"},
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("response_too_large")
        quota = {
            "requests_remaining": response.headers.get("x-requests-remaining"),
            "requests_used": response.headers.get("x-requests-used"),
            "requests_last": response.headers.get("x-requests-last"),
        }
        return json.loads(body.decode("utf-8")), quota


def fetch_odds(
    competition_name: str,
    *,
    regions: tuple[str, ...] = ("eu",),
    markets: tuple[str, ...] = ("h2h", "totals"),
    odds_format: str = "decimal",
    commence_from: str = "",
    commence_to: str = "",
) -> dict:
    mapping = resolve_competition(competition_name)
    if mapping is None:
        return _status(
            "rejected",
            error_code="competition_not_reviewed",
            safe_reason="Competition is not in the reviewed provider catalog",
        )
    if not set(regions).issubset(ALLOWED_REGIONS) or not regions:
        return _status("rejected", error_code="regions_rejected", safe_reason="Unsupported region")
    if not set(markets).issubset(ALLOWED_MARKETS) or not markets:
        return _status("rejected", error_code="markets_rejected", safe_reason="Unsupported market")
    if odds_format not in ALLOWED_FORMATS:
        return _status(
            "rejected", error_code="odds_format_rejected", safe_reason="Unsupported odds format"
        )
    for value in (commence_from, commence_to):
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return _status(
                    "rejected",
                    error_code="time_window_rejected",
                    safe_reason="Commencement window must use ISO-8601 timestamps",
                )
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        return _status(
            "unavailable",
            error_code="odds_key_missing",
            safe_reason="The Odds API is not configured; query current Sporttery fixtures",
        )
    try:
        catalog, catalog_quota = _request("sports", {}, api_key)
        if not isinstance(catalog, list):
            return _status(
                "rejected",
                error_code="catalog_unexpected_payload",
                safe_reason="The sports catalog response was not recognized",
                quota=catalog_quota,
            )
        available = {
            str(item.get("key"))
            for item in catalog
            if isinstance(item, dict) and item.get("active") is not False
        }
        sport_key = mapping["odds_sport_key"]
        if sport_key not in available:
            return _status(
                "rejected",
                error_code="odds_sport_not_in_catalog",
                safe_reason="The reviewed competition is not in the current sports catalog",
                quota=catalog_quota,
            )
        params = {
            "regions": ",".join(regions),
            "markets": ",".join(markets),
            "oddsFormat": odds_format,
        }
        if commence_from:
            params["commenceTimeFrom"] = commence_from
        if commence_to:
            params["commenceTimeTo"] = commence_to
        games, quota = _request(f"sports/{sport_key}/odds", params, api_key)
        if not isinstance(games, list):
            return _status(
                "rejected",
                error_code="odds_unexpected_payload",
                safe_reason="The odds response was not recognized",
                quota=quota,
            )
        return _status("success" if games else "empty", data=games, quota=quota)
    except (urllib.error.URLError, TimeoutError):
        return _status(
            "unavailable",
            error_code="odds_request_failed",
            safe_reason="The Odds API request did not complete; query current Sporttery fixtures",
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _status(
            "rejected",
            error_code="odds_malformed_json",
            safe_reason="The Odds API returned malformed JSON",
        )
    except ValueError:
        return _status(
            "rejected",
            error_code="odds_response_rejected",
            safe_reason="The Odds API response exceeded a safety constraint",
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Reviewed football odds source")
    parser.add_argument("--competition")
    parser.add_argument("--regions", default="eu")
    parser.add_argument("--markets", default="h2h,totals")
    parser.add_argument("--odds-format", default="decimal", choices=sorted(ALLOWED_FORMATS))
    parser.add_argument("--commence-from", default="")
    parser.add_argument("--commence-to", default="")
    parser.add_argument("--list-competitions", action="store_true")
    args = parser.parse_args()
    if args.list_competitions:
        output_json(
            {
                "competitions": [
                    {"competition": name, "country": country}
                    for name, country, _espn, _odds, _aliases in COMPETITIONS
                ]
            }
        )
        return
    if not args.competition:
        output_json(
            _status(
                "rejected",
                error_code="competition_required",
                safe_reason="--competition is required",
            )
        )
        return
    output_json(
        fetch_odds(
            args.competition,
            regions=tuple(filter(None, args.regions.split(","))),
            markets=tuple(filter(None, args.markets.split(","))),
            odds_format=args.odds_format,
            commence_from=args.commence_from,
            commence_to=args.commence_to,
        )
    )


if __name__ == "__main__":
    main()

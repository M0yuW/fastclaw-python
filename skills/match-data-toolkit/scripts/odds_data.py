#!/usr/bin/env python3
"""
World Cup Odds Fetcher (The Odds API)
=====================================
Fetch FIFA World Cup match odds (1X2 / over-under) from The Odds API.
Used as the *second* layer behind web_search — call this only when web
search can't supply odds or when precise implied probabilities are needed.
Free tier is quota-limited (500 req), so every response echoes the
remaining quota and the script never loops over many requests.

Key is read from the ODDS_API_KEY environment variable (never hardcoded,
so this script is safe to commit).

Usage:
    ODDS_API_KEY=xxx python3 odds_data.py --list
    ODDS_API_KEY=xxx python3 odds_data.py --match "France" "Sweden"

Output is JSON to stdout; errors to stderr. On failure/empty it returns
{"error": ..., "note": "fall back to web_search"} so the agent degrades.
"""
from __future__ import annotations

import argparse
import os
import sys
import json
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.utils import output_json, error_exit

SPORT = "soccer_fifa_world_cup"
BASE = "https://api.the-odds-api.com/v4"
REGIONS = "eu"          # eu/uk give 1X2 broadly; au lacks spreads
MARKETS = "h2h,totals"  # 1X2 + over/under; spreads (handicap) intentionally omitted
BJ = timezone(timedelta(hours=8))


def _key() -> str:
    k = os.environ.get("ODDS_API_KEY", "").strip()
    if not k:
        error_exit("ODDS_API_KEY env var not set")
    return k


def _get(path: str, params: dict):
    """GET an Odds API endpoint; return (parsed_json, remaining_quota)."""
    params = dict(params, apiKey=_key())
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "wc-odds/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        remaining = r.headers.get("x-requests-remaining")
        return json.loads(r.read().decode("utf-8")), remaining


def _bj(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BJ)
        return dt.strftime("%Y-%m-%d %H:%M (北京时间)")
    except Exception:
        return iso


def _implied(price):
    """Decimal odds -> implied probability (raw, not vig-adjusted)."""
    try:
        return round(1.0 / float(price), 4)
    except Exception:
        return None


def _consensus(games: list, want_home: str | None = None,
               want_away: str | None = None) -> list:
    """Reduce raw bookmaker data to a per-match consensus (averaged 1X2 +
    implied probabilities + a sample over/under). Optionally filter to one
    match by team-name substring."""
    out = []
    for g in games:
        home, away = g.get("home_team", ""), g.get("away_team", "")
        if want_home and want_home.lower() not in (home + away).lower():
            continue
        if want_away and want_away.lower() not in (home + away).lower():
            continue
        h2h_prices = {"home": [], "away": [], "draw": []}
        ou_sample = None
        for b in g.get("bookmakers") or []:
            for m in b.get("markets") or []:
                if m.get("key") == "h2h":
                    for o in m.get("outcomes") or []:
                        n = o.get("name")
                        if n == home:
                            h2h_prices["home"].append(o.get("price"))
                        elif n == away:
                            h2h_prices["away"].append(o.get("price"))
                        elif n == "Draw":
                            h2h_prices["draw"].append(o.get("price"))
                elif m.get("key") == "totals" and ou_sample is None:
                    ou_sample = {o.get("name"): {"point": o.get("point"),
                                                 "price": o.get("price")}
                                 for o in (m.get("outcomes") or [])}

        def avg(xs):
            xs = [float(x) for x in xs if x]
            return round(sum(xs) / len(xs), 3) if xs else None

        ah, aa, ad = avg(h2h_prices["home"]), avg(h2h_prices["away"]), avg(h2h_prices["draw"])
        out.append({
            "match": f"{home} vs {away}",
            "home": home, "away": away,
            "kickoff_bj": _bj(g.get("commence_time", "")),
            "bookmaker_count": len(g.get("bookmakers") or []),
            "avg_odds": {"home": ah, "draw": ad, "away": aa},
            "implied_prob": {"home": _implied(ah), "draw": _implied(ad),
                             "away": _implied(aa)},
            "over_under_sample": ou_sample,
        })
    return out


def fetch_list() -> dict:
    """List upcoming World Cup matches with quota info (cheap recon)."""
    try:
        games, remaining = _get(f"sports/{SPORT}/odds/",
                                {"regions": REGIONS, "markets": "h2h",
                                 "oddsFormat": "decimal"})
    except Exception as e:
        return {"matches": [], "error": str(e),
                "note": "odds list failed; fall back to web_search"}
    if isinstance(games, dict):
        return {"matches": [], "error": games.get("message"),
                "note": "fall back to web_search"}
    rows = [{"match": f'{g.get("home_team")} vs {g.get("away_team")}',
             "kickoff_bj": _bj(g.get("commence_time", ""))} for g in games]
    return {"requests_remaining": remaining, "count": len(rows), "matches": rows}


def fetch_match(team_a: str, team_b: str) -> dict:
    """Odds + implied probabilities for a single match."""
    try:
        games, remaining = _get(f"sports/{SPORT}/odds/",
                                {"regions": REGIONS, "markets": MARKETS,
                                 "oddsFormat": "decimal"})
    except Exception as e:
        return {"match": f"{team_a} vs {team_b}", "error": str(e),
                "note": "odds fetch failed; fall back to web_search"}
    if isinstance(games, dict):
        return {"error": games.get("message"), "note": "fall back to web_search"}
    rows = _consensus(games, want_home=team_a, want_away=team_b)
    if not rows:
        return {"requests_remaining": remaining,
                "match": f"{team_a} vs {team_b}", "odds": [],
                "note": "no matching fixture in odds feed; fall back to web_search"}
    return {"requests_remaining": remaining, "as_of": _bj(
        datetime.now(timezone.utc).isoformat()), "odds": rows}


def main():
    p = argparse.ArgumentParser(
        description="World Cup Odds Fetcher (The Odds API; web_search first)")
    p.add_argument("teams", nargs="*", help="two team names for --match")
    p.add_argument("--list", action="store_true",
                   help="list fixtures + remaining quota (1 request)")
    p.add_argument("--match", action="store_true",
                   help="odds + implied prob for one match (1 request)")
    args = p.parse_args()

    try:
        if args.list:
            data = fetch_list()
        elif args.match:
            if len(args.teams) < 2:
                error_exit("--match requires two team names")
            data = fetch_match(args.teams[0], args.teams[1])
        else:
            error_exit("Specify --list or --match A B")
            return
        output_json(data)
    except Exception as e:
        error_exit(f"Error fetching odds: {e}")


if __name__ == "__main__":
    main()

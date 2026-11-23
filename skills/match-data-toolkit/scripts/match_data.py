#!/usr/bin/env python3
"""
World Cup Match Data Fetcher (TheSportsDB, free test key)
=========================================================
Fetch FIFA World Cup schedule, results, team form, head-to-head and
group standings from TheSportsDB. Free test key "123" (no signup).

Usage:
    python3 match_data.py --schedule --date 2026-06-30   # Matches on a day
    python3 match_data.py --results --season 2026         # All results in a season
    python3 match_data.py --team Brazil --form            # Team's recent form
    python3 match_data.py --h2h Brazil Argentina          # Head-to-head history
    python3 match_data.py --standings --season 2026       # Group standings

All output is JSON to stdout; errors to stderr. Interface/network
failures degrade to {"error": ..., "note": ...} so the calling agent
can fall back to web_search. Times are converted to Beijing time (UTC+8).
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
import urllib.request
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.utils import output_json, error_exit

API_KEY = "123"  # free public test key
BASE = f"https://www.thesportsdb.com/api/v1/json/{API_KEY}"
WC_LEAGUE_ID = "4429"  # FIFA World Cup
BJ = timezone(timedelta(hours=8))


def _get(path: str, params: dict) -> dict:
    """GET a TheSportsDB endpoint, return parsed JSON or raise."""
    url = f"{BASE}/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "wc-toolkit/1.0"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def _to_beijing(date_str: str, time_str: str | None) -> str:
    """Combine event date + UTC time into a Beijing-time string."""
    if not date_str:
        return ""
    if not time_str or time_str in ("00:00:00", ""):
        return date_str
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc).astimezone(BJ)
        return dt.strftime("%Y-%m-%d %H:%M (北京时间)")
    except Exception:
        return f"{date_str} {time_str} UTC"


def _event_row(e: dict) -> dict:
    return {
        "match": e.get("strEvent", ""),
        "home": e.get("strHomeTeam", ""),
        "away": e.get("strAwayTeam", ""),
        "home_score": e.get("intHomeScore"),
        "away_score": e.get("intAwayScore"),
        "date": e.get("dateEvent", ""),
        "kickoff_bj": _to_beijing(e.get("dateEvent", ""), e.get("strTime")),
        "round": e.get("intRound", ""),
        "venue": e.get("strVenue", ""),
        "status": e.get("strStatus", ""),
    }


# ---------------------------------------------------------------------------
# Schedule for a given day
# ---------------------------------------------------------------------------

def fetch_schedule(date: str) -> dict:
    """World Cup matches on a given day (date = YYYY-MM-DD)."""
    try:
        d = _get("eventsday.php", {"d": date, "l": WC_LEAGUE_ID})
    except Exception as e:
        return {"date": date, "matches": [], "error": str(e),
                "note": "eventsday failed; fall back to web_search"}
    ev = d.get("events") or []
    return {"date": date, "count": len(ev),
            "matches": [_event_row(e) for e in ev]}


# ---------------------------------------------------------------------------
# Results / all events in a season
# ---------------------------------------------------------------------------

def fetch_results(season: str, limit: int = 80) -> dict:
    """All World Cup events in a season (e.g. 2022). Includes scores."""
    try:
        d = _get("eventsseason.php", {"id": WC_LEAGUE_ID, "s": season})
    except Exception as e:
        return {"season": season, "matches": [], "error": str(e),
                "note": "eventsseason failed; fall back to web_search"}
    ev = d.get("events") or []
    rows = [_event_row(e) for e in ev[:limit]]
    played = [r for r in rows if r["home_score"] is not None]
    return {"season": season, "total": len(ev),
            "played": len(played), "matches": rows}


# ---------------------------------------------------------------------------
# Team helpers + form + head-to-head
# ---------------------------------------------------------------------------

def _team_id(name: str) -> str | None:
    try:
        d = _get("searchteams.php", {"t": name})
    except Exception:
        return None
    teams = d.get("teams") or []
    return teams[0].get("idTeam") if teams else None


def fetch_form(team: str, limit: int = 6) -> dict:
    """Team's most recent matches (any competition) as a form proxy."""
    tid = _team_id(team)
    if not tid:
        return {"team": team, "form": [], "error": "team not found",
                "note": "searchteams empty; fall back to web_search"}
    try:
        d = _get("eventslast.php", {"id": tid})
    except Exception as e:
        return {"team": team, "form": [], "error": str(e),
                "note": "eventslast failed; fall back to web_search"}
    ev = d.get("results") or []
    return {"team": team, "team_id": tid, "count": min(len(ev), limit),
            "form": [_event_row(e) for e in ev[:limit]]}


def fetch_h2h(team_a: str, team_b: str, limit: int = 10) -> dict:
    """Head-to-head: recent matches of team_a that involve team_b."""
    tid = _team_id(team_a)
    if not tid:
        return {"h2h": [], "error": f"team not found: {team_a}",
                "note": "fall back to web_search"}
    try:
        d = _get("eventslast.php", {"id": tid})
    except Exception as e:
        return {"h2h": [], "error": str(e), "note": "fall back to web_search"}
    ev = d.get("results") or []
    b = team_b.lower()
    hits = [_event_row(e) for e in ev
            if b in (e.get("strEvent", "").lower())]
    return {"team_a": team_a, "team_b": team_b,
            "count": len(hits[:limit]), "h2h": hits[:limit],
            "note": "free key only exposes recent events; deep H2H via web_search"}


# ---------------------------------------------------------------------------
# Group standings
# ---------------------------------------------------------------------------

def fetch_standings(season: str) -> dict:
    """Group-stage standings table for a season."""
    try:
        d = _get("lookuptable.php", {"l": WC_LEAGUE_ID, "s": season})
    except Exception as e:
        return {"season": season, "standings": [], "error": str(e),
                "note": "lookuptable failed; fall back to web_search"}
    tbl = d.get("table") or []
    rows = [{
        "rank": t.get("intRank"), "team": t.get("strTeam"),
        "played": t.get("intPlayed"), "win": t.get("intWin"),
        "draw": t.get("intDraw"), "loss": t.get("intLoss"),
        "gd": t.get("intGoalDifference"), "points": t.get("intPoints"),
    } for t in tbl]
    return {"season": season, "count": len(rows), "standings": rows,
            "note": "" if rows else "no table yet; fall back to web_search"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="World Cup Match Data Fetcher (TheSportsDB, no signup)")
    p.add_argument("teams", nargs="*", help="team name(s) for --form/--h2h")
    p.add_argument("--schedule", action="store_true", help="matches on --date")
    p.add_argument("--results", action="store_true", help="results in --season")
    p.add_argument("--form", action="store_true", help="team recent form")
    p.add_argument("--h2h", action="store_true", help="head-to-head (2 teams)")
    p.add_argument("--standings", action="store_true", help="group standings")
    p.add_argument("--team", default=None, help="team name for --form")
    p.add_argument("--date", default=None, help="YYYY-MM-DD (default today)")
    p.add_argument("--season", default=None, help="e.g. 2022")
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args()

    def lim(n):
        return args.limit if args.limit else n

    try:
        if args.schedule:
            date = args.date or datetime.now(BJ).strftime("%Y-%m-%d")
            data = fetch_schedule(date)
        elif args.results:
            if not args.season:
                error_exit("--results requires --season")
            data = fetch_results(args.season, limit=lim(80))
        elif args.form:
            team = args.team or (args.teams[0] if args.teams else None)
            if not team:
                error_exit("--form requires --team or a team argument")
            data = fetch_form(team, limit=lim(6))
        elif args.h2h:
            if len(args.teams) < 2:
                error_exit("--h2h requires two team arguments")
            data = fetch_h2h(args.teams[0], args.teams[1], limit=lim(10))
        elif args.standings:
            if not args.season:
                error_exit("--standings requires --season")
            data = fetch_standings(args.season)
        else:
            error_exit("Specify one of: --schedule/--results/--form/--h2h/--standings")
            return
        output_json(data)
    except Exception as e:
        error_exit(f"Error fetching data: {e}")


if __name__ == "__main__":
    main()

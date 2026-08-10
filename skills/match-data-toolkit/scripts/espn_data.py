#!/usr/bin/env python3
"""
ESPN Football Data Fetcher
==========================
Fetch a reviewed football competition's schedule, event summary, odds, team form, and H2H from
ESPN's public site API. Prefer this over OCR when ESPN JSON endpoints are
available.

Usage:
    python3 espn_data.py --competition "FIFA World Cup" --schedule --date 2026-07-02
    python3 espn_data.py --competition "瑞典超" --summary --event 401842783
    python3 espn_data.py --competition "瑞典超" --match "Sirius" "Brommapojkarna" --date 2026-08-10
    python3 espn_data.py --competition "FIFA World Cup" --form Portugal --event 760496

All output is JSON to stdout. Network/API failures degrade to
{"error": ..., "note": "fall back to OCR/web_search"}.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.utils import output_json

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
BJ = timezone(timedelta(hours=8))

# Keep this allowlist synchronized with fastclaw.tools.football_competitions. Unknown
# provider identifiers fail closed instead of being interpolated into a request URL.
COMPETITIONS = (
    ("FIFA World Cup", "Worldwide", "fifa.world", ("World Cup", "世界杯", "FIFA世界杯")),
    ("UEFA Champions League", "Europe", "uefa.champions", ("Champions League", "UCL", "欧冠")),
    ("UEFA Europa League", "Europe", "uefa.europa", ("Europa League", "UEL", "欧联", "欧联杯")),
    ("English Premier League", "England", "eng.1", ("Premier League", "EPL", "英超")),
    ("Spanish LALIGA", "Spain", "esp.1", ("La Liga", "西甲")),
    ("German Bundesliga", "Germany", "ger.1", ("Bundesliga", "德甲")),
    ("Italian Serie A", "Italy", "ita.1", ("Serie A", "意甲")),
    ("French Ligue 1", "France", "fra.1", ("Ligue 1", "法甲")),
    ("Dutch Eredivisie", "Netherlands", "ned.1", ("Eredivisie", "荷甲")),
    ("Portuguese Primeira Liga", "Portugal", "por.1", ("Primeira Liga", "葡超")),
    ("Swedish Allsvenskan", "Sweden", "swe.1", ("Allsvenskan", "瑞典超", "瑞典超级联赛")),
    ("Norwegian Eliteserien", "Norway", "nor.1", ("Eliteserien", "挪超", "挪威超级联赛")),
    ("Major League Soccer", "United States", "usa.1", ("MLS", "美职联")),
)


def _normalize(value: str) -> str:
    return re.sub(r"[^\w]+", "", unicodedata.normalize("NFKC", value).casefold())


def resolve_competition(value: str) -> dict | None:
    needle = _normalize(value)
    matches = []
    for name, country, slug, aliases in COMPETITIONS:
        labels = (name, slug, *aliases)
        if needle in {_normalize(label) for label in labels}:
            matches.append({"competition": name, "country": country, "espn_slug": slug})
    return matches[0] if len(matches) == 1 else None


def _get(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "Referer": "https://www.espn.com/soccer/",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/127.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code != 403:
            raise
    return _get_with_system_curl(url)


def _get_with_system_curl(url: str) -> dict:
    if not url.startswith(f"{BASE}/"):
        raise ValueError("ESPN URL is outside the fixed soccer API origin")
    executable = next(
        (path for path in (Path("/usr/bin/curl"), Path("/bin/curl")) if path.is_file()),
        None,
    )
    if executable is None:
        raise RuntimeError("ESPN rejected Python HTTP and trusted system curl is unavailable")
    result = subprocess.run(
        [
            str(executable),
            "--fail",
            "--silent",
            "--show-error",
            "--max-time",
            "25",
            "--max-filesize",
            "2000000",
            "--proto",
            "=https",
            "--max-redirs",
            "0",
            url,
        ],
        check=False,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin"},
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("ESPN request failed")
    if len(result.stdout) > 2_000_000:
        raise RuntimeError("ESPN response exceeded the safety limit")
    return json.loads(result.stdout.decode("utf-8"))


def _bj(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(BJ)
        return dt.strftime("%Y-%m-%d %H:%M (北京时间)")
    except Exception:
        return iso or ""


def _date_key(date: str) -> str:
    return date.replace("-", "")


def _moneyline_to_decimal(odds):
    if odds is None:
        return None
    try:
        odds = float(odds)
        if odds > 0:
            return round(1 + odds / 100, 3)
        return round(1 + 100 / abs(odds), 3)
    except Exception:
        return None


def _implied_from_decimal(price):
    try:
        return round(1.0 / float(price), 4) if price else None
    except Exception:
        return None


def _team_name(comp: dict) -> str:
    return ((comp.get("team") or {}).get("displayName") or "").strip()


def _event_row(e: dict) -> dict:
    comp = (e.get("competitions") or [{}])[0]
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    return {
        "id": e.get("id"),
        "match": f"{_team_name(home)} vs {_team_name(away)}".strip(),
        "espn_name": e.get("name"),
        "short_name": e.get("shortName"),
        "home": _team_name(home),
        "away": _team_name(away),
        "home_score": home.get("score"),
        "away_score": away.get("score"),
        "date_utc": e.get("date"),
        "kickoff_bj": _bj(e.get("date", "")),
        "venue": (comp.get("venue") or {}).get("fullName"),
        "status": ((e.get("status") or {}).get("type") or {}).get("description"),
    }


def _league_slug(data: dict) -> str:
    header = data.get("header") or {}
    league = header.get("league") or ((data.get("leagues") or [{}])[0])
    return str(league.get("slug") or "") if isinstance(league, dict) else ""


def _validate_league(data: dict, expected: str) -> dict | None:
    actual = _league_slug(data)
    if actual != expected:
        return {
            "error": "ESPN response competition does not match the trusted mapping",
            "expected_espn_slug": expected,
            "actual_espn_slug": actual,
        }
    return None


def fetch_schedule(date: str, mapping: dict) -> dict:
    slug = mapping["espn_slug"]
    try:
        data = _get(f"{BASE}/{slug}/scoreboard?dates={_date_key(date)}")
    except Exception as e:
        return {"date": date, "matches": [], "error": str(e),
                "note": "ESPN scoreboard failed; fall back to OCR/web_search"}
    if mismatch := _validate_league(data, slug):
        return mismatch
    events = data.get("events") or []
    league = (data.get("leagues") or [{}])[0]
    return {
        "source": "espn:scoreboard",
        "mapping": mapping,
        "date": date,
        "league": league.get("name"),
        "season": (league.get("season") or {}).get("displayName"),
        "count": len(events),
        "matches": [_event_row(e) for e in events],
    }


def find_event(team_a: str, team_b: str, mapping: dict, date: str | None = None) -> dict:
    dates = [date] if date else []
    if date:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d")
            dates.extend([(dt + timedelta(days=delta)).strftime("%Y-%m-%d")
                          for delta in (-1, 1)])
        except Exception:
            pass
    if not dates:
        today = datetime.now(timezone.utc).astimezone(BJ)
        dates = [(today + timedelta(days=delta)).strftime("%Y-%m-%d")
                 for delta in range(-2, 4)]

    wanted = {team_a.lower(), team_b.lower()}
    checked = []
    for d in dict.fromkeys(dates):
        sched = fetch_schedule(d, mapping)
        checked.append({"date": d, "count": sched.get("count", 0)})
        for match in sched.get("matches") or []:
            hay = f"{match.get('home', '')} {match.get('away', '')}".lower()
            if all(w in hay for w in wanted):
                return {"found": True, "event": match, "checked": checked}
    return {"found": False, "checked": checked,
            "note": "no ESPN event matched those teams in checked dates"}


def _event_teams(summary: dict) -> tuple[str, str]:
    comp = (((summary.get("header") or {}).get("competitions") or [{}])[0])
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    return _team_name(home), _team_name(away)


def _odds(summary: dict) -> list[dict]:
    rows = []
    home, away = _event_teams(summary)
    for item in summary.get("odds") or []:
        ml = item.get("moneyline") or {}
        home_dec = _moneyline_to_decimal(((ml.get("home") or {}).get("close") or {}).get("odds"))
        away_dec = _moneyline_to_decimal(((ml.get("away") or {}).get("close") or {}).get("odds"))
        draw_dec = _moneyline_to_decimal(((ml.get("draw") or {}).get("close") or {}).get("odds"))
        rows.append({
            "provider": (item.get("provider") or {}).get("name"),
            "details": item.get("details"),
            "over_under": item.get("overUnder"),
            "american_moneyline": {
                "home": ((ml.get("home") or {}).get("close") or {}).get("odds"),
                "draw": ((ml.get("draw") or {}).get("close") or {}).get("odds"),
                "away": ((ml.get("away") or {}).get("close") or {}).get("odds"),
            },
            "decimal_odds": {"home": home_dec, "draw": draw_dec, "away": away_dec},
            "implied_prob": {
                "home": _implied_from_decimal(home_dec),
                "draw": _implied_from_decimal(draw_dec),
                "away": _implied_from_decimal(away_dec),
            },
            "teams": {"home": home, "away": away},
        })
    return rows


def _form(summary: dict) -> dict:
    form = {}
    for team in ((summary.get("boxscore") or {}).get("form") or []):
        name = ((team.get("team") or {}).get("displayName") or "").strip()
        events = []
        for e in team.get("events") or []:
            events.append({
                "id": e.get("id"),
                "date_utc": e.get("gameDate"),
                "kickoff_bj": _bj(e.get("gameDate", "")),
                "score": e.get("score"),
                "result": e.get("gameResult"),
                "round": e.get("roundName"),
                "opponent": ((e.get("opponent") or {}).get("displayName")),
                "competition": e.get("competitionName"),
            })
        if name:
            form[name] = events
    return form


def _card_events(summary: dict, team_name: str | None = None) -> list[dict]:
    rows = []
    wanted = (team_name or "").lower()
    for ev in summary.get("key_events") or []:
        typ = ((ev.get("type") or {}).get("type") or "").lower()
        text = " ".join(str(ev.get(k) or "") for k in ("text", "shortText")).lower()
        if "card" not in typ and "card" not in text and "sent off" not in text:
            continue
        team = ((ev.get("team") or {}).get("displayName") or "").strip()
        if wanted and wanted not in team.lower():
            continue
        participants = []
        for p in ev.get("participants") or []:
            athlete = p.get("athlete") or {}
            if athlete.get("displayName"):
                participants.append(athlete.get("displayName"))
        rows.append({
            "event_id": summary.get("event"),
            "match": summary.get("match"),
            "team": team,
            "card_type": typ,
            "minute": ((ev.get("clock") or {}).get("displayValue") or ""),
            "players": participants,
            "text": ev.get("text") or ev.get("shortText"),
        })
    return rows


def fetch_discipline(team: str, event_id: str, mapping: dict) -> dict:
    current = fetch_summary(event_id, mapping)
    if current.get("error"):
        return current
    forms = current.get("form") or {}
    matched = {name: rows for name, rows in forms.items() if team.lower() in name.lower()}
    events = []
    checked = []
    for rows in matched.values():
        for row in rows:
            if not row.get("id"):
                continue
            if str(row.get("id")) == str(event_id):
                continue
            prev = fetch_summary(str(row["id"]), mapping)
            checked.append({"event": row.get("id"), "match": prev.get("match"), "status": prev.get("status")})
            if not prev.get("error"):
                events.extend(_card_events(prev, team))

    by_player = {}
    for ev in events:
        players = ev.get("players") or ["unknown"]
        for player in players:
            rec = by_player.setdefault(player, {"yellow": 0, "red": 0, "events": []})
            card_type = ev.get("card_type") or ""
            text = (ev.get("text") or "").lower()
            if "red-card" in card_type or "red card" in text or "sent off" in text:
                rec["red"] += 1
            elif "yellow" in card_type or "yellow card" in text:
                rec["yellow"] += 1
            rec["events"].append(ev)

    return {
        "source": "espn:summary.key_events",
        "mapping": mapping,
        "team": team,
        "current_event": event_id,
        "checked": checked,
        "card_events": events,
        "by_player": by_player,
        "availability_note": "ESPN keyEvents can show match cards but does not confirm competition suspension rules or official unavailable list; verify confirmed suspensions with team/FIFA/news sources.",
    }


def fetch_summary(event_id: str, mapping: dict) -> dict:
    slug = mapping["espn_slug"]
    try:
        data = _get(
            f"{BASE}/{slug}/summary?event={urllib.parse.quote(str(event_id))}"
        )
    except Exception as e:
        return {"event": event_id, "error": str(e),
                "note": "ESPN summary failed; fall back to OCR/web_search"}

    if mismatch := _validate_league(data, slug):
        return mismatch
    comp = (((data.get("header") or {}).get("competitions") or [{}])[0])
    competitors = comp.get("competitors") or []
    home = next((c for c in competitors if c.get("homeAway") == "home"), {})
    away = next((c for c in competitors if c.get("homeAway") == "away"), {})
    return {
        "source": "espn:summary",
        "mapping": mapping,
        "event": event_id,
        "match": f"{_team_name(home)} vs {_team_name(away)}".strip(),
        "date_utc": comp.get("date") or ((data.get("header") or {}).get("competitions") or [{}])[0].get("date"),
        "kickoff_bj": _bj(comp.get("date") or ""),
        "status": ((comp.get("status") or {}).get("type") or {}).get("description"),
        "venue": ((data.get("gameInfo") or {}).get("venue") or {}).get("fullName"),
        "attendance": (data.get("gameInfo") or {}).get("attendance"),
        "score": {"home": home.get("score"), "away": away.get("score")},
        "teams": {"home": _team_name(home), "away": _team_name(away)},
        "form": _form(data),
        "odds": _odds(data),
        "head_to_head_games": data.get("headToHeadGames"),
        "standings": data.get("standings"),
        "leaders": data.get("leaders"),
        "key_events": data.get("keyEvents"),
    }


def main():
    ap = argparse.ArgumentParser(description="Competition-aware ESPN football data fetcher")
    ap.add_argument(
        "--competition",
        help="reviewed competition name or alias; use --list-competitions",
    )
    ap.add_argument(
        "--list-competitions", action="store_true", help="list trusted ESPN mappings"
    )
    ap.add_argument("--schedule", action="store_true", help="Fetch ESPN scoreboard for a date")
    ap.add_argument("--summary", action="store_true", help="Fetch ESPN event summary")
    ap.add_argument("--match", nargs=2, metavar=("TEAM_A", "TEAM_B"), help="Find a match and return summary")
    ap.add_argument("--form", metavar="TEAM", help="Return a team's ESPN form from an event summary")
    ap.add_argument("--discipline", metavar="TEAM", help="Return team yellow/red-card events from prior World Cup matches in ESPN form")
    ap.add_argument("--date", help="YYYY-MM-DD for --schedule or --match search")
    ap.add_argument("--event", help="ESPN event id for --summary/--form/--discipline")
    args = ap.parse_args()

    if args.list_competitions:
        output_json(
            {
                "competitions": [
                    {"competition": name, "country": country, "espn_slug": slug}
                    for name, country, slug, _aliases in COMPETITIONS
                ]
            }
        )
        return
    mapping = resolve_competition(args.competition or "")
    if mapping is None:
        output_json(
            {
                "error": "--competition must identify a trusted competition",
                "note": "use --list-competitions",
            }
        )
        return

    if args.schedule:
        if not args.date:
            output_json({"error": "--schedule requires --date"})
            return
        output_json(fetch_schedule(args.date, mapping))
        return

    if args.match:
        found = find_event(args.match[0], args.match[1], mapping, args.date)
        if not found.get("found"):
            output_json(found)
            return
        summary = fetch_summary(found["event"]["id"], mapping)
        summary["matched_event"] = found["event"]
        summary["checked"] = found.get("checked")
        output_json(summary)
        return

    if args.summary:
        if not args.event:
            output_json({"error": "--summary requires --event"})
            return
        output_json(fetch_summary(args.event, mapping))
        return

    if args.form:
        if not args.event:
            output_json({"error": "--form requires --event"})
            return
        summary = fetch_summary(args.event, mapping)
        forms = summary.get("form") or {}
        needle = args.form.lower()
        match = {name: rows for name, rows in forms.items() if needle in name.lower()}
        output_json({"source": "espn:summary.form", "event": args.event,
                     "team": args.form, "form": match})
        return

    if args.discipline:
        if not args.event:
            output_json({"error": "--discipline requires --event"})
            return
        output_json(fetch_discipline(args.discipline, args.event, mapping))
        return

    ap.print_help()


if __name__ == "__main__":
    main()

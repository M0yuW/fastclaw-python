#!/usr/bin/env python3
"""
Sporttery football odds fetcher and EV helper.

Fetches China Sports Lottery football calculator data from the public mobile
calculator API. The endpoint currently works without poolCode; adding poolCode
can trigger WAF blocks, so this script fetches the full calculator payload and
filters locally.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.http_fetch import fetch_json

API_URL = "https://webapi.sporttery.cn/gateway/uniform/football/getMatchCalculatorV1.qry?channel=c"
REFERER = "https://m.sporttery.cn/mjc/jsq/zqspf/"

# Total budget for the one call this script makes, retries included.
REQUEST_TIMEOUT = 20.0

ALIASES = {
    "colombia": "哥伦比亚",
    "ghana": "加纳",
    "canada": "加拿大",
    "morocco": "摩洛哥",
    "paraguay": "巴拉圭",
    "france": "法国",
    "brazil": "巴西",
    "norway": "挪威",
    "mexico": "墨西哥",
    "england": "英格兰",
    "argentina": "阿根廷",
    "cape verde": "佛得角",
    "australia": "澳大利亚",
    "egypt": "埃及",
}


def output_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def fetch_payload() -> dict[str, Any]:
    return fetch_json(
        API_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) sporttery-ev/1.0",
            "Referer": REFERER,
            "Accept": "application/json,text/plain,*/*",
        },
        timeout=REQUEST_TIMEOUT,
    )


def norm(s: str) -> str:
    return (s or "").strip().lower()


def expand_team(s: str) -> list[str]:
    n = norm(s)
    out = [n]
    if n in ALIASES:
        out.append(ALIASES[n].lower())
    return out


def iter_matches(payload: dict[str, Any]):
    for day in ((payload.get("value") or {}).get("matchInfoList") or []):
        for m in day.get("subMatchList") or []:
            yield m


def pick_pool(pool: dict[str, Any] | None) -> dict[str, Any] | None:
    if not pool:
        return None
    vals = {k: pool.get(k) for k in ("h", "d", "a")}
    if not any(vals.values()):
        return None
    vals.update({
        "goalLine": pool.get("goalLine"),
        "goalLineValue": pool.get("goalLineValue"),
        "updateDate": pool.get("updateDate"),
        "updateTime": pool.get("updateTime"),
    })
    return vals


def compact_match(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "sporttery:getMatchCalculatorV1",
        "matchId": m.get("matchId"),
        "matchNum": m.get("matchNumStr"),
        "businessDate": m.get("businessDate"),
        "league": m.get("leagueAbbName") or m.get("leagueAllName"),
        "home": m.get("homeTeamAllName"),
        "away": m.get("awayTeamAllName"),
        "homeAbbr": m.get("homeTeamAbbName"),
        "awayAbbr": m.get("awayTeamAbbName"),
        "matchTime": f"{m.get('matchDate', '')} {m.get('matchTime', '')}".strip(),
        "had": pick_pool(m.get("had")),
        "hhad": pick_pool(m.get("hhad")),
    }


def list_worldcup(payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for m in iter_matches(payload):
        league = (m.get("leagueAbbName") or m.get("leagueAllName") or "")
        if "世界杯" in league:
            rows.append(compact_match(m))
    return rows


def find_match(payload: dict[str, Any], team_a: str, team_b: str) -> dict[str, Any] | None:
    needles_a = expand_team(team_a)
    needles_b = expand_team(team_b)
    for m in iter_matches(payload):
        home = " ".join(str(m.get(k) or "") for k in ("homeTeamAllName", "homeTeamAbbName", "homeTeamAbbEnName")).lower()
        away = " ".join(str(m.get(k) or "") for k in ("awayTeamAllName", "awayTeamAbbName", "awayTeamAbbEnName")).lower()
        hay = home + " " + away
        if any(x in hay for x in needles_a) and any(x in hay for x in needles_b):
            return compact_match(m)
    return None


def odds_to_probs(pool: dict[str, Any] | None) -> dict[str, Any] | None:
    if not pool:
        return None
    odds = {}
    for k in ("h", "d", "a"):
        try:
            odds[k] = float(pool[k])
        except Exception:
            return None
    raw = {k: 1.0 / v for k, v in odds.items()}
    total = sum(raw.values())
    return {
        "odds": odds,
        "raw_implied_prob": {k: round(v, 4) for k, v in raw.items()},
        "overround": round(total - 1.0, 4),
        "vig_free_prob": {k: round(v / total, 4) for k, v in raw.items()},
    }


def parse_model_prob(s: str) -> dict[str, float]:
    parts = [p.strip() for p in s.split(",")]
    if len(parts) != 3:
        raise ValueError("--model-prob must be home,draw,away")
    vals = [float(p) for p in parts]
    total = sum(vals)
    if total <= 0:
        raise ValueError("model probabilities must sum to positive value")
    if max(vals) > 1.0 or total > 1.5:
        vals = [v / 100.0 for v in vals]
    return {"h": vals[0], "d": vals[1], "a": vals[2]}


def ev_for_pool(pool: dict[str, Any] | None, model_prob: dict[str, float]) -> dict[str, Any] | None:
    probs = odds_to_probs(pool)
    if not probs:
        return None
    odds = probs["odds"]
    ev = {k: model_prob[k] * odds[k] - 1.0 for k in ("h", "d", "a")}
    best = max(ev, key=ev.get)
    return {
        **probs,
        "model_prob": {k: round(model_prob[k], 4) for k in ("h", "d", "a")},
        "ev": {k: round(v, 4) for k, v in ev.items()},
        "best_outcome": best,
        "best_ev": round(ev[best], 4),
        "value_flag": "positive" if ev[best] > 0.03 else ("borderline" if ev[best] > 0 else "no_clear_value"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Sporttery football odds and calculate EV")
    ap.add_argument("--list-worldcup", action="store_true", help="List available World Cup matches")
    ap.add_argument("--match", nargs=2, metavar=("TEAM_A", "TEAM_B"), help="Find one match by team names")
    ap.add_argument("--ev", nargs=2, metavar=("TEAM_A", "TEAM_B"), help="Find match and calculate EV")
    ap.add_argument("--model-prob", help="Model probabilities as home,draw,away; percentages or decimals")
    args = ap.parse_args()

    try:
        payload = fetch_payload()
    except Exception as e:
        output_json({"error": str(e), "note": "Sporttery API fetch failed"})
        return

    if payload.get("errorCode") not in (0, "0", None):
        output_json({"error": payload.get("errorMessage"), "raw": payload})
        return

    if args.list_worldcup:
        output_json({"source": API_URL, "matches": list_worldcup(payload)})
        return

    if args.match:
        row = find_match(payload, args.match[0], args.match[1])
        output_json(row or {"error": "match not found", "teams": args.match})
        return

    if args.ev:
        if not args.model_prob:
            output_json({"error": "--ev requires --model-prob home,draw,away"})
            return
        row = find_match(payload, args.ev[0], args.ev[1])
        if not row:
            output_json({"error": "match not found", "teams": args.ev})
            return
        try:
            model_prob = parse_model_prob(args.model_prob)
        except Exception as e:
            output_json({"error": str(e)})
            return
        row["had_ev"] = ev_for_pool(row.get("had"), model_prob)
        row["hhad_prob"] = odds_to_probs(row.get("hhad"))
        row["note"] = "EV is informational price calibration only; it is not betting advice."
        output_json(row)
        return

    ap.print_help()


if __name__ == "__main__":
    main()

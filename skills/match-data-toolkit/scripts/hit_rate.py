#!/usr/bin/env python3
"""
World Cup Prediction Hit-Rate Calculator
========================================
Read the prediction ledger and compute rolling hit rates for the
multi-agent system vs. baselines (coin flip, and any external model
recorded in the ledger such as Opta / official).

Ledger format (JSON, one object per settled or pending match) —
written by the coordinator. See ledger schema below. Pending rows
(actual_result == null) are ignored in hit-rate math but counted.

Usage:
    python3 hit_rate.py                       # uses default ledger path
    python3 hit_rate.py --ledger /path/ledger.json
    python3 hit_rate.py --by-date             # also break down per date

Output is JSON to stdout; errors to stderr.

Ledger row schema:
{
  "date": "2026-06-30",
  "match": "France vs Sweden",
  "our_pred": "France",        # our fused lean: home team | away team | "draw"
  "our_confidence": "high",    # high|mid|low (optional)
  "baselines": {               # optional external predictions, same value space
      "opta": "France",
      "official": "France"
  },
  "actual_result": "France"    # actual winner team name, or "draw"; null = pending
}
"""
from __future__ import annotations

import argparse
import os
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.utils import output_json, error_exit

DEFAULT_LEDGER = os.environ.get(
    "WC_LEDGER",
    str(Path.home() / ".fastclaw" / "workspaces"
        / "agt_0810c26790ce1c32bb11" / "worldcup" / "ledger.json"))


def _load(path: str) -> list:
    p = Path(path)
    if not p.exists():
        return []
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    # accept either a bare list or {"matches": [...]}
    if isinstance(data, dict):
        data = data.get("matches") or data.get("ledger") or []
    return data if isinstance(data, list) else []


def _hit(pred, actual) -> bool:
    if pred is None or actual is None:
        return False
    return str(pred).strip().lower() == str(actual).strip().lower()


def _coin_baseline(n_settled: int) -> float:
    # 3-way outcome (home/draw/away) → random guess ≈ 1/3
    return round(1.0 / 3.0, 4)


def compute(rows: list, by_date: bool = False) -> dict:
    settled = [r for r in rows if r.get("actual_result")]
    pending = [r for r in rows if not r.get("actual_result")]

    n = len(settled)
    our_hits = sum(1 for r in settled if _hit(r.get("our_pred"), r.get("actual_result")))

    # collect every baseline name present
    baseline_names = set()
    for r in settled:
        for k in (r.get("baselines") or {}):
            baseline_names.add(k)

    baselines_out = {}
    for name in sorted(baseline_names):
        hits = total = 0
        for r in settled:
            b = (r.get("baselines") or {}).get(name)
            if b is not None:
                total += 1
                if _hit(b, r.get("actual_result")):
                    hits += 1
        baselines_out[name] = {
            "hits": hits, "total": total,
            "hit_rate": round(hits / total, 4) if total else None,
        }

    result = {
        "settled": n,
        "pending": len(pending),
        "ours": {
            "hits": our_hits, "total": n,
            "hit_rate": round(our_hits / n, 4) if n else None,
        },
        "coin_flip_3way": _coin_baseline(n),
        "baselines": baselines_out,
    }

    if by_date:
        dates = {}
        for r in settled:
            d = r.get("date", "?")
            dates.setdefault(d, {"hits": 0, "total": 0})
            dates[d]["total"] += 1
            if _hit(r.get("our_pred"), r.get("actual_result")):
                dates[d]["hits"] += 1
        for d in dates:
            t = dates[d]["total"]
            dates[d]["hit_rate"] = round(dates[d]["hits"] / t, 4) if t else None
        result["by_date"] = dict(sorted(dates.items()))

    return result


def main():
    p = argparse.ArgumentParser(description="World Cup prediction hit-rate")
    p.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger JSON path")
    p.add_argument("--by-date", action="store_true", help="per-date breakdown")
    args = p.parse_args()

    try:
        rows = _load(args.ledger)
    except Exception as e:
        error_exit(f"failed to read ledger {args.ledger}: {e}")
        return
    output_json(compute(rows, by_date=args.by_date))


if __name__ == "__main__":
    main()

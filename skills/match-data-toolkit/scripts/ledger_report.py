#!/usr/bin/env python3
"""Render the World Cup prediction ledger in compact, deterministic tables."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path.home() / ".fastclaw/workspaces/agt_0810c26790ce1c32bb11/worldcup/ledger.json"


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open() as f:
        return json.load(f)


def verdict(row: dict[str, Any]) -> str:
    actual = row.get("actual_result")
    if not actual:
        return "⏳"
    return "✅" if row.get("our_pred") == actual else "❌"


def short_date(s: str | None) -> str:
    if not s:
        return ""
    return s[5:] if len(s) >= 10 else s


def cell(v: Any) -> str:
    if v is None or v == "":
        return "—"
    return str(v).replace("\n", " ").replace("|", "/")


def htft_cell(r: dict[str, Any]) -> str:
    """Build a compact HT/FT string from separate fields or our_score_pred."""
    htf = r.get("ht_ft_pred", "")
    hts = r.get("ht_score_pred", "")
    fts = r.get("ft_score_pred", "")
    if htf:
        parts = [f"HT {hts}" if hts else "", f"FT {fts}" if fts else "", htf]
        return " / ".join(p for p in parts if p)
    # Fallback: try to extract from our_score_pred
    osp = str(r.get("our_score_pred", ""))
    if "(HT" in osp or "HT" in osp:
        return osp
    return "—"


def render_table(rows: list[dict[str, Any]], start: int = 1) -> str:
    lines = [
        "| # | 日期 | 比赛 | 预测 | 信心 | 比分 | 半全场 | 实际 | 比分 | 状态 |",
        "|---:|---|---|---|---|---|---|---|---|:---:|",
    ]
    for idx, r in enumerate(rows, start):
        lines.append(
            "| {idx} | {date} | {match} | {pred} | {conf} | {score} | {htft} | {actual} | {actual_score} | {status} |".format(
                idx=idx,
                date=short_date(cell(r.get("date"))),
                match=cell(r.get("match")),
                pred=cell(r.get("our_pred")),
                conf=cell(r.get("our_confidence")),
                score=cell(r.get("our_score_pred")),
                htft=htft_cell(r),
                actual=cell(r.get("actual_result")),
                actual_score=cell(r.get("actual_score")),
                status=verdict(r),
            )
        )
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Render World Cup ledger tables")
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to ledger.json")
    ap.add_argument("--full", action="store_true", help="Print all rows")
    ap.add_argument("--pending", action="store_true", help="Print pending rows only")
    ap.add_argument("--settled", action="store_true", help="Print settled rows only")
    args = ap.parse_args()

    rows = load_rows(Path(os.path.expanduser(args.ledger)))
    settled = [r for r in rows if r.get("actual_result")]
    pending = [r for r in rows if not r.get("actual_result")]
    hits = sum(1 for r in settled if r.get("our_pred") == r.get("actual_result"))
    misses = len(settled) - hits
    hit_rate = hits / len(settled) if settled else 0.0

    if args.pending:
        selected = pending
        title = f"## 待结算 {len(pending)} 场"
        start = len(settled) + 1
    elif args.settled:
        selected = settled
        title = f"## 已结算 {len(settled)} 场"
        start = 1
    else:
        selected = rows
        title = f"## 全量预测数据 {len(rows)} 行"
        start = 1

    print(title)
    print()
    print(f"统计：总计 {len(rows)} 行；已结算 {len(settled)}；待结算 {len(pending)}；命中 {hits}；未中 {misses}；命中率 {hit_rate:.1%}。")
    print()
    print(render_table(selected, start))
    print()
    print(f"以上，{len(selected)} 行展示完毕。")


if __name__ == "__main__":
    main()

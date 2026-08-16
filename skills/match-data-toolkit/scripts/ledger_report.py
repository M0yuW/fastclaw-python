#!/usr/bin/env python3
"""Render a football prediction ledger in the fixed customer-facing format."""

from __future__ import annotations

import argparse
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

DEFAULT_LEDGER = Path(
    os.environ.get(
        "FOOTBALL_LEDGER",
        os.environ.get(
            "WC_LEDGER",
            str(Path.home() / ".fastclaw/workspaces/agt_0810c26790ce1c32bb11/worldcup/ledger.json"),
        ),
    )
)


def load_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
        raise ValueError("ledger must contain an array of objects")
    return coalesce_rows(payload)[0]


def competition_key(value: Any) -> str:
    text = " ".join(str(value or "").split())
    normalized = unicodedata.normalize("NFKC", text).casefold()
    compact = re.sub(r"[^\w]+", "", normalized)
    aliases = {
        "西甲": "spanish-laliga",
        "西甲laliga": "spanish-laliga",
        "laliga": "spanish-laliga",
        "西班牙甲级联赛": "spanish-laliga",
        "spanishlaliga": "spanish-laliga",
        "荷甲": "dutch-eredivisie",
        "eredivisie": "dutch-eredivisie",
        "dutcheredivisie": "dutch-eredivisie",
    }
    return aliases.get(compact, compact)


def team_key(value: Any) -> str:
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", str(value or "")).casefold()
        if not unicodedata.combining(char)
    )
    compact = "".join(re.findall(r"[\w]+", normalized))
    if compact.startswith(("fc", "sbv", "afc", "sc")) and len(compact) > 2:
        compact = re.sub(r"^(?:fc|sbv|afc|sc)", "", compact)
    return {
        "阿拉维斯": "alaves",
        "deportivoalaves": "alaves",
        "赫塔费": "getafe",
        "fcutrecht": "utrecht",
        "utrecht": "utrecht",
        "精英": "excelsior",
        "excelsior": "excelsior",
        "威廉二世": "willemii",
        "奈梅亨": "nec",
        "阿尔克马": "az",
        "azalkmaar": "az",
        "埃因霍温": "psveindhoven",
        "psv": "psveindhoven",
        "福图纳锡塔德": "fortunasittard",
        "坎布尔": "cambuur",
    }.get(compact, compact)


def entry_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    parts = re.split(r"\s*vs\.?\s*", str(row.get("match") or ""), maxsplit=1, flags=re.I)
    return (
        competition_key(row.get("competition")),
        str(row.get("date") or "").strip(),
        team_key(parts[0]),
        team_key(parts[1]) if len(parts) == 2 else "",
    )


def coalesce_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    result: list[dict[str, Any]] = []
    positions: dict[tuple[str, str, str, str], int] = {}
    merged_count = 0
    for source in rows:
        row = dict(source)
        key = entry_key(row)
        if key not in positions:
            positions[key] = len(result)
            result.append(row)
            continue
        merged_count += 1
        current = result[positions[key]]
        old_notes = current.get("notes")
        old_pred = current.get("our_pred")
        old_confidence = current.get("our_confidence")
        for field, value in row.items():
            if field not in {"competition", "date", "match", "notes"} and value not in (None, ""):
                current[field] = value
        if row.get("notes"):
            current["notes"] = row["notes"]
        history = []
        if old_pred and old_pred != current.get("our_pred"):
            history.append(f"旧预测: {old_pred}")
        if old_confidence and old_confidence != current.get("our_confidence"):
            history.append(f"旧信心: {old_confidence}")
        if old_notes and old_notes != row.get("notes"):
            history.append(f"旧备注: {old_notes}")
        if history:
            current["notes"] = (str(current.get("notes") or "").strip() + "\n[合并旧记录] " + "; ".join(history)).strip()
    return result, merged_count


def verdict(row: dict[str, Any]) -> str:
    actual = row.get("actual_result")
    if not actual:
        return "⏳"
    return "✅" if row.get("our_pred") == actual else "❌"


def short_date(value: Any) -> str:
    text = str(value or "")
    return text[5:] if len(text) >= 10 else text


def cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return str(value).replace("\n", " ").replace("|", "/")


def htft_cell(row: dict[str, Any]) -> str:
    """Build the compact HT/FT value used by the Go World Cup report."""

    half_full = row.get("ht_ft_pred", "")
    half_score = row.get("ht_score_pred", "")
    full_score = row.get("ft_score_pred", "")
    if half_full:
        parts = [
            f"HT {half_score}" if half_score else "",
            f"FT {full_score}" if full_score else "",
            str(half_full),
        ]
        return " / ".join(part for part in parts if part)
    score_prediction = str(row.get("our_score_pred", ""))
    return score_prediction if "HT" in score_prediction else ""


def render_table(rows: list[dict[str, Any]], start: int = 1) -> str:
    lines = [
        "| # | 赛事 | 赛季 | 日期 | 比赛 | 1X2预测 | 信心 | 比分 | 半全场 | 实际 | 比分 | 状态 |",
        "|---:|---|---|---|---|---|---|---|---|---|---|:---:|",
    ]
    for index, row in enumerate(rows, start):
        values = (
            index,
            row.get("competition", ""),
            row.get("season", ""),
            short_date(row.get("date")),
            row.get("match", ""),
            row.get("our_pred", ""),
            row.get("our_confidence", ""),
            row.get("our_score_pred", ""),
            htft_cell(row),
            row.get("actual_result", ""),
            row.get("actual_score", ""),
            verdict(row),
        )
        lines.append("| " + " | ".join(cell(value) for value in values) + " |")
    return "\n".join(lines)


def render_report(
    rows: list[dict[str, Any]], selected: list[dict[str, Any]], title: str, start: int = 1
) -> str:
    settled = [row for row in rows if row.get("actual_result")]
    pending = [row for row in rows if not row.get("actual_result")]
    hits = sum(1 for row in settled if row.get("our_pred") == row.get("actual_result"))
    misses = len(settled) - hits
    hit_rate = hits / len(settled) if settled else 0.0
    lines = [
        title,
        "",
        (
            f"统计: 总计 {len(rows)} 行; 已结算 {len(settled)}; 待结算 {len(pending)}; "
            f"命中 {hits}; 未中 {misses}; 命中率 {hit_rate:.1%}。"
        ),
        "",
        render_table(selected, start),
        "",
        f"以上, {len(selected)} 行展示完毕。",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Render football ledger tables")
    parser.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to ledger.json")
    parser.add_argument("--full", action="store_true", help="Print all rows")
    parser.add_argument("--pending", action="store_true", help="Print pending rows only")
    parser.add_argument("--settled", action="store_true", help="Print settled rows only")
    parser.add_argument("--competition", help="Filter by competition")
    parser.add_argument("--season", help="Filter by season")
    args = parser.parse_args()

    rows = load_rows(Path(os.path.expanduser(args.ledger)))
    if args.competition:
        rows = [row for row in rows if competition_key(row.get("competition")) == competition_key(args.competition)]
    if args.season:
        rows = [row for row in rows if str(row.get("season") or "") == args.season]

    settled_count = sum(1 for row in rows if row.get("actual_result"))
    pending = [row for row in rows if not row.get("actual_result")]
    settled = [row for row in rows if row.get("actual_result")]
    if args.pending:
        selected = pending
        title = f"## 待结算 {len(pending)} 场"
        start = settled_count + 1
    elif args.settled:
        selected = settled
        title = f"## 已结算 {len(settled)} 场"
        start = 1
    else:
        selected = rows
        title = f"## 全量预测数据 {len(rows)} 行"
        start = 1

    # Keep the statistics scoped to the requested competition/season while the
    # table selection follows the Go report's pending/settled switches.
    print(render_report(rows, selected, title, start))


if __name__ == "__main__":
    main()

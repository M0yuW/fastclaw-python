#!/usr/bin/env python3
"""Back up and merge semantically duplicate FastClaw football ledgers.

This is intentionally a separate migration command.  Runtime ledger requests
never overwrite a duplicate set without this explicit backup step.
"""

from __future__ import annotations

import argparse
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from fastclaw.tools.football import FootballLedgerTool


def _backup_path(path: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.pre-dedupe-{stamp}.json")
    suffix = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.pre-dedupe-{stamp}-{suffix}.json")
        suffix += 1
    return candidate


def _write(path: Path, rows: list[dict[str, object]]) -> None:
    temporary = path.with_name(f".{path.name}.migration.tmp")
    temporary.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def migrate(data_root: Path, *, dry_run: bool) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for ledger in sorted((data_root / "workspaces").glob("*/football/ledger.json")):
        payload = json.loads(ledger.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError(f"invalid ledger: {ledger}")
        merged, duplicate_count = FootballLedgerTool.normalize_rows(payload)
        report: dict[str, object] = {
            "ledger": str(ledger),
            "rowsBefore": len(payload),
            "rowsAfter": len(merged),
            "mergedRows": duplicate_count,
            "normalizedRows": sum(
                1 for before, after in zip(payload, merged, strict=False) if before != after
            ),
            "changed": merged != payload,
        }
        if merged != payload and not dry_run:
            backup = _backup_path(ledger)
            shutil.copy2(ledger, backup)
            _write(ledger, merged)
            report["backup"] = str(backup)
        reports.append(report)
    return reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    reports = migrate(args.data_root.expanduser().resolve(), dry_run=args.dry_run)
    print(json.dumps({"dryRun": args.dry_run, "ledgers": reports}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

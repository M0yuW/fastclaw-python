"""Structured, atomic ledger operations for arbitrary football competitions."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import anyio

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult

_LOCKS: defaultdict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)


class FootballLedgerTool:
    """Maintain one competition-aware prediction ledger per Agent."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.expanduser().resolve()
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_ledger",
                description=(
                    "Append, settle, or report the current Agent's structured football "
                    "competition ledger. Every row is scoped by competition, date, and match. "
                    "Reports are returned directly without model rewriting."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "operation": {"type": "string", "enum": ["append", "settle", "report"]},
                        "entry": {"type": "object"},
                        "competition": {"type": "string"},
                        "season": {"type": "string"},
                        "date": {"type": "string"},
                        "match": {"type": "string"},
                        "actual_result": {"type": "string"},
                        "actual_score": {"type": "string"},
                        "pending_only": {"type": "boolean"},
                    },
                    "required": ["operation"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        ledger = self._ledger_path(context.agent_id)
        lock = _LOCKS[ledger]
        async with lock:
            rows = await anyio.to_thread.run_sync(self._read, ledger)
            operation = str(arguments["operation"])
            if operation == "append":
                entry = arguments.get("entry")
                if not isinstance(entry, dict):
                    raise ValueError("append requires an entry object")
                self._validate_entry(entry)
                key = self._entry_key(entry)
                if any(self._entry_key(row) == key for row in rows):
                    raise ValueError("ledger already contains this competition, date, and match")
                rows.append(dict(entry))
                await anyio.to_thread.run_sync(self._write, ledger, rows)
                return ToolResult(content="prediction appended")
            if operation == "settle":
                key = (
                    self._key_part(arguments.get("competition")),
                    str(arguments.get("date") or "").strip(),
                    self._key_part(arguments.get("match")),
                )
                if not all(key):
                    raise ValueError("settle requires competition, date, and match")
                selected = [row for row in rows if self._entry_key(row) == key]
                if len(selected) != 1:
                    raise ValueError(
                        "settle must identify exactly one competition, date, and match"
                    )
                selected[0]["actual_result"] = str(arguments.get("actual_result") or "")
                selected[0]["actual_score"] = str(arguments.get("actual_score") or "") or None
                await anyio.to_thread.run_sync(self._write, ledger, rows)
                return ToolResult(content="prediction settled")
            if operation == "report":
                competition = self._key_part(arguments.get("competition"))
                season = self._key_part(arguments.get("season"))
                if competition:
                    rows = [
                        row for row in rows if self._key_part(row.get("competition")) == competition
                    ]
                if season:
                    rows = [row for row in rows if self._key_part(row.get("season")) == season]
                if bool(arguments.get("pending_only")):
                    rows = [row for row in rows if not row.get("actual_result")]
                return ToolResult(content=self._report(rows), direct_return=True)
            raise ValueError("unknown ledger operation")

    def _ledger_path(self, agent_id: str) -> Path:
        workspace = (self._data_root / "workspaces" / agent_id).resolve()
        if not workspace.is_relative_to(self._data_root):
            raise ValueError("invalid Agent workspace")
        return workspace / "football" / "ledger.json"

    @staticmethod
    def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str]:
        return (
            FootballLedgerTool._key_part(entry.get("competition")),
            str(entry.get("date") or "").strip(),
            FootballLedgerTool._key_part(entry.get("match")),
        )

    @staticmethod
    def _key_part(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    @staticmethod
    def _validate_entry(entry: dict[str, Any]) -> None:
        for field in ("competition", "date", "match", "our_pred"):
            if not isinstance(entry.get(field), str) or not entry[field].strip():
                raise ValueError(f"entry requires non-empty {field}")

    @staticmethod
    def _read(path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ValueError("ledger must contain an array of objects")
        return payload

    @staticmethod
    def _write(path: Path, rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".ledger.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(rows, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _report(rows: list[dict[str, Any]]) -> str:
        lines = [
            "| Competition | Season | Date | Match | Prediction | Confidence | Actual | Score |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for row in rows:
            values = (
                row.get("competition", ""),
                row.get("season", ""),
                row.get("date", ""),
                row.get("match", ""),
                row.get("our_pred", ""),
                row.get("our_confidence", ""),
                row.get("actual_result", "") or "pending",
                row.get("actual_score", "") or "",
            )
            escaped = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
            lines.append("| " + " | ".join(escaped) + " |")
        return "\n".join(lines)

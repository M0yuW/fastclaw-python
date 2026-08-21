"""Structured, atomic ledger operations for arbitrary football competitions."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

import anyio

from fastclaw.execution import ExecutionContext, football_team_identity
from fastclaw.football_identity import resolve_team_identity
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult
from fastclaw.tools.football_competitions import (
    canonical_competition_display,
    canonical_competition_identity,
)

_LOCKS: defaultdict[Path, asyncio.Lock] = defaultdict(asyncio.Lock)


class FootballLedgerTool:
    """Maintain one competition-aware prediction ledger per Agent."""

    def __init__(self, data_root: Path) -> None:
        self._data_root = data_root.expanduser().resolve()
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="football_ledger",
                description=(
                    "Append, update, settle, or report the current Agent's structured football "
                    "competition ledger. Every row is scoped by competition, date, and match. "
                    "Use append only for a new key; use update for an existing key. Reports are "
                    "returned directly without model rewriting."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": ["append", "update", "settle", "report"],
                        },
                        "entry": {
                            "type": "object",
                            "description": (
                                "For append, put all new prediction fields inside this object. "
                                "For update, put the existing competition, date, and match key "
                                "plus only the fields that should change inside this object."
                            ),
                            "properties": {
                                "competition": {"type": "string"},
                                "season": {"type": "string"},
                                "date": {"type": "string"},
                                "match": {"type": "string"},
                                "home_team_id": {"type": "string"},
                                "away_team_id": {"type": "string"},
                                "home_canonical_id": {"type": "string"},
                                "away_canonical_id": {"type": "string"},
                                "our_pred": {"type": "string"},
                                "our_confidence": {"type": "string"},
                                "our_score_pred": {
                                    "type": "string",
                                    "description": "Exact full-time score, for example 2-0",
                                },
                                "ht_ft_pred": {
                                    "type": "string",
                                    "description": "Half-time/full-time outcome, for example 平/胜",
                                },
                                "ht_score_pred": {
                                    "type": "string",
                                    "description": "Exact half-time score, for example 0-0",
                                },
                                "ft_score_pred": {
                                    "type": "string",
                                    "description": (
                                        "Exact full-time score; must equal our_score_pred"
                                    ),
                                },
                                "baselines": {"type": "object"},
                                "ev_review": {"type": "object"},
                                "actual_result": {"type": "string"},
                                "actual_score": {"type": "string"},
                                "notes": {"type": "string"},
                            },
                        },
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
                entry = self._with_team_identity(self._normalize_entry(arguments))
                self._validate_entry(entry)
                key = self._entry_key(entry)
                if any(self._same_fixture(row, entry) for row in rows):
                    return ToolResult(
                        content=(
                            "existing prediction found; append made no change. "
                            "Use operation=update with the same competition, date, and match "
                            "to modify it."
                        ),
                        metadata={
                            "status": "existing_requires_update",
                            "entryKey": "|".join(key),
                        },
                    )
                rows.append(dict(entry))
                await anyio.to_thread.run_sync(self._write, ledger, rows)
                return ToolResult(content="prediction appended", metadata={"status": "appended"})
            if operation == "update":
                entry = self._with_team_identity(self._normalize_entry(arguments))
                self._validate_identity(entry)
                key = self._entry_key(entry)
                selected = [row for row in rows if self._same_fixture(row, entry)]
                if len(selected) != 1:
                    return ToolResult(
                        content=(
                            "football_ledger update rejected: the competition, date, and match "
                            "did not identify exactly one existing row. This is a deterministic "
                            "key mismatch; do not retry unchanged. Call operation=report and "
                            "reuse the stored competition and match labels."
                        ),
                        is_error=True,
                        metadata={
                            "status": "update_key_mismatch",
                            "errorCode": "ledger_update_key_mismatch",
                            "entryKey": "|".join(key),
                        },
                    )
                changes = {
                    field: value
                    for field, value in entry.items()
                    if field not in {"competition", "date", "match"}
                }
                if not changes:
                    raise ValueError("update requires at least one field to change")
                updated = dict(selected[0])
                updated.update(changes)
                self._validate_entry(updated)
                selected[0].update(changes)
                await anyio.to_thread.run_sync(self._write, ledger, rows)
                return ToolResult(
                    content="prediction updated",
                    metadata={"status": "updated", "entryKey": "|".join(key)},
                )
            if operation == "settle":
                # Models commonly use the same nested ``entry`` shape as append
                # and update, while older prompts emitted the identity fields at
                # the top level.  Accept both forms for settlement as well.
                entry = self._with_team_identity(self._normalize_entry(arguments))
                try:
                    self._validate_identity(entry)
                except ValueError as exc:
                    return ToolResult(
                        content=f"football_ledger settlement rejected: {exc}",
                        is_error=True,
                        metadata={"errorCode": "ledger_settle_invalid_identity"},
                    )
                key = self._entry_key(entry)
                selected = [row for row in rows if self._same_fixture(row, entry)]
                if len(selected) != 1:
                    return ToolResult(
                        content=(
                            "football_ledger settlement rejected: expected exactly one "
                            f"existing fixture, matched {len(selected)} "
                            f"for {' | '.join(key)}"
                        ),
                        is_error=True,
                        metadata={
                            "errorCode": "ledger_settle_fixture_not_unique",
                            "entryKey": "|".join(key),
                            "matchCount": len(selected),
                        },
                    )
                selected[0]["actual_result"] = str(entry.get("actual_result") or "")
                selected[0]["actual_score"] = str(entry.get("actual_score") or "") or None
                await anyio.to_thread.run_sync(self._write, ledger, rows)
                return ToolResult(
                    content="prediction settled",
                    metadata={"status": "settled", "entryKey": "|".join(key)},
                )
            if operation == "report":
                competition = self._competition_key(arguments.get("competition"))
                season = self._key_part(arguments.get("season"))
                if competition:
                    rows = [
                        row for row in rows
                        if self._competition_key(row.get("competition")) == competition
                    ]
                if season:
                    rows = [row for row in rows if self._key_part(row.get("season")) == season]
                if bool(arguments.get("pending_only")):
                    rows = [row for row in rows if not row.get("actual_result")]
                return ToolResult(
                    content=self._report(rows),
                    direct_return=True,
                    metadata={"status": "report"},
                )
            raise ValueError("unknown ledger operation")

    def _ledger_path(self, agent_id: str) -> Path:
        workspace = (self._data_root / "workspaces" / agent_id).resolve()
        if not workspace.is_relative_to(self._data_root):
            raise ValueError("invalid Agent workspace")
        return workspace / "football" / "ledger.json"

    @classmethod
    def _normalize_entry(cls, arguments: dict[str, Any]) -> dict[str, Any]:
        """Accept the canonical nested shape and common model-emitted aliases."""

        raw_entry = arguments.get("entry")
        entry = dict(raw_entry) if isinstance(raw_entry, dict) else {}
        for field in (
            "competition",
            "season",
            "date",
            "match",
            "home_team_id",
            "away_team_id",
            "home_canonical_id",
            "away_canonical_id",
            "our_pred",
            "our_confidence",
            "actual_result",
            "actual_score",
        ):
            if field not in entry and isinstance(arguments.get(field), str):
                entry[field] = arguments[field]
        if "our_pred" not in entry:
            for alias in ("prediction", "pred", "lean"):
                value = entry.get(alias)
                if isinstance(value, str) and value.strip():
                    entry["our_pred"] = value
                    break
        if "our_confidence" not in entry:
            value = entry.get("confidence")
            if isinstance(value, str) and value.strip():
                entry["our_confidence"] = value
        if isinstance(entry.get("competition"), str) and entry["competition"].strip():
            entry["competition"] = canonical_competition_display(entry["competition"])
        if isinstance(entry.get("ht_ft_pred"), str) and entry["ht_ft_pred"].strip():
            entry["ht_ft_pred"] = cls._canonical_htft_display(entry["ht_ft_pred"])
        return entry

    @classmethod
    def _with_team_identity(cls, entry: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(entry)
        home, away = cls._split_match(enriched.get("match"))
        if home and away:
            enriched.setdefault(
                "home_canonical_id", resolve_team_identity(home).canonical_id
            )
            enriched.setdefault(
                "away_canonical_id", resolve_team_identity(away).canonical_id
            )
        return enriched

    @staticmethod
    def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str, str]:
        return FootballLedgerTool._identity_key(
            entry.get("competition"), entry.get("date"), entry.get("match")
        )

    @classmethod
    def _same_fixture(cls, left: dict[str, Any], right: dict[str, Any]) -> bool:
        """Compare a fixture using canonical names and tolerant date forms.

        Reports intentionally render dates as ``MM-DD``.  Models often copy
        that presentation value back into a settle/update call even though the
        stored ledger row contains ``YYYY-MM-DD``.  A short date is safe here
        because competition and home/away identities remain part of the key;
        if it could match multiple years, the caller still rejects it as
        non-unique instead of updating an arbitrary row.
        """

        return (
            cls._competition_key(left.get("competition"))
            == cls._competition_key(right.get("competition"))
            and bool(
                cls._team_identity_candidates(left, home=True)
                & cls._team_identity_candidates(right, home=True)
            )
            and bool(
                cls._team_identity_candidates(left, home=False)
                & cls._team_identity_candidates(right, home=False)
            )
            and cls._dates_match(left.get("date"), right.get("date"))
        )

    @classmethod
    def _team_identity_candidates(cls, entry: dict[str, Any], *, home: bool) -> set[str]:
        home_name, away_name = cls._split_match(entry.get("match"))
        team_name = home_name if home else away_name
        field = "home_canonical_id" if home else "away_canonical_id"
        candidates: set[str] = set()
        explicit = str(entry.get(field) or "").strip().casefold()
        if explicit:
            candidates.add(explicit)
        if team_name:
            candidates.add(resolve_team_identity(team_name).canonical_id.casefold())
            candidates.add(football_team_identity(team_name).casefold())
        return {candidate for candidate in candidates if candidate}

    @staticmethod
    def _dates_match(left: Any, right: Any) -> bool:
        left_parts = FootballLedgerTool._date_parts(left)
        right_parts = FootballLedgerTool._date_parts(right)
        if left_parts is None or right_parts is None:
            return FootballLedgerTool._key_part(left) == FootballLedgerTool._key_part(right)
        left_year, left_month, left_day = left_parts
        right_year, right_month, right_day = right_parts
        if (left_month, left_day) != (right_month, right_day):
            return False
        return not left_year or not right_year or left_year == right_year

    @staticmethod
    def _date_parts(value: Any) -> tuple[str, str, str] | None:
        text = str(value or "").strip().replace("/", "-").replace(".", "-")
        text = text.split("T", 1)[0].split(" ", 1)[0]
        full = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
        if full:
            return full.group(1), full.group(2).zfill(2), full.group(3).zfill(2)
        short = re.fullmatch(r"(\d{1,2})-(\d{1,2})", text)
        if short:
            return "", short.group(1).zfill(2), short.group(2).zfill(2)
        return None

    @staticmethod
    def _identity_key(
        competition: Any, date: Any, match: Any
    ) -> tuple[str, str, str, str]:
        home, away = FootballLedgerTool._split_match(match)
        return (
            FootballLedgerTool._competition_key(competition),
            str(date or "").strip(),
            football_team_identity(home) if home else FootballLedgerTool._key_part(match),
            football_team_identity(away) if away else "",
        )

    @staticmethod
    def _split_match(value: Any) -> tuple[str, str]:
        text = " ".join(str(value or "").split())
        parts = re.split(r"\s+(?:vs\.?|[-\u2013\u2014])\s+", text, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 2:
            return parts[0], parts[1]
        parts = re.split(r"\s*(?:vs\.?|[-\u2013\u2014])\s*", text, maxsplit=1, flags=re.IGNORECASE)
        return (parts[0], parts[1]) if len(parts) == 2 else (text, "")

    @staticmethod
    def _competition_key(value: Any) -> str:
        text = " ".join(str(value or "").split())
        return canonical_competition_identity(text) if text else ""

    @classmethod
    def coalesce_rows(cls, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Merge semantic duplicates for the explicit, backed-up migration step."""

        merged: list[dict[str, Any]] = []
        positions: dict[tuple[str, str, str, str], int] = {}
        merged_count = 0
        for source in rows:
            row = dict(source)
            key = cls._entry_key(row)
            position = positions.get(key)
            if position is None:
                positions[key] = len(merged)
                merged.append(row)
                continue
            merged_count += 1
            current = merged[position]
            prior_pred = current.get("our_pred")
            prior_confidence = current.get("our_confidence")
            prior_notes = current.get("notes")
            latest_notes = row.get("notes")
            for field, value in row.items():
                if field not in {"competition", "season", "date", "match", "notes"}:
                    if value is not None and value != "":
                        current[field] = value
            for field in ("season", "date", "match"):
                if not current.get(field) and row.get(field):
                    current[field] = row[field]
            if row.get("notes"):
                current["notes"] = row["notes"]
            history: list[str] = []
            if prior_pred and prior_pred != current.get("our_pred"):
                history.append(f"旧预测: {prior_pred}")
            if prior_confidence and prior_confidence != current.get("our_confidence"):
                history.append(f"旧信心: {prior_confidence}")
            if prior_notes and prior_notes != latest_notes:
                history.append(f"旧备注: {prior_notes}")
            if history:
                suffix = "[合并旧记录] " + "; ".join(history)
                existing_notes = str(current.get("notes") or "").strip()
                current["notes"] = f"{existing_notes}\n{suffix}".strip()
        return merged, merged_count

    @classmethod
    def normalize_rows(cls, rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
        """Normalize customer-facing labels before deduplicating a ledger."""

        normalized: list[dict[str, Any]] = []
        for source in rows:
            row = dict(source)
            if isinstance(row.get("competition"), str):
                row["competition"] = canonical_competition_display(row["competition"])
            if isinstance(row.get("ht_ft_pred"), str) and row["ht_ft_pred"].strip():
                try:
                    row["ht_ft_pred"] = cls._canonical_htft_display(row["ht_ft_pred"])
                except ValueError:
                    # Preserve an invalid legacy value for manual review rather
                    # than making the migration destructive.
                    pass
            normalized.append(row)
        return cls.coalesce_rows(normalized)

    @staticmethod
    def _key_part(value: Any) -> str:
        return " ".join(str(value or "").split()).casefold()

    @staticmethod
    def _validate_entry(entry: dict[str, Any]) -> None:
        FootballLedgerTool._validate_identity(entry)
        if not isinstance(entry.get("our_pred"), str) or not entry["our_pred"].strip():
            raise ValueError("entry requires non-empty our_pred")
        FootballLedgerTool._validate_prediction_consistency(entry)

    @staticmethod
    def _score_outcome(value: Any, *, field: str) -> tuple[str, tuple[int, int]] | None:
        text = str(value or "").strip()
        if not text:
            return None
        matched = re.fullmatch(r"(\d+)\s*[-:]\s*(\d+)", text)
        if matched is None:
            raise ValueError(f"{field} must be an exact score such as 2-0")
        home, away = int(matched.group(1)), int(matched.group(2))
        outcome = "主胜" if home > away else "客胜" if home < away else "平"
        return outcome, (home, away)

    @staticmethod
    def _htft_outcomes(value: Any) -> tuple[str, str] | None:
        text = " ".join(str(value or "").split()).casefold()
        if not text:
            return None
        parts = re.split(r"\s*(?:/|→|>)\s*", text)
        if len(parts) != 2:
            raise ValueError("ht_ft_pred must contain half-time/full-time outcomes such as 平/胜")
        aliases = {
            "胜": "主胜",
            "主胜": "主胜",
            "主": "主胜",
            "home": "主胜",
            "home win": "主胜",
            "平": "平",
            "平局": "平",
            "draw": "平",
            "负": "客胜",
            "客胜": "客胜",
            "客": "客胜",
            "away": "客胜",
            "away win": "客胜",
        }
        try:
            return aliases[parts[0]], aliases[parts[1]]
        except KeyError as exc:
            raise ValueError(
                "ht_ft_pred outcomes must be home win/draw/away win or 胜/平/负"
            ) from exc

    @classmethod
    def _canonical_htft_display(cls, value: Any) -> str:
        """Normalize half/full-time shorthand to the fixed 胜/平/负 display form."""

        parsed = cls._htft_outcomes(value)
        if parsed is None:
            return ""
        labels = {"主胜": "胜", "平": "平", "客胜": "负"}
        return "/".join(labels[item] for item in parsed)

    @staticmethod
    def _validate_prediction_consistency(entry: dict[str, Any]) -> None:
        score = FootballLedgerTool._score_outcome(
            entry.get("our_score_pred"), field="our_score_pred"
        )
        full_score = FootballLedgerTool._score_outcome(
            entry.get("ft_score_pred"), field="ft_score_pred"
        )
        half_score = FootballLedgerTool._score_outcome(
            entry.get("ht_score_pred"), field="ht_score_pred"
        )
        if score and full_score and score[1] != full_score[1]:
            raise ValueError("ft_score_pred must equal our_score_pred")
        resolved_full = full_score or score
        prediction = FootballLedgerTool._prediction_outcome(entry.get("our_pred"))
        if resolved_full and prediction in {"主胜", "平", "客胜"}:
            if resolved_full[0] != prediction:
                raise ValueError("full-time score outcome must equal our_pred 1X2 direction")
        if half_score and resolved_full:
            if (
                half_score[1][0] > resolved_full[1][0]
                or half_score[1][1] > resolved_full[1][1]
            ):
                raise ValueError("full-time goals cannot be below half-time goals")
        htft = FootballLedgerTool._htft_outcomes(entry.get("ht_ft_pred"))
        if htft and half_score and htft[0] != half_score[0]:
            raise ValueError("ht_ft_pred half-time outcome must equal ht_score_pred")
        if htft and resolved_full and htft[1] != resolved_full[0]:
            raise ValueError("ht_ft_pred full-time outcome must equal full-time score")

    @staticmethod
    def _validate_identity(entry: dict[str, Any]) -> None:
        for field in ("competition", "date", "match"):
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
        settled = [row for row in rows if row.get("actual_result")]
        pending = [row for row in rows if not row.get("actual_result")]
        evaluated = [
            row
            for row in settled
            if FootballLedgerTool._prediction_outcome(row.get("our_pred"))
        ]
        hits = sum(
            1
            for row in evaluated
            if FootballLedgerTool._prediction_outcome(row.get("our_pred"))
            == FootballLedgerTool._prediction_outcome(row.get("actual_result"))
        )
        misses = len(evaluated) - hits
        not_rated = len(settled) - len(evaluated)
        hit_rate = hits / len(evaluated) if evaluated else 0.0
        stats = (
            f"统计: 总计 {len(rows)} 行; 已结算 {len(settled)}; 待结算 {len(pending)}; "
            f"命中 {hits}; 未中 {misses}"
        )
        if not_rated:
            stats += f"; 无明确方向 {not_rated}"
        stats += f"; 命中率 {hit_rate:.1%}。"

        lines = [
            f"## 全量预测数据 {len(rows)} 行",
            "",
            (
                stats
            ),
            "",
            (
                "| # | 赛事 | 赛季 | 日期 | 比赛 | 1X2预测 | 信心 | 比分 | "
                "半全场 | 实际 | 比分 | 状态 |"
            ),
            "|---:|---|---|---|---|---|---|---|---|---|---|:---:|",
        ]
        for index, row in enumerate(rows, 1):
            values = (
                index,
                canonical_competition_display(row.get("competition", "")),
                row.get("season", ""),
                FootballLedgerTool._short_date(row.get("date")),
                row.get("match", ""),
                row.get("our_pred", ""),
                row.get("our_confidence", ""),
                row.get("our_score_pred", ""),
                FootballLedgerTool._htft_cell(row),
                row.get("actual_result", ""),
                row.get("actual_score", ""),
                FootballLedgerTool._verdict(row),
            )
            rendered_values = " | ".join(FootballLedgerTool._cell(value) for value in values)
            lines.append("| " + rendered_values + " |")
        lines.extend(("", f"以上, {len(rows)} 行展示完毕。"))
        return "\n".join(lines)

    @staticmethod
    def _cell(value: Any) -> str:
        if value is None or value == "":
            return "—"
        return str(value).replace("\n", " ").replace("|", "/")

    @staticmethod
    def _short_date(value: Any) -> str:
        text = str(value or "")
        return text[5:] if len(text) >= 10 else text

    @staticmethod
    def _verdict(row: dict[str, Any]) -> str:
        actual = row.get("actual_result")
        if not actual:
            return "⏳"
        prediction = FootballLedgerTool._prediction_outcome(row.get("our_pred"))
        if not prediction:
            return "—"
        return "✅" if prediction == FootballLedgerTool._prediction_outcome(actual) else "❌"

    @staticmethod
    def _prediction_outcome(value: Any) -> str:
        """Reduce decorated predictions to their comparable 1X2 direction."""

        text = " ".join(str(value or "").split()).casefold()
        if not text or "no clear edge" in text or "无明确方向" in text:
            return ""
        if "主胜" in text or "home win" in text:
            return "主胜"
        if "客胜" in text or "away win" in text:
            return "客胜"
        if text == "平" or "draw" in text or "平局" in text:
            return "平"
        return text

    @staticmethod
    def _htft_cell(row: dict[str, Any]) -> str:
        half_full = FootballLedgerTool._canonical_htft_display(row.get("ht_ft_pred", ""))
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
        if "HT" in score_prediction:
            return score_prediction
        return ""

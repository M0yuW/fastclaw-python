"""Read-only, idempotent import from a FastClaw Go SQLite database."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastclaw.storage import (
    AgentFileRecord,
    AgentRecord,
    APIKeyRecord,
    ConfigRecord,
    CronJobRecord,
    Database,
    SessionRecord,
    SQLAlchemyStore,
    UserRecord,
)
from fastclaw.storage.models import (
    AgentFileModel,
    AgentModel,
    APIKeyAgentModel,
    APIKeyModel,
    ConfigModel,
    CronJobModel,
    ImportRunModel,
    SessionModel,
    UserModel,
)

_IMPORT_TABLES = (
    "users",
    "apikeys",
    "agents",
    "apikey_agents",
    "sessions",
    "agent_files",
    "configs",
    "cron_jobs",
)
_SECRET_KEYS = {
    "accesskey",
    "access_key",
    "apikey",
    "api_key",
    "apptoken",
    "app_token",
    "bottoken",
    "bot_token",
    "password",
    "secret",
    "secretkey",
    "secret_key",
    "token",
}


class ImportReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_path: str
    source_sha256: str
    dry_run: bool
    idempotent: bool = False
    source_counts: dict[str, int] = Field(default_factory=dict)
    target_counts: dict[str, int] = Field(default_factory=dict)
    skipped_web_sessions: int = 0
    redacted_secrets: int = 0
    agent_file_sha256: dict[str, str] = Field(default_factory=dict)
    foreign_key_errors: tuple[str, ...] = ()


async def import_go_database(
    *, source: Path, target_url: str, dry_run: bool = False
) -> ImportReport:
    """Import a Go SQLite database without modifying it."""

    source = await anyio.to_thread.run_sync(_validated_source, source)
    if _sqlite_target_path(target_url) == source:
        raise ValueError("source and target SQLite paths must be different")

    before_sha = _sha256_file(source)
    with _read_only_sqlite(source) as connection:
        source_counts = {table: _table_count(connection, table) for table in _IMPORT_TABLES}
        skipped_web_sessions = _table_count(connection, "web_sessions")
        preview = _read_source(connection)
    after_sha = _sha256_file(source)
    if before_sha != after_sha:
        raise RuntimeError("source database changed while it was being read")

    redacted_configs, redacted_secrets = _redact_configs(preview["configs"])
    preview["configs"] = redacted_configs
    file_manifest = {
        f"{row['agent_id']}:{row.get('user_id', '')}:{row['filename']}": hashlib.sha256(
            _bytes(row.get("content", ""))
        ).hexdigest()
        for row in preview["agent_files"]
    }
    base_report = ImportReport(
        source_path=str(source),
        source_sha256=before_sha,
        dry_run=dry_run,
        source_counts=source_counts,
        skipped_web_sessions=skipped_web_sessions,
        redacted_secrets=redacted_secrets,
        agent_file_sha256=file_manifest,
    )
    if dry_run:
        return base_report

    database = Database(target_url)
    try:
        await database.create_schema()
        async with database.session() as session:
            previous = await session.get(ImportRunModel, before_sha)
            if previous is not None:
                prior = ImportReport.model_validate(previous.report)
                return prior.model_copy(update={"idempotent": True})
            existing_counts = await _target_counts(session)
            if any(existing_counts.values()):
                raise RuntimeError("target database is not empty and has no matching import record")

            store = SQLAlchemyStore(session)
            await _write_preview(store, preview)
            await session.flush()
            foreign_key_errors = tuple(await database.sqlite_integrity_errors(session))
            if foreign_key_errors:
                raise RuntimeError(
                    "imported data violates foreign keys: " + ", ".join(foreign_key_errors)
                )
            target_counts = await _target_counts(session)
            report = base_report.model_copy(
                update={
                    "target_counts": target_counts,
                    "foreign_key_errors": foreign_key_errors,
                }
            )
            session.add(
                ImportRunModel(
                    source_sha256=before_sha,
                    source_path=str(source),
                    imported_at=datetime.now(UTC),
                    report=report.model_dump(mode="json"),
                )
            )
        if _sha256_file(source) != before_sha:
            raise RuntimeError("source database changed during target commit")
        return report
    finally:
        await database.close()


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if exists is None:
        return 0
    return int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _rows(connection: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    if _table_count(connection, table) == 0:
        return []
    return [dict(row) for row in connection.execute(f'SELECT * FROM "{table}"')]


def _read_source(connection: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    return {table: _rows(connection, table) for table in _IMPORT_TABLES}


async def _write_preview(
    store: SQLAlchemyStore, preview: Mapping[str, list[dict[str, Any]]]
) -> None:
    for row in preview["users"]:
        await store.save_user(
            UserRecord(
                id=str(row["id"]),
                username=str(row["username"]),
                email=str(row["email"]),
                password_hash=str(row["password_hash"]),
                display_name=str(row.get("display_name") or ""),
                role=str(row.get("role") or "user"),
                status=str(row.get("status") or "active"),
                created_at=_datetime(row.get("created_at")),
                updated_at=_datetime(row.get("updated_at")),
            )
        )
    for row in preview["agents"]:
        await store.save_agent(
            AgentRecord(
                id=str(row["id"]),
                user_id=str(row["user_id"]),
                name=str(row.get("name") or ""),
                config=_json_object(row.get("config")),
                is_public=bool(row.get("is_public", False)),
                created_at=_datetime(row.get("created_at")),
                updated_at=_datetime(row.get("updated_at")),
            )
        )
    for row in preview["apikeys"]:
        await store.save_api_key(
            APIKeyRecord(
                id=str(row["id"]),
                user_id=str(row["user_id"]),
                name=str(row.get("name") or ""),
                key_hash=str(row["key_hash"]),
                key_prefix=str(row.get("key_prefix") or ""),
                type=str(row.get("type") or "agent"),
                created_at=_datetime(row.get("created_at")),
            )
        )
    key_agents: dict[str, list[str]] = {}
    for row in preview["apikey_agents"]:
        key_agents.setdefault(str(row["apikey_id"]), []).append(str(row["agent_id"]))
    for api_key_id, agent_ids in key_agents.items():
        await store.set_api_key_agents(api_key_id, agent_ids)
    for row in preview["sessions"]:
        updated_at = _datetime(row.get("updated_at"))
        await store.save_session(
            SessionRecord(
                user_id=str(row["user_id"]),
                agent_id=str(row["agent_id"]),
                key=str(row["session_key"]),
                channel=str(row.get("channel") or ""),
                account_id=str(row.get("account_id") or ""),
                chat_id=str(row.get("chat_id") or ""),
                project_id=str(row.get("project_id") or ""),
                messages=_json_list(row.get("messages")),
                title=str(row.get("title") or ""),
                message_count=int(row.get("message_count") or 0),
                chatter_user_id=str(row.get("chatter_user_id") or row["user_id"]),
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
    for row in preview["agent_files"]:
        await store.save_agent_file(
            AgentFileRecord(
                agent_id=str(row["agent_id"]),
                user_id=str(row.get("user_id") or ""),
                filename=str(row["filename"]),
                data=_bytes(row.get("content", "")),
                updated_at=_datetime(row.get("updated_at")),
            )
        )
    for row in preview["configs"]:
        await store.save_config(
            ConfigRecord(
                id=str(row["id"]),
                kind=str(row["kind"]),
                user_id=_config_user_id(row),
                agent_id=_config_agent_id(row),
                name=str(row["name"]),
                enabled=bool(row.get("enabled", True)),
                data=_json_object(row.get("data")),
                created_at=_datetime(row.get("created_at")),
                updated_at=_datetime(row.get("updated_at")),
            )
        )
    for row in preview["cron_jobs"]:
        await store.save_cron_job(
            CronJobRecord(
                id=str(row["id"]),
                user_id=str(row.get("user_id") or ""),
                agent_id=str(row["agent_id"]),
                name=str(row.get("name") or ""),
                type=str(row.get("type") or "cron"),
                schedule=str(row["schedule"]),
                message=str(row["message"]),
                channel=str(row.get("channel") or ""),
                chat_id=str(row.get("chat_id") or ""),
                account_id=str(row.get("account_id") or ""),
                timezone=str(row.get("timezone") or "UTC"),
                enabled=bool(row.get("enabled", True)),
                last_run=_optional_datetime(row.get("last_run")),
                next_run=_optional_datetime(row.get("next_run")),
                created_at=_datetime(row.get("created_at")),
            )
        )


async def _target_counts(session: AsyncSession) -> dict[str, int]:
    models = {
        "users": UserModel,
        "apikeys": APIKeyModel,
        "agents": AgentModel,
        "apikey_agents": APIKeyAgentModel,
        "sessions": SessionModel,
        "agent_files": AgentFileModel,
        "configs": ConfigModel,
        "cron_jobs": CronJobModel,
    }
    return {
        table: int(await session.scalar(select(func.count()).select_from(model)) or 0)
        for table, model in models.items()
    }


def _redact_configs(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    redacted_rows: list[dict[str, Any]] = []
    count = 0
    for row in rows:
        copied = dict(row)
        value, changes = _redact_value(_json_object(row.get("data")))
        copied["data"] = json.dumps(value, separators=(",", ":"))
        count += changes
        redacted_rows.append(copied)
    return redacted_rows, count


def _config_user_id(row: Mapping[str, Any]) -> str:
    if "user_id" in row:
        return str(row.get("user_id") or "")
    return str(row.get("scope_id") or "") if row.get("scope") == "user" else ""


def _config_agent_id(row: Mapping[str, Any]) -> str:
    if "agent_id" in row:
        return str(row.get("agent_id") or "")
    return str(row.get("scope_id") or "") if row.get("scope") == "agent" else ""


def _redact_value(value: Any) -> tuple[Any, int]:
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        count = 0
        for key, item in value.items():
            if key.lower() in _SECRET_KEYS and item not in (None, ""):
                output[key] = ""
                count += 1
            else:
                output[key], nested = _redact_value(item)
                count += nested
        return output, count
    if isinstance(value, list):
        output_list: list[Any] = []
        count = 0
        for item in value:
            redacted, nested = _redact_value(item)
            output_list.append(redacted)
            count += nested
        return output_list, count
    return value, 0


def _json_object(value: Any) -> dict[str, Any]:
    parsed = _json(value)
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def _json_list(value: Any) -> list[dict[str, Any]]:
    parsed = _json(value)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("expected a JSON array of objects")
    return parsed


def _json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _datetime(value: Any) -> datetime:
    parsed = _optional_datetime(value)
    return parsed or datetime.now(UTC)


def _optional_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(value, UTC)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


def _bytes(value: Any) -> bytes:
    if isinstance(value, bytes):
        return value
    return str(value).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_source(path: Path) -> Path:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    return source


def _sqlite_target_path(target_url: str) -> Path | None:
    prefix = "sqlite+aiosqlite:///"
    if not target_url.startswith(prefix):
        return None
    raw = target_url.removeprefix(prefix)
    if raw == ":memory:":
        return None
    return Path(raw).expanduser().resolve()

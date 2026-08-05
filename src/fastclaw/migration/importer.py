"""Read-only, idempotent import from a FastClaw Go SQLite database."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import anyio
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from fastclaw.providers import ChatMessage
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

_PROVIDER_FIELDS = frozenset({"apiBase", "apiType", "authType", "models"})
_MODEL_FIELDS = frozenset(
    {"id", "name", "reasoning", "input", "cost", "contextWindow", "maxTokens"}
)
_MODEL_COST_FIELDS = frozenset({"input", "output", "cacheRead", "cacheWrite"})
_SIMPLE_SETTING_FIELDS: dict[str, frozenset[str]] = {
    "agents.defaults": frozenset(
        {"model", "maxTokens", "temperature", "maxToolIterations", "thinking", "policy"}
    ),
    "sandbox": frozenset({"enabled", "image", "policy", "backend", "network", "idleTTLSec"}),
    "taskqueue": frozenset({"maxConcurrent", "taskTimeoutSec"}),
    "skills.install": frozenset({"nodeManager"}),
    "skillsLearner": frozenset({"enabled", "minToolCalls", "model"}),
    "heartbeat": frozenset({"intervalMinutes"}),
}


class ImportIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    table: str
    row_id: str
    reason: str


class ImportValidationError(RuntimeError):
    def __init__(self, issues: Iterable[ImportIssue]) -> None:
        self.issues = tuple(issues)
        detail = "; ".join(f"{item.table}:{item.row_id}: {item.reason}" for item in self.issues)
        super().__init__(f"source database failed migration preflight: {detail}")


class ImportReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_path: str
    source_sha256: str
    source_files: dict[str, str] = Field(default_factory=dict)
    dry_run: bool
    idempotent: bool = False
    source_counts: dict[str, int] = Field(default_factory=dict)
    target_counts: dict[str, int] = Field(default_factory=dict)
    skipped_web_sessions: int = 0
    redacted_secrets: int = 0
    channels_require_reconfiguration: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    quarantined_rows: tuple[ImportIssue, ...] = ()
    agent_file_sha256: dict[str, str] = Field(default_factory=dict)
    foreign_key_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Snapshot:
    sha256: str
    source_files: dict[str, str]
    source_counts: dict[str, int]
    skipped_web_sessions: int
    preview: dict[str, list[dict[str, Any]]]


async def import_go_database(
    *,
    source: Path,
    target_url: str,
    dry_run: bool = False,
    orphan_policy: Literal["reject", "quarantine"] = "reject",
) -> ImportReport:
    """Import a stable Go SQLite snapshot without modifying its source files."""

    source = await anyio.to_thread.run_sync(_validated_source, source)
    if _sqlite_target_path(target_url) == source:
        raise ValueError("source and target SQLite paths must be different")
    if orphan_policy not in {"reject", "quarantine"}:
        raise ValueError("orphan_policy must be 'reject' or 'quarantine'")

    snapshot = await anyio.to_thread.run_sync(_snapshot_source, source)
    preview = snapshot.preview
    warnings: list[str] = [
        "The Go runtime must be stopped before migration; source file stability was verified."
    ]

    sanitized_agents: list[dict[str, Any]] = []
    redacted_secrets = 0
    for row in preview["agents"]:
        copied = dict(row)
        original = _json_object(row.get("config"))
        copied["config"] = json.dumps(_sanitize_agent_config(original), separators=(",", ":"))
        redacted_secrets += _removed_nonempty_values(original, _json_object(copied["config"]))
        sanitized_agents.append(copied)
    preview["agents"] = sanitized_agents

    sanitized_configs, config_redactions, channel_ids, config_warnings = _sanitize_configs(
        preview["configs"]
    )
    preview["configs"] = sanitized_configs
    redacted_secrets += config_redactions
    warnings.extend(config_warnings)

    preview["sessions"] = [_normalize_session_row(row) for row in preview["sessions"]]
    quarantined = _apply_reference_policy(preview, orphan_policy)

    file_manifest = {
        f"{row['agent_id']}:{row.get('user_id', '')}:{row['filename']}": hashlib.sha256(
            _bytes(row.get("content", ""))
        ).hexdigest()
        for row in preview["agent_files"]
    }
    base_report = ImportReport(
        source_path=str(source),
        source_sha256=snapshot.sha256,
        source_files=snapshot.source_files,
        dry_run=dry_run,
        source_counts=snapshot.source_counts,
        skipped_web_sessions=snapshot.skipped_web_sessions,
        redacted_secrets=redacted_secrets,
        channels_require_reconfiguration=tuple(sorted(channel_ids)),
        warnings=tuple(dict.fromkeys(warnings)),
        quarantined_rows=quarantined,
        agent_file_sha256=file_manifest,
    )
    if dry_run:
        return base_report

    database = Database(target_url)
    try:
        await database.create_schema()
        async with database.session() as session:
            previous = await session.get(ImportRunModel, snapshot.sha256)
            if previous is not None:
                prior = ImportReport.model_validate(previous.report)
                return prior.model_copy(update={"idempotent": True})
            existing_counts = await _target_counts(session)
            if any(existing_counts.values()):
                raise RuntimeError("target database is not empty and has no matching import record")

            store = SQLAlchemyStore(session)
            try:
                await _write_preview(store, preview)
                await session.flush()
            except IntegrityError as exc:
                raise RuntimeError(
                    "target rejected the preflighted import; the transaction was rolled back"
                ) from exc
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
                    source_sha256=snapshot.sha256,
                    source_path=str(source),
                    imported_at=datetime.now(UTC),
                    report=report.model_dump(mode="json"),
                )
            )
        return report
    finally:
        await database.close()


def _snapshot_source(source: Path) -> _Snapshot:
    before = _source_manifest(source)
    with tempfile.TemporaryDirectory(prefix="fastclaw-import-") as temporary:
        staging = Path(temporary)
        for member in _source_members(source):
            shutil.copy2(member, staging / member.name)
        after = _source_manifest(source)
        if before != after:
            raise RuntimeError("source database files changed while the snapshot was staged")

        staged_db = staging / source.name
        snapshot_db = staging / "snapshot.db"
        staged_connection = sqlite3.connect(staged_db)
        snapshot_connection = sqlite3.connect(snapshot_db)
        try:
            before_version = int(staged_connection.execute("PRAGMA data_version").fetchone()[0])
            staged_connection.backup(snapshot_connection)
            after_version = int(staged_connection.execute("PRAGMA data_version").fetchone()[0])
            if before_version != after_version:
                raise RuntimeError("staged database changed during sqlite3_backup")
        finally:
            snapshot_connection.close()
            staged_connection.close()

        snapshot_sha = _sha256_file(snapshot_db)
        with _read_only_sqlite(snapshot_db) as connection:
            counts = {table: _table_count(connection, table) for table in _IMPORT_TABLES}
            skipped_web_sessions = _table_count(connection, "web_sessions")
            preview = _read_source(connection)

    return _Snapshot(
        sha256=snapshot_sha,
        source_files={name: fingerprint[1] for name, fingerprint in before.items()},
        source_counts=counts,
        skipped_web_sessions=skipped_web_sessions,
        preview=preview,
    )


def _source_members(source: Path) -> tuple[Path, ...]:
    candidates = (source, Path(f"{source}-wal"), Path(f"{source}-shm"))
    return tuple(candidate for candidate in candidates if candidate.exists())


def _source_manifest(source: Path) -> dict[str, tuple[int, str]]:
    return {
        member.name: (member.stat().st_size, _sha256_file(member))
        for member in _source_members(source)
    }


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


def _sanitize_configs(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int, set[str], list[str]]:
    output: list[dict[str, Any]] = []
    redactions = 0
    channel_ids: set[str] = set()
    warnings: list[str] = []
    for row in rows:
        copied = dict(row)
        data = _json_object(row.get("data"))
        kind = str(row.get("kind") or "")
        name = str(row.get("name") or "")
        if kind == "provider":
            sanitized = _sanitize_provider(data)
        elif kind == "channel":
            sanitized = _sanitize_channel(data)
            copied["credential_key"] = ""
            channel_ids.add(str(row.get("id") or name))
        elif kind == "setting":
            sanitized, known = _sanitize_setting(name, data)
            if not known:
                warnings.append(
                    f"config {row.get('id', name)} used unknown setting {name!r}; data cleared"
                )
        else:
            sanitized = {}
            warnings.append(
                f"config {row.get('id', name)} used unknown kind {kind!r}; data cleared"
            )
        redactions += _removed_nonempty_values(data, sanitized)
        copied["data"] = json.dumps(sanitized, separators=(",", ":"))
        output.append(copied)
    return output, redactions, channel_ids, warnings


def _sanitize_provider(data: Mapping[str, Any]) -> dict[str, Any]:
    output = _pick(data, _PROVIDER_FIELDS - {"models"})
    models = data.get("models")
    if isinstance(models, list):
        output["models"] = []
        for model in models:
            if not isinstance(model, dict):
                continue
            safe_model = _pick(model, _MODEL_FIELDS - {"cost"})
            if isinstance(model.get("cost"), dict):
                safe_model["cost"] = _pick(model["cost"], _MODEL_COST_FIELDS)
            output["models"].append(safe_model)
    return output


def _sanitize_channel(data: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    if "enabled" in data:
        output["enabled"] = bool(data["enabled"])
    accounts = data.get("accounts")
    if isinstance(accounts, dict):
        output["accounts"] = {str(account_id): {} for account_id in accounts}
    return output


def _sanitize_setting(name: str, data: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    if name in _SIMPLE_SETTING_FIELDS:
        fields = _SIMPLE_SETTING_FIELDS[name]
        return (_pick(data, fields) if fields else dict(data), True)
    if name == "objectstore":
        output = _pick(data, frozenset({"type", "accountId", "aliyunInternal"}))
        if isinstance(data.get("local"), dict):
            output["local"] = _pick(data["local"], frozenset({"root"}))
        if isinstance(data.get("s3"), dict):
            output["s3"] = _pick(
                data["s3"],
                frozenset({"endpoint", "region", "bucket", "prefix", "useSSL"}),
            )
        return output, True
    if name == "hooks":
        return _pick(data, frozenset({"enabled", "path", "port"})), True
    if name == "plugins":
        output = _pick(data, frozenset({"enabled", "paths"}))
        entries = data.get("entries")
        if isinstance(entries, dict):
            output["entries"] = {
                str(key): _pick(value, frozenset({"enabled"}))
                for key, value in entries.items()
                if isinstance(value, dict)
            }
        return output, True
    if name == "tools.providers":
        return _map_named_objects(data, frozenset({"endpoint"})), True
    if name == "tools.categories":
        return _map_named_objects(data, frozenset({"primary", "fallbacks", "autoFallback"})), True
    if name == "skills.entries":
        return _map_named_objects(data, frozenset({"enabled"})), True
    if name == "memory":
        output = {}
        if isinstance(data.get("autoPersist"), dict):
            output["autoPersist"] = _pick(
                data["autoPersist"], frozenset({"enabled", "everyNTurns", "model"})
            )
        if isinstance(data.get("fts"), dict):
            output["fts"] = _pick(data["fts"], frozenset({"enabled", "dbPath"}))
        return output, True
    if name == "privacy":
        output = {}
        if isinstance(data.get("piiScrubbing"), dict):
            output["piiScrubbing"] = _pick(data["piiScrubbing"], frozenset({"enabled"}))
        return output, True
    if name == "teams":
        return (
            _map_named_objects(data, frozenset({"agents", "defaultAgent", "groupBehavior"})),
            True,
        )
    if name == "bindings":
        # ConfigRecord data is object-only while the locked Go shape is a list.
        # Do not guess at a legacy wrapper that could contain credentials.
        return {}, True
    return {}, False


def _sanitize_agent_config(data: Mapping[str, Any]) -> dict[str, Any]:
    output = _pick(
        data,
        frozenset(
            {
                "model",
                "maxTokens",
                "temperature",
                "maxToolIterations",
                "workspace",
                "thinking",
                "policy",
            }
        ),
    )
    if isinstance(data.get("skills"), dict):
        output["skills"] = _pick(data["skills"], frozenset({"disabled", "alwaysLoad"}))
    if isinstance(data.get("providers"), dict):
        output["providers"] = {
            str(key): _sanitize_provider(value)
            for key, value in data["providers"].items()
            if isinstance(value, dict)
        }
    if isinstance(data.get("toolProviders"), dict):
        output["toolProviders"] = _map_named_objects(data["toolProviders"], frozenset({"endpoint"}))
    if isinstance(data.get("tools"), dict):
        output["tools"] = _map_named_objects(
            data["tools"], frozenset({"primary", "fallbacks", "autoFallback"})
        )
    servers = data.get("mcpServers")
    if isinstance(servers, dict):
        safe_servers: dict[str, Any] = {}
        for key, value in servers.items():
            if not isinstance(value, dict):
                continue
            safe = _pick(value, frozenset({"type", "command", "args"}))
            url = value.get("url")
            if isinstance(url, str) and not urlsplit(url).username and not urlsplit(url).password:
                safe["url"] = url
            safe_servers[str(key)] = safe
        output["mcpServers"] = safe_servers
    return output


def _map_named_objects(data: Mapping[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {
        str(key): _pick(value, fields) for key, value in data.items() if isinstance(value, dict)
    }


def _pick(data: Mapping[str, Any], fields: frozenset[str]) -> dict[str, Any]:
    return {key: data[key] for key in fields if key in data}


def _removed_nonempty_values(original: Any, sanitized: Any) -> int:
    if isinstance(original, dict):
        safe = sanitized if isinstance(sanitized, dict) else {}
        return sum(
            _removed_nonempty_values(value, safe[key])
            if key in safe
            else _nonempty_leaf_count(value)
            for key, value in original.items()
        )
    if isinstance(original, list):
        if not isinstance(sanitized, list):
            return _nonempty_leaf_count(original)
        return sum(
            _removed_nonempty_values(value, sanitized[index])
            if index < len(sanitized)
            else _nonempty_leaf_count(value)
            for index, value in enumerate(original)
        )
    return int(original not in (None, "") and original != sanitized)


def _nonempty_leaf_count(value: Any) -> int:
    if isinstance(value, dict):
        return sum(_nonempty_leaf_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_nonempty_leaf_count(item) for item in value)
    return int(value not in (None, ""))


def _normalize_session_row(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    messages = _json_list(row.get("messages"))
    copied["messages"] = json.dumps(
        [
            ChatMessage.model_validate(message).model_dump(
                by_alias=True,
                mode="json",
                exclude_defaults=True,
                exclude_none=True,
            )
            for message in messages
        ],
        separators=(",", ":"),
    )
    return copied


def _apply_reference_policy(
    preview: dict[str, list[dict[str, Any]]], policy: Literal["reject", "quarantine"]
) -> tuple[ImportIssue, ...]:
    issues = _reference_issues(preview)
    if issues and policy == "reject":
        raise ImportValidationError(issues)
    quarantined: list[ImportIssue] = []
    while issues:
        quarantined.extend(issues)
        rejected = {(issue.table, issue.row_id) for issue in issues}
        for table in _IMPORT_TABLES:
            preview[table] = [
                row for row in preview[table] if (table, _row_id(table, row)) not in rejected
            ]
        issues = _reference_issues(preview)
    return tuple(quarantined)


def _reference_issues(preview: Mapping[str, list[dict[str, Any]]]) -> tuple[ImportIssue, ...]:
    users = {str(row["id"]) for row in preview["users"]}
    agents = {str(row["id"]): str(row.get("user_id") or "") for row in preview["agents"]}
    api_keys = {str(row["id"]): str(row.get("user_id") or "") for row in preview["apikeys"]}
    issues: list[ImportIssue] = []

    def require(table: str, row: Mapping[str, Any], condition: bool, reason: str) -> None:
        if not condition:
            issues.append(ImportIssue(table=table, row_id=_row_id(table, row), reason=reason))

    for row in preview["agents"]:
        require("agents", row, str(row.get("user_id") or "") in users, "missing owner user")
    for row in preview["apikeys"]:
        require("apikeys", row, str(row.get("user_id") or "") in users, "missing owner user")
    for row in preview["apikey_agents"]:
        require("apikey_agents", row, str(row.get("apikey_id")) in api_keys, "missing API key")
        require("apikey_agents", row, str(row.get("agent_id")) in agents, "missing agent")
    for row in preview["sessions"]:
        user_id, agent_id = str(row.get("user_id") or ""), str(row.get("agent_id") or "")
        require("sessions", row, user_id in users, "missing user")
        require("sessions", row, agent_id in agents, "missing agent")
        require("sessions", row, agents.get(agent_id) == user_id, "agent belongs to another user")
    for row in preview["agent_files"]:
        require("agent_files", row, str(row.get("agent_id")) in agents, "missing agent")
    for row in preview["cron_jobs"]:
        require("cron_jobs", row, str(row.get("agent_id")) in agents, "missing agent")
    for row in preview["configs"]:
        scope, scope_id = _config_scope(row)
        if scope == "user":
            require("configs", row, scope_id in users, "missing scoped user")
        elif scope == "agent":
            require("configs", row, scope_id in agents, "missing scoped agent")

    seen: dict[tuple[str, str, str, str], str] = {}
    for row in preview["configs"]:
        scope, scope_id = _config_scope(row)
        key = (str(row.get("kind")), scope, scope_id, str(row.get("name")))
        if key in seen:
            issues.append(
                ImportIssue(
                    table="configs",
                    row_id=_row_id("configs", row),
                    reason=f"duplicate scope key also used by {seen[key]}",
                )
            )
        else:
            seen[key] = _row_id("configs", row)
    return tuple(dict.fromkeys(issues))


def _row_id(table: str, row: Mapping[str, Any]) -> str:
    if "id" in row:
        return str(row["id"])
    keys = {
        "apikey_agents": ("apikey_id", "agent_id"),
        "sessions": ("user_id", "agent_id", "session_key"),
        "agent_files": ("agent_id", "user_id", "filename"),
    }.get(table, ())
    return ":".join(str(row.get(key) or "") for key in keys)


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
        scope, scope_id = _config_scope(row)
        await store.save_config(
            ConfigRecord(
                id=str(row["id"]),
                kind=str(row["kind"]),
                scope=scope,
                scope_id=scope_id,
                user_id=scope_id if scope == "user" else "",
                agent_id=scope_id if scope == "agent" else "",
                name=str(row["name"]),
                enabled=bool(row.get("enabled", True)),
                credential_key="",
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


def _config_scope(row: Mapping[str, Any]) -> tuple[str, str]:
    scope = str(row.get("scope") or "")
    if scope in {"system", "user", "agent", "skill"}:
        return scope, str(row.get("scope_id") or "")
    if row.get("agent_id"):
        return "agent", str(row["agent_id"])
    if row.get("user_id"):
        return "user", str(row["user_id"])
    return "system", ""


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
        raise ValueError("Go database timestamps must use RFC3339, not numeric epochs")
    else:
        raw = str(value)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            # modernc.org/sqlite serializes Go time.Time columns in this exact form.
            parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S.%f %z UTC")
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

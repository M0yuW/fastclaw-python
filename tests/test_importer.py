from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from fastclaw.identity import hash_password, verify_password
from fastclaw.migration import import_go_database
from fastclaw.storage import Database, UnitOfWork


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _create_go_fixture(path: Path) -> None:
    password_hash = hash_password("legacy-password")
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE users (
            id TEXT PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT,
            display_name TEXT, role TEXT, status TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE web_sessions (sid TEXT PRIMARY KEY, user_id TEXT);
        CREATE TABLE apikeys (
            id TEXT PRIMARY KEY, user_id TEXT, name TEXT, key_hash TEXT,
            key_prefix TEXT, type TEXT, created_at TEXT
        );
        CREATE TABLE agents (
            id TEXT PRIMARY KEY, user_id TEXT, name TEXT, config TEXT,
            is_public INTEGER, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE apikey_agents (apikey_id TEXT, agent_id TEXT);
        CREATE TABLE sessions (
            user_id TEXT, agent_id TEXT, session_key TEXT, channel TEXT,
            account_id TEXT, chat_id TEXT, project_id TEXT, title TEXT,
            messages TEXT, message_count INTEGER, updated_at TEXT, chatter_user_id TEXT
        );
        CREATE TABLE agent_files (
            agent_id TEXT, user_id TEXT, filename TEXT, content BLOB, updated_at TEXT
        );
        CREATE TABLE configs (
            id TEXT PRIMARY KEY, kind TEXT, scope TEXT, scope_id TEXT,
            user_id TEXT, agent_id TEXT, name TEXT, enabled INTEGER, data TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE cron_jobs (
            id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT, name TEXT, type TEXT,
            schedule TEXT, message TEXT, channel TEXT, chat_id TEXT, account_id TEXT,
            timezone TEXT, enabled INTEGER, last_run TEXT, next_run TEXT, created_at TEXT
        );
        """
    )
    now = "2026-08-04T00:00:00Z"
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "user-1",
            "alice",
            "alice@example.test",
            password_hash,
            "Alice",
            "user",
            "active",
            now,
            now,
        ),
    )
    connection.execute("INSERT INTO web_sessions VALUES ('old-cookie', 'user-1')")
    connection.execute(
        "INSERT INTO apikeys VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("key-1", "user-1", "legacy", "legacy-sha256", "fc_old", "agent", now),
    )
    connection.execute(
        "INSERT INTO agents VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("agent-1", "user-1", "Finance", "{}", 1, now, now),
    )
    connection.execute("INSERT INTO apikey_agents VALUES ('key-1', 'agent-1')")
    connection.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "user-1",
            "agent-1",
            "session-1",
            "web",
            "",
            "chat-1",
            "project-1",
            "Fixture",
            '[{"role":"user","content":"hello"}]',
            1,
            now,
            "user-1",
        ),
    )
    connection.execute(
        "INSERT INTO agent_files VALUES (?, ?, ?, ?, ?)",
        ("agent-1", "user-1", "MEMORY.md", b"fixture memory", now),
    )
    connection.execute(
        "INSERT INTO configs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "config-1",
            "provider",
            "",
            "",
            "user-1",
            "agent-1",
            "primary",
            1,
            json.dumps(
                {
                    "model": "fixture",
                    "apiKey": "must-redact",
                    "nested": {"botToken": "redact"},
                }
            ),
            now,
            now,
        ),
    )
    connection.execute(
        "INSERT INTO cron_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "cron-1",
            "user-1",
            "agent-1",
            "daily",
            "cron",
            "0 9 * * *",
            "run",
            "web",
            "chat-1",
            "",
            "Asia/Shanghai",
            1,
            None,
            None,
            now,
        ),
    )
    connection.commit()
    connection.close()


@pytest.mark.asyncio
async def test_go_import_is_read_only_redacted_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "go.db"
    target = tmp_path / "python.db"
    _create_go_fixture(source)
    original_sha = _sha256(source)
    target_url = f"sqlite+aiosqlite:///{target}"

    dry_run = await import_go_database(source=source, target_url=target_url, dry_run=True)
    assert not target.exists()
    assert dry_run.source_counts["users"] == 1
    assert dry_run.skipped_web_sessions == 1
    assert dry_run.redacted_secrets == 2
    assert dry_run.agent_file_sha256
    assert _sha256(source) == original_sha

    report = await import_go_database(source=source, target_url=target_url)
    assert report.target_counts == report.source_counts
    assert report.foreign_key_errors == ()
    assert _sha256(source) == original_sha

    database = Database(target_url)
    try:
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            user = await store.get_user("user-1")
            assert user is not None
            assert verify_password("legacy-password", user.password_hash)
            assert await store.api_key_can_access_agent("key-1", "agent-1")
            session = await store.get_session("user-1", "agent-1", "session-1")
            assert session is not None
            assert session.project_id == "project-1"
            files = await store.list_agent_files("agent-1", "user-1")
            assert files[0].data == b"fixture memory"
            configs = await store.list_configs(
                kind="provider", user_id="user-1", agent_id="agent-1"
            )
            assert configs[0].data["apiKey"] == ""
            assert configs[0].data["nested"]["botToken"] == ""
    finally:
        await database.close()

    repeated = await import_go_database(source=source, target_url=target_url)
    assert repeated.idempotent
    assert repeated.target_counts == report.target_counts


@pytest.mark.asyncio
async def test_import_rejects_shared_source_and_target(tmp_path: Path) -> None:
    source = tmp_path / "shared.db"
    _create_go_fixture(source)

    with pytest.raises(ValueError, match="must be different"):
        await import_go_database(
            source=source,
            target_url=f"sqlite+aiosqlite:///{source}",
        )

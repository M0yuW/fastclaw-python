from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fastclaw.migration import AssetImportConflictError, import_assets
from fastclaw.storage import AgentRecord, Database, UnitOfWork, UserRecord


async def target_database(path: Path) -> str:
    url = f"sqlite+aiosqlite:///{path}"
    database = Database(url)
    await database.create_schema()
    now = datetime.now(UTC)
    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        await store.save_user(
            UserRecord(
                id="user-1",
                username="fixture",
                email="fixture@example.test",
                password_hash="unused",
                created_at=now,
                updated_at=now,
            )
        )
        await store.save_agent(
            AgentRecord(
                id="valid-agent",
                user_id="user-1",
                name="Valid",
                created_at=now,
                updated_at=now,
            )
        )
    await database.close()
    return url


def source_assets(root: Path) -> None:
    for agent_id in ("valid-agent", "stale-agent"):
        agent = root / "agents" / agent_id / "agent"
        workspace = root / "workspaces" / agent_id
        agent.mkdir(parents=True)
        workspace.mkdir(parents=True)
        (agent / "SOUL.md").write_text(agent_id, encoding="utf-8")
        (workspace / "notes.txt").write_text(agent_id, encoding="utf-8")
    skill = root / "skills" / "fixture"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
    (skill / ".env").write_text("SECRET=leak", encoding="utf-8")
    (skill / "cache").mkdir()
    (skill / "cache" / "response.json").write_text("cached", encoding="utf-8")


async def test_asset_import_is_dry_run_idempotent_and_agent_scoped(tmp_path: Path) -> None:
    source = tmp_path / "go"
    target = tmp_path / "python"
    source_assets(source)
    database_url = await target_database(tmp_path / "target.db")

    preview = await import_assets(
        source_root=source,
        target_root=target,
        database_url=database_url,
        dry_run=True,
    )

    assert not target.exists()
    assert preview.valid_agent_count == 1
    assert "agents/stale-agent" in preview.excluded
    assert "workspaces/stale-agent" in preview.excluded
    assert "skills/fixture/.env" in preview.excluded
    assert "skills/fixture/cache" in preview.excluded

    imported = await import_assets(
        source_root=source,
        target_root=target,
        database_url=database_url,
    )
    repeated = await import_assets(
        source_root=source,
        target_root=target,
        database_url=database_url,
    )

    assert len(imported.copied) == 3
    assert repeated.copied == ()
    assert repeated.unchanged == imported.copied
    assert (target / "agents/valid-agent/agent/SOUL.md").read_text() == "valid-agent"
    assert not (target / "agents/stale-agent").exists()
    assert not (target / "skills/fixture/.env").exists()


async def test_asset_import_reports_conflict_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "go"
    target = tmp_path / "python"
    source_assets(source)
    database_url = await target_database(tmp_path / "target.db")
    conflict = target / "agents/valid-agent/agent/SOUL.md"
    conflict.parent.mkdir(parents=True)
    conflict.write_text("local change", encoding="utf-8")

    with pytest.raises(AssetImportConflictError) as captured:
        await import_assets(
            source_root=source,
            target_root=target,
            database_url=database_url,
        )

    assert captured.value.report.conflicts == ("agents/valid-agent/agent/SOUL.md",)
    assert not (target / "workspaces/valid-agent/notes.txt").exists()


async def test_asset_import_rejects_nested_source_and_target(tmp_path: Path) -> None:
    source = tmp_path / "go"
    source_assets(source)
    database_url = await target_database(tmp_path / "target.db")

    with pytest.raises(ValueError, match="independent"):
        await import_assets(
            source_root=source,
            target_root=source / "python-target",
            database_url=database_url,
            dry_run=True,
        )

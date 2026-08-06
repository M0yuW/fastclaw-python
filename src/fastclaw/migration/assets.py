"""Idempotent, allow-scoped import of Go Agent, workspace, and Skill assets."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import anyio
from pydantic import BaseModel, ConfigDict, Field

from fastclaw.storage import Database, UnitOfWork

_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "cache",
        "caches",
        "debug",
        "logs",
        "node_modules",
    }
)
_EXCLUDED_FILENAMES = frozenset(
    {".env", "credentials.json", "runtime-benchmark-tenant.json", "secrets.json"}
)
_EXCLUDED_SUFFIXES = (".db", ".db-shm", ".db-wal", ".key", ".log", ".pem", ".pyc")


class AssetImportReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_root: str
    target_root: str
    dry_run: bool
    valid_agent_count: int
    source_sha256: dict[str, str] = Field(default_factory=dict)
    copied: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()
    excluded: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class AssetImportConflictError(RuntimeError):
    def __init__(self, report: AssetImportReport) -> None:
        self.report = report
        super().__init__(f"asset import found {len(report.conflicts)} conflicting target file(s)")


@dataclass(frozen=True, slots=True)
class _Asset:
    source: Path
    target: Path
    relative: str
    sha256: str


async def import_assets(
    *,
    source_root: Path,
    target_root: Path,
    database_url: str,
    dry_run: bool = False,
) -> AssetImportReport:
    source_root, target_root = await anyio.to_thread.run_sync(
        _resolve_roots, source_root, target_root
    )
    if (
        source_root == target_root
        or source_root.is_relative_to(target_root)
        or target_root.is_relative_to(source_root)
    ):
        raise ValueError("source and target asset roots must be independent")
    database = Database(database_url)
    try:
        async with UnitOfWork(database) as unit:
            store = unit.require_store()
            users = await store.list_users()
            valid_agents = {
                agent.id for user in users for agent in await store.list_agents(user.id)
            }
    finally:
        await database.close()

    assets, excluded, warnings = await anyio.to_thread.run_sync(
        _collect_assets, source_root, target_root, valid_agents
    )
    copied: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for asset in assets:
        if not asset.target.exists():
            copied.append(asset.relative)
        elif asset.target.is_file() and _sha256(asset.target) == asset.sha256:
            unchanged.append(asset.relative)
        else:
            conflicts.append(asset.relative)
    base = AssetImportReport(
        source_root=str(source_root),
        target_root=str(target_root),
        dry_run=dry_run,
        valid_agent_count=len(valid_agents),
        source_sha256={asset.relative: asset.sha256 for asset in assets},
        copied=tuple(copied),
        unchanged=tuple(unchanged),
        excluded=tuple(sorted(excluded)),
        conflicts=tuple(conflicts),
        warnings=tuple(warnings),
    )
    if conflicts:
        raise AssetImportConflictError(base)
    if dry_run:
        return base
    unchanged_set = set(unchanged)
    for asset in assets:
        if asset.relative not in unchanged_set:
            await anyio.to_thread.run_sync(_copy_atomic, asset)
    return base


def _resolve_roots(source_root: Path, target_root: Path) -> tuple[Path, Path]:
    return source_root.expanduser().resolve(strict=True), target_root.expanduser().resolve()


def _collect_assets(
    source_root: Path, target_root: Path, valid_agents: set[str]
) -> tuple[list[_Asset], set[str], list[str]]:
    assets: list[_Asset] = []
    excluded: set[str] = set()
    warnings: list[str] = []
    for collection in ("agents", "workspaces"):
        base = source_root / collection
        if not base.is_dir():
            warnings.append(f"source has no {collection} directory")
            continue
        for child in sorted(base.iterdir()):
            relative_child = child.relative_to(source_root).as_posix()
            if child.name not in valid_agents:
                excluded.add(relative_child)
                continue
            _walk(child, source_root, target_root, assets, excluded, warnings)
    skills = source_root / "skills"
    if skills.is_dir():
        _walk(skills, source_root, target_root, assets, excluded, warnings)
    else:
        warnings.append("source has no skills directory")
    return sorted(assets, key=lambda item: item.relative), excluded, warnings


def _walk(
    root: Path,
    source_root: Path,
    target_root: Path,
    assets: list[_Asset],
    excluded: set[str],
    warnings: list[str],
) -> None:
    for directory, names, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(directory)
        retained: list[str] = []
        for name in sorted(names):
            candidate = current / name
            relative = candidate.relative_to(source_root).as_posix()
            if candidate.is_symlink() or name in _EXCLUDED_DIRECTORIES:
                excluded.add(relative)
                if candidate.is_symlink():
                    warnings.append(f"symbolic link excluded: {relative}")
                continue
            retained.append(name)
        names[:] = retained
        for name in sorted(filenames):
            source = current / name
            relative = source.relative_to(source_root).as_posix()
            if source.is_symlink() or _excluded_file(source):
                excluded.add(relative)
                if source.is_symlink():
                    warnings.append(f"symbolic link excluded: {relative}")
                continue
            assets.append(
                _Asset(
                    source=source,
                    target=target_root / relative,
                    relative=relative,
                    sha256=_sha256(source),
                )
            )


def _excluded_file(path: Path) -> bool:
    name = path.name
    return (
        name in _EXCLUDED_FILENAMES or name.startswith(".env.") or name.endswith(_EXCLUDED_SUFFIXES)
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_atomic(asset: _Asset) -> None:
    asset.target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f".{asset.target.name}.", dir=asset.target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        shutil.copy2(asset.source, temporary)
        if _sha256(temporary) != asset.sha256:
            raise RuntimeError(f"asset changed while copying: {asset.relative}")
        temporary.replace(asset.target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

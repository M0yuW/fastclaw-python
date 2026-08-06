#!/usr/bin/env python3
"""Fail closed when release artifacts omit runtime assets or include local caches."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

_REQUIRED_WHEEL_SUFFIXES = {
    "fastclaw/alembic.ini",
    "fastclaw/bundled_plugins/finance-tools/LICENSE",
    "fastclaw/bundled_plugins/finance-tools/plugin.json",
    "fastclaw/bundled_plugins/finance-tools/plugin.py",
    "fastclaw/cutover.py",
    "fastclaw/migrations/versions/20260805_01_initial_schema.py",
    "fastclaw/py.typed",
}
_FORBIDDEN_SDIST_PARTS = {
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "node_modules",
    "out",
    "playwright-report",
    "test-results",
}
_MAX_WHEEL_BYTES = 2 * 1024 * 1024
_MAX_SDIST_BYTES = 10 * 1024 * 1024


def verify_distribution(directory: Path) -> tuple[int, int]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError("distribution directory must contain exactly one wheel and one sdist")
    wheel, sdist = wheels[0], sdists[0]
    if wheel.stat().st_size > _MAX_WHEEL_BYTES:
        raise RuntimeError("wheel exceeds the 2 MiB release limit")
    if sdist.stat().st_size > _MAX_SDIST_BYTES:
        raise RuntimeError("sdist exceeds the 10 MiB release limit")

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    missing = sorted(
        suffix
        for suffix in _REQUIRED_WHEEL_SUFFIXES
        if not any(name.endswith(suffix) for name in wheel_names)
    )
    if missing:
        raise RuntimeError(f"wheel is missing required runtime files: {missing}")

    with tarfile.open(sdist, "r:gz") as archive:
        sdist_names = archive.getnames()
    forbidden = sorted(
        name for name in sdist_names if _FORBIDDEN_SDIST_PARTS.intersection(Path(name).parts)
    )
    if forbidden:
        raise RuntimeError(f"sdist contains generated or cached files: {forbidden[:10]}")
    return len(wheel_names), len(sdist_names)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    wheel_count, sdist_count = verify_distribution(args.directory)
    print(f"Distribution verified: {wheel_count} wheel files + {sdist_count} sdist files")


if __name__ == "__main__":
    main()

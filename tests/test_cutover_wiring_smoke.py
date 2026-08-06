from __future__ import annotations

import runpy
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SCRIPT_GLOBALS = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "cutover_wiring_smoke.py")
)
validate_disposable_database = cast(
    Callable[..., Path], SCRIPT_GLOBALS["_validate_disposable_database"]
)
LOCKED_LIVE_DATABASES = cast(set[Path], SCRIPT_GLOBALS["_LOCKED_LIVE_DATABASES"])


def test_cutover_wiring_smoke_requires_explicit_disposable_acknowledgement(
    tmp_path: Path,
) -> None:
    database = tmp_path / "copy.db"
    database.touch()

    with pytest.raises(RuntimeError, match="acknowledge-disposable-copy"):
        validate_disposable_database(database, acknowledge=False)

    assert validate_disposable_database(database, acknowledge=True) == database.resolve()


@pytest.mark.parametrize("database", sorted(LOCKED_LIVE_DATABASES))
def test_cutover_wiring_smoke_rejects_locked_live_databases(database: Path) -> None:
    with pytest.raises(RuntimeError, match="locked Go or Python live database"):
        validate_disposable_database(database, acknowledge=True)

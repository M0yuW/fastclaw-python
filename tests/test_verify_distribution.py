from __future__ import annotations

import io
import runpy
import tarfile
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

SCRIPT_GLOBALS = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "verify_distribution.py")
)
verify_distribution = cast(Callable[[Path], tuple[int, int]], SCRIPT_GLOBALS["verify_distribution"])
REQUIRED = cast(set[str], SCRIPT_GLOBALS["_REQUIRED_WHEEL_SUFFIXES"])
REQUIRED_SDIST = cast(set[str], SCRIPT_GLOBALS["_REQUIRED_SDIST_SUFFIXES"])


def create_artifacts(root: Path, *, forbidden: str = "") -> None:
    with zipfile.ZipFile(root / "fastclaw-0.1.0-py3-none-any.whl", "w") as archive:
        for name in REQUIRED:
            archive.writestr(name, b"fixture")
    with tarfile.open(root / "fastclaw-0.1.0.tar.gz", "w:gz") as archive:
        names = ["fastclaw-0.1.0/README.md"] + [
            f"fastclaw-0.1.0/{suffix}" for suffix in REQUIRED_SDIST
        ]
        if forbidden:
            names.append(f"fastclaw-0.1.0/{forbidden}/artifact")
        for name in names:
            payload = b"fixture"
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def test_distribution_contract_accepts_required_runtime_files(tmp_path: Path) -> None:
    create_artifacts(tmp_path)

    assert verify_distribution(tmp_path) == (len(REQUIRED), 1 + len(REQUIRED_SDIST))


def test_distribution_contract_rejects_missing_verification_script(tmp_path: Path) -> None:
    create_artifacts(tmp_path)
    sdist = tmp_path / "fastclaw-0.1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        payload = b"fixture"
        info = tarfile.TarInfo("fastclaw-0.1.0/README.md")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="required verification files"):
        verify_distribution(tmp_path)


@pytest.mark.parametrize("forbidden", ["web/node_modules", "web/.next", "web/out"])
def test_distribution_contract_rejects_generated_web_files(tmp_path: Path, forbidden: str) -> None:
    create_artifacts(tmp_path, forbidden=forbidden)

    with pytest.raises(RuntimeError, match="generated or cached"):
        verify_distribution(tmp_path)

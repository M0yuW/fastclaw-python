from __future__ import annotations

import json
import re
import runpy
from pathlib import Path
from typing import Any, Protocol, cast

import pytest


class OverlayVerifier(Protocol):
    def __call__(self, modified_files: dict[str, Any], *, web_root: Path) -> None: ...


SCRIPT_GLOBALS = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts" / "verify_web_snapshot.py")
)
OVERLAY_MANIFEST = cast(Path, SCRIPT_GLOBALS["OVERLAY_MANIFEST"])
ROOT = cast(Path, SCRIPT_GLOBALS["ROOT"])
verify_overlay_hashes = cast(OverlayVerifier, SCRIPT_GLOBALS["verify_overlay_hashes"])

OVERLAY_PATHS = {
    "package.json",
    "pnpm-lock.yaml",
    "src/app/agents/[id]/chat/page.tsx",
    "src/app/agents/page.tsx",
    "src/lib/api.ts",
    "src/lib/chat-stream.ts",
}


def copy_overlays(tmp_path: Path) -> tuple[dict[str, object], Path]:
    modified_files: dict[str, object] = json.loads(OVERLAY_MANIFEST.read_text())["modifiedFiles"]
    assert set(modified_files) == OVERLAY_PATHS

    web_root = tmp_path / "web"
    for name in modified_files:
        target = web_root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / "web" / name).read_bytes())
    return modified_files, web_root


def test_overlay_hashes_match_manifest(tmp_path: Path) -> None:
    modified_files, web_root = copy_overlays(tmp_path)

    verify_overlay_hashes(modified_files, web_root=web_root)


@pytest.mark.parametrize("name", sorted(OVERLAY_PATHS))
def test_overlay_hash_change_is_rejected(tmp_path: Path, name: str) -> None:
    modified_files, web_root = copy_overlays(tmp_path)
    with (web_root / name).open("ab") as overlay:
        overlay.write(b"\n")

    with pytest.raises(RuntimeError, match=re.escape(name)):
        verify_overlay_hashes(modified_files, web_root=web_root)

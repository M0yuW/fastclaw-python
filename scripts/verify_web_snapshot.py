"""Generate or verify the attributed Go 792417b Web snapshot manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

REFERENCE_COMMIT = "792417b86b5c12af1b99364865217a74f4d52f38"
ADDED_FILES = {"LICENSE", "SOURCE.md"}
MISSING_FILES = {"next-env.d.ts"}
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "tests" / "fixtures" / "web-snapshot-792417b.json"
OVERLAY_MANIFEST = ROOT / "tests" / "fixtures" / "web-python-overlays.json"


def git(*arguments: str, cwd: Path = ROOT) -> bytes:
    return subprocess.run(
        ("git", *arguments),
        cwd=cwd,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_overlay_hashes(modified_files: dict[str, Any], *, web_root: Path = ROOT / "web") -> None:
    expected_keys = {"reason", "sha256"}
    invalid = [
        name
        for name, details in modified_files.items()
        if not isinstance(details, dict)
        or set(details) != expected_keys
        or not isinstance(details["reason"], str)
        or not details["reason"]
        or not isinstance(details["sha256"], str)
        or len(details["sha256"]) != 64
        or any(character not in "0123456789abcdef" for character in details["sha256"])
    ]
    if invalid:
        raise RuntimeError(f"Web overlay entries must contain reason and SHA-256: {invalid}")

    mismatches = [
        name
        for name, details in modified_files.items()
        if sha256((web_root / name).read_bytes()) != details["sha256"]
    ]
    if mismatches:
        raise RuntimeError(f"Web overlay hashes differ: {mismatches}")


def generate(reference_repo: Path, output: Path) -> None:
    names = (
        git(
            "ls-tree",
            "-r",
            "--name-only",
            REFERENCE_COMMIT,
            "--",
            "web",
            cwd=reference_repo,
        )
        .decode()
        .splitlines()
    )
    relative_names = {name.removeprefix("web/") for name in names}
    if len(relative_names) != 91:
        raise RuntimeError(f"expected 91 reference files, found {len(relative_names)}")
    if not MISSING_FILES <= relative_names:
        raise RuntimeError("reference snapshot does not contain next-env.d.ts")
    common = sorted(relative_names - MISSING_FILES)
    hashes = {
        name: sha256(git("show", f"{REFERENCE_COMMIT}:web/{name}", cwd=reference_repo))
        for name in common
    }
    manifest: dict[str, Any] = {
        "referenceCommit": REFERENCE_COMMIT,
        "referenceTrackedFiles": len(relative_names),
        "commonFiles": hashes,
        "addedFiles": sorted(ADDED_FILES),
        "missingFiles": sorted(MISSING_FILES),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


def verify(manifest_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    overlay = json.loads(OVERLAY_MANIFEST.read_text())
    common: dict[str, str] = manifest["commonFiles"]
    if manifest["referenceCommit"] != REFERENCE_COMMIT:
        raise RuntimeError("snapshot manifest references the wrong Go commit")
    if len(common) != 90 or manifest["referenceTrackedFiles"] != 91:
        raise RuntimeError("snapshot manifest must describe 90 of 91 reference files")
    if set(manifest["addedFiles"]) != ADDED_FILES:
        raise RuntimeError("snapshot added-file contract changed")
    if set(manifest["missingFiles"]) != MISSING_FILES:
        raise RuntimeError("snapshot missing-file contract changed")
    if overlay["referenceCommit"] != REFERENCE_COMMIT:
        raise RuntimeError("Web overlay references the wrong Go commit")
    modified_files: dict[str, Any] = overlay["modifiedFiles"]
    modified = set(modified_files)
    additions = set(overlay["addedFiles"])
    if not modified <= set(common):
        raise RuntimeError(f"Web overlay modifies unknown files: {sorted(modified - set(common))}")
    if additions & (set(common) | ADDED_FILES):
        raise RuntimeError("Web overlay additions collide with snapshot files")

    tracked = {name.removeprefix("web/") for name in git("ls-files", "web").decode().splitlines()}
    expected = set(common) | ADDED_FILES | additions
    unexpected = sorted(tracked - expected)
    missing = sorted(expected - tracked)
    if unexpected or missing:
        raise RuntimeError(
            f"web snapshot file set differs: unexpected={unexpected}, missing={missing}"
        )
    mismatches = [
        name
        for name, expected_hash in common.items()
        if name not in modified
        if sha256((ROOT / "web" / name).read_bytes()) != expected_hash
    ]
    if mismatches:
        raise RuntimeError(f"web snapshot hashes differ: {mismatches}")
    verify_overlay_hashes(modified_files)
    print(
        "Web snapshot verified: "
        f"{len(common) - len(modified)} unchanged + "
        f"{len(modified)} declared overlays + "
        f"{len(ADDED_FILES) + len(additions)} attributed additions"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--generate-from", type=Path)
    arguments = parser.parse_args()
    if arguments.generate_from is not None:
        generate(arguments.generate_from.resolve(), arguments.manifest.resolve())
    verify(arguments.manifest.resolve())


if __name__ == "__main__":
    main()

"""Skill discovery and explicitly prepared isolated Python environments."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import venv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio


class SkillError(RuntimeError):
    pass


class SkillNotFoundError(SkillError):
    pass


class SkillNotPreparedError(SkillError):
    pass


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    description: str
    root: Path
    instructions: str
    requirements_hash: str
    environment_names: tuple[str, ...]


def _parse_frontmatter(document: str, path: Path) -> tuple[dict[str, Any], str]:
    lines = document.splitlines()
    if not lines or lines[0].strip() != "---":
        raise SkillError(f"{path} has no YAML frontmatter")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise SkillError(f"{path} has unterminated YAML frontmatter") from exc
    metadata: dict[str, Any] = {}
    env_names: list[str] = []
    in_env = False
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "env:":
            in_env = True
            continue
        if in_env:
            match = re.match(r"^-\s+name:\s*(.+)$", stripped)
            if match:
                env_names.append(match.group(1).strip().strip("\"'"))
                continue
            if not line.startswith((" ", "\t", "-")):
                in_env = False
        if not in_env and ":" in stripped:
            key, value = stripped.split(":", 1)
            metadata[key] = value.strip().strip("\"'")
    metadata["environment_names"] = tuple(env_names)
    return metadata, "\n".join(lines[end + 1 :]).strip()


class SkillCatalog:
    def __init__(self, skills_root: Path, environments_root: Path | None = None) -> None:
        self.skills_root = skills_root.expanduser().resolve()
        self.environments_root = (
            environments_root.expanduser().resolve()
            if environments_root is not None
            else self.skills_root.parent / "skill-envs"
        )
        self._skills: dict[str, Skill] = {}
        self._prepare_locks: dict[Path, asyncio.Lock] = {}

    @property
    def skills(self) -> tuple[Skill, ...]:
        return tuple(self._skills[name] for name in sorted(self._skills))

    def discover(self) -> tuple[Skill, ...]:
        discovered: dict[str, Skill] = {}
        if not self.skills_root.is_dir():
            self._skills = {}
            return ()
        for skill_file in sorted(self.skills_root.glob("*/SKILL.md")):
            root = skill_file.parent.resolve()
            if not root.is_relative_to(self.skills_root):
                continue
            document = skill_file.read_text(encoding="utf-8")
            metadata, body = _parse_frontmatter(document, skill_file)
            name = str(metadata.get("name") or "").strip()
            if not name:
                raise SkillError(f"{skill_file} has no skill name")
            if name in discovered:
                raise SkillError(f"duplicate skill name {name!r}")
            requirements = root / "requirements.txt"
            requirements_bytes = requirements.read_bytes() if requirements.is_file() else b""
            digest = hashlib.sha256(
                f"python={current_python_tag()}\n".encode() + requirements_bytes
            ).hexdigest()[:16]
            discovered[name] = Skill(
                name=name,
                description=str(metadata.get("description") or ""),
                root=root,
                instructions=body,
                requirements_hash=digest,
                environment_names=tuple(metadata["environment_names"]),
            )
        self._skills = discovered
        return self.skills

    def require(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            raise SkillNotFoundError(f"required skill {name!r} is not installed")
        return skill

    def environment_path(self, skill: Skill) -> Path:
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", skill.name).strip("-")
        return self.environments_root / f"{safe_name}-{skill.requirements_hash}"

    def interpreter(self, skill: Skill) -> Path:
        directory = self.environment_path(skill)
        executable = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        marker = directory / "fastclaw-skill.json"
        if not executable.is_file() or not marker.is_file():
            raise SkillNotPreparedError(
                f"skill {skill.name!r} is not prepared; run `fastclaw skills prepare {skill.name}`"
            )
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillNotPreparedError(
                f"skill {skill.name!r} environment marker is invalid"
            ) from exc
        if payload != {"name": skill.name, "requirementsHash": skill.requirements_hash}:
            raise SkillNotPreparedError(f"skill {skill.name!r} environment is stale")
        return executable.resolve(strict=True)

    def is_prepared(self, skill: Skill) -> bool:
        try:
            self.interpreter(skill)
        except SkillNotPreparedError:
            return False
        return True

    async def prepare(self, skill: Skill) -> Path:
        destination = self.environment_path(skill)
        lock = self._prepare_locks.setdefault(destination, asyncio.Lock())
        async with lock:
            if self.is_prepared(skill):
                return destination
            self.environments_root.mkdir(parents=True, exist_ok=True)
            await anyio.to_thread.run_sync(_clear_environment, destination)
            try:
                requirements = skill.root / "requirements.txt"
                has_requirements = requirements.is_file() and bool(
                    requirements.read_text(encoding="utf-8").strip()
                )
                await anyio.to_thread.run_sync(
                    lambda: venv.EnvBuilder(
                        with_pip=has_requirements,
                        clear=True,
                        symlinks=os.name != "nt",
                    ).create(destination)
                )
                interpreter = destination / (
                    "Scripts/python.exe" if os.name == "nt" else "bin/python"
                )
                if has_requirements:
                    process = await asyncio.create_subprocess_exec(
                        str(interpreter),
                        "-m",
                        "pip",
                        "install",
                        "--requirement",
                        str(requirements),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await process.communicate()
                    if process.returncode != 0:
                        reason = stderr.decode(errors="replace")[-2000:]
                        raise SkillError(
                            f"dependency installation failed for {skill.name}: {reason}"
                        )
                    del stdout
                marker = destination / "fastclaw-skill.json"
                marker.write_text(
                    json.dumps(
                        {"name": skill.name, "requirementsHash": skill.requirements_hash},
                        sort_keys=True,
                    ),
                    encoding="utf-8",
                )
                return destination
            except BaseException:
                await anyio.to_thread.run_sync(_clear_environment, destination)
                raise

    @staticmethod
    def prompt(skill: Skill, *, source_root: Path, target_root: Path) -> str:
        content = skill.instructions.replace(str(source_root), str(target_root))
        return f"## Skill: {skill.name}\n\nSkill root: {skill.root}\n\n{content}"

    def trusted_environment(self, skill: Skill) -> dict[str, str]:
        data_root = self.skills_root.parent
        environment = {
            "FASTCLAW_DATA_ROOT": str(data_root),
            "HOME": str(data_root),
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
        }
        for name in skill.environment_names:
            value = os.environ.get(name)
            if value:
                environment[name] = value
        return environment


def current_python_tag() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _clear_environment(destination: Path) -> None:
    if destination.exists():
        shutil.rmtree(destination)

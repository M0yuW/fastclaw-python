"""Command-level execution policy for explicitly prepared Skill scripts."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import anyio

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.skills import Skill, SkillCatalog, SkillError
from fastclaw.tools.base import ToolResult


class SkillScriptTool:
    def __init__(
        self,
        catalog: SkillCatalog,
        skills: tuple[Skill, ...],
        *,
        forbidden_roots: tuple[Path, ...] = (),
        max_output_bytes: int = 1_000_000,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        self._catalog = catalog
        self._skills = {skill.name: skill for skill in skills}
        self._forbidden_roots = tuple(
            str(root.expanduser().resolve()).rstrip(os.sep) for root in forbidden_roots
        )
        self._max_output_bytes = max_output_bytes
        self._termination_grace_seconds = termination_grace_seconds
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="exec",
                description=(
                    "Run one Python script from an explicitly prepared, always-loaded Skill. "
                    "Shell commands, inline code, modules, stdin scripts, and arbitrary paths "
                    "are denied."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "skill": {"type": "string"},
                        "argv": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["argv"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        argv = arguments.get("argv")
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise ValueError("argv must be a non-empty array of strings")
        skill = self._select_skill(str(arguments.get("skill") or ""))
        script_value, script_arguments = self._parse_argv(argv)
        if any(root and root in value for root in self._forbidden_roots for value in argv):
            raise ValueError("legacy Runtime paths are denied")
        script = self._resolve_script(skill, script_value)
        interpreter = self._catalog.interpreter(skill)
        environment = {
            "PATH": str(interpreter.parent),
            **self._catalog.trusted_environment(skill),
        }
        if skill.name == "match-data-toolkit":
            environment["WC_LEDGER"] = str(
                self._catalog.skills_root.parent
                / "workspaces"
                / context.agent_id
                / "worldcup"
                / "ledger.json"
            )
        process = await asyncio.create_subprocess_exec(
            str(interpreter),
            str(script),
            *script_arguments,
            cwd=skill.root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        output = bytearray()
        output_lock = asyncio.Lock()
        exceeded = asyncio.Event()

        async def read(stream: asyncio.StreamReader) -> None:
            while chunk := await stream.read(64 * 1024):
                async with output_lock:
                    remaining = self._max_output_bytes - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        exceeded.set()

        readers = [
            asyncio.create_task(read(process.stdout)),
            asyncio.create_task(read(process.stderr)),
        ]
        waiter = asyncio.create_task(process.wait())
        limiter = asyncio.create_task(exceeded.wait())
        truncated = False
        try:
            done, _ = await asyncio.wait({waiter, limiter}, return_when=asyncio.FIRST_COMPLETED)
            if limiter in done and exceeded.is_set():
                truncated = True
                await self._terminate(process, waiter)
            else:
                limiter.cancel()
                if self._process_group_exists(process.pid):
                    await self._terminate(process, waiter)
            await asyncio.gather(*readers)
            await waiter
        except BaseException:
            with anyio.CancelScope(shield=True):
                await self._terminate(process, waiter)
                for task in readers:
                    task.cancel()
                limiter.cancel()
                await asyncio.gather(*readers, limiter, return_exceptions=True)
            raise
        finally:
            if not limiter.done():
                limiter.cancel()
            await asyncio.gather(limiter, return_exceptions=True)
        return ToolResult(
            content=bytes(output).decode(errors="replace"),
            is_error=process.returncode != 0,
            metadata={"exitCode": process.returncode, "truncated": truncated},
        )

    def _select_skill(self, name: str) -> Skill:
        if name:
            skill = self._skills.get(name)
            if skill is None:
                raise SkillError("requested Skill is not allowed for this Agent")
            return skill
        if len(self._skills) != 1:
            raise SkillError("skill must be specified when multiple Skills are loaded")
        return next(iter(self._skills.values()))

    @staticmethod
    def _parse_argv(argv: list[str]) -> tuple[str, list[str]]:
        if argv[0] in {"python", "python3"}:
            if len(argv) < 2:
                raise ValueError("a Skill script is required")
            script = argv[1]
            arguments = argv[2:]
        else:
            script = argv[0]
            arguments = argv[1:]
        if script in {"-", "-c", "-m"} or script.startswith(("-c=", "-m=")):
            raise ValueError("inline, module, and stdin Python execution is denied")
        return script, arguments

    @staticmethod
    def _resolve_script(skill: Skill, value: str) -> Path:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            if candidate.parts and candidate.parts[0] == skill.name:
                candidate = Path(*candidate.parts[1:])
            candidate = skill.root / candidate
        script = candidate.resolve(strict=True)
        scripts_root = (skill.root / "scripts").resolve(strict=True)
        if (
            not script.is_relative_to(scripts_root)
            or not script.is_file()
            or script.suffix != ".py"
        ):
            raise ValueError(
                "only Python files below the selected Skill scripts directory are allowed"
            )
        return script

    async def _terminate(
        self, process: asyncio.subprocess.Process, waiter: asyncio.Task[int]
    ) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = asyncio.get_running_loop().time() + self._termination_grace_seconds
        while self._process_group_exists(process.pid):
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(0.01, remaining))
        if self._process_group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await asyncio.gather(waiter, return_exceptions=True)

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

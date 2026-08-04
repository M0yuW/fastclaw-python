"""Minimal local file, restricted process, and public HTTP data tools."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import shutil
import signal
import socket
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import anyio
import httpx

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult

HostResolver = Callable[[str, int], Awaitable[Sequence[str]]]


class ReadFileTool:
    def __init__(self, root: Path, *, max_bytes: int = 1_000_000) -> None:
        self._root = root.expanduser().resolve()
        self._max_bytes = max_bytes
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="read_file",
                description="Read a UTF-8 file below the configured workspace root.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        path = await anyio.to_thread.run_sync(self._resolve, str(arguments["path"]))
        data = await anyio.to_thread.run_sync(path.read_bytes)
        if len(data) > self._max_bytes:
            return ToolResult(content="file exceeds size limit", is_error=True)
        return ToolResult(content=data.decode("utf-8"))

    def _resolve(self, value: str) -> Path:
        path = (self._root / value).resolve()
        if not path.is_relative_to(self._root) or not path.is_file():
            raise ValueError("path must name a file below the workspace root")
        return path


@dataclass(frozen=True, slots=True)
class _Executable:
    path: Path
    device: int
    inode: int


class ExecTool:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_commands: frozenset[str] = frozenset(),
        allowed_executables: Mapping[str, Path] | None = None,
        trusted_path: Sequence[Path] = (
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/usr/bin"),
            Path("/bin"),
        ),
        writable_roots: Sequence[Path] | None = None,
        max_output_bytes: int = 1_000_000,
        termination_grace_seconds: float = 1.0,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._max_output_bytes = max_output_bytes
        self._termination_grace_seconds = termination_grace_seconds
        roots = writable_roots
        if roots is None:
            roots = (self._workspace, Path(tempfile.gettempdir()))
        self._writable_roots = tuple(root.expanduser().resolve() for root in roots)
        self._executables = self._resolve_executables(
            allowed_commands=allowed_commands,
            allowed_executables=allowed_executables,
            trusted_path=trusted_path,
        )
        self._environment = {
            "PATH": os.pathsep.join(
                dict.fromkeys(
                    str(executable.path.parent) for executable in self._executables.values()
                )
            ),
            "LANG": "C.UTF-8",
        }
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="exec",
                description="Run one allow-listed executable without a shell.",
                parameters={
                    "type": "object",
                    "properties": {"argv": {"type": "array", "items": {"type": "string"}}},
                    "required": ["argv"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) for v in argv):
            raise ValueError("argv must be a non-empty array of strings")
        command = argv[0]
        if (
            Path(command).name != command
            or os.sep in command
            or (os.altsep and os.altsep in command)
        ):
            return ToolResult(content="executable paths are denied by policy", is_error=True)
        executable = self._executables.get(command)
        if executable is None:
            return ToolResult(content="executable is denied by policy", is_error=True)
        if not await anyio.to_thread.run_sync(self._executable_is_unchanged, executable):
            return ToolResult(content="executable changed after policy validation", is_error=True)

        process = await asyncio.create_subprocess_exec(
            str(executable.path),
            *argv[1:],
            cwd=self._workspace,
            env=self._environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        output = bytearray()
        output_lock = asyncio.Lock()
        limit_reached = asyncio.Event()

        async def read_stream(stream: asyncio.StreamReader) -> None:
            while chunk := await stream.read(64 * 1024):
                async with output_lock:
                    remaining = self._max_output_bytes - len(output)
                    if remaining > 0:
                        output.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        limit_reached.set()
                        # Keep draining until termination closes the pipe. Stopping the
                        # reader here can deadlock Process.wait() on a full OS pipe.

        readers = [
            asyncio.create_task(read_stream(process.stdout)),
            asyncio.create_task(read_stream(process.stderr)),
        ]
        process_wait = asyncio.create_task(process.wait())
        limit_wait = asyncio.create_task(limit_reached.wait())
        truncated = False
        try:
            done, _ = await asyncio.wait(
                {process_wait, limit_wait}, return_when=asyncio.FIRST_COMPLETED
            )
            if limit_wait in done and limit_reached.is_set():
                truncated = True
                await self._terminate_process_group(process, process_wait)
            else:
                limit_wait.cancel()
            await asyncio.gather(*readers)
            await process_wait
        except BaseException:
            await self._terminate_process_group(process, process_wait)
            for task in readers:
                task.cancel()
            process_wait.cancel()
            limit_wait.cancel()
            await asyncio.gather(*readers, process_wait, limit_wait, return_exceptions=True)
            raise
        finally:
            if not limit_wait.done():
                limit_wait.cancel()
                await asyncio.gather(limit_wait, return_exceptions=True)

        return ToolResult(
            content=bytes(output).decode(errors="replace"),
            is_error=process.returncode != 0,
            metadata={"exitCode": process.returncode, "truncated": truncated},
        )

    def _resolve_executables(
        self,
        *,
        allowed_commands: frozenset[str],
        allowed_executables: Mapping[str, Path] | None,
        trusted_path: Sequence[Path],
    ) -> dict[str, _Executable]:
        candidates: dict[str, Path] = {}
        if allowed_executables is not None:
            candidates.update(allowed_executables)
        search_path = os.pathsep.join(str(path.expanduser().resolve()) for path in trusted_path)
        for command in allowed_commands:
            if Path(command).name != command:
                raise ValueError("allowed command names must not contain path separators")
            resolved = shutil.which(command, path=search_path)
            if resolved is None:
                raise ValueError(f"allowed executable {command!r} was not found in trusted PATH")
            candidates[command] = Path(resolved)

        executables: dict[str, _Executable] = {}
        for command, candidate in candidates.items():
            if Path(command).name != command:
                raise ValueError("allowed executable names must not contain path separators")
            path = candidate.expanduser().resolve(strict=True)
            if any(path.is_relative_to(root) for root in self._writable_roots):
                raise ValueError(f"allowed executable {path} is inside an agent-writable root")
            file_stat = path.stat()
            if not stat.S_ISREG(file_stat.st_mode) or not os.access(path, os.X_OK):
                raise ValueError(f"allowed executable {path} is not a regular executable file")
            executables[command] = _Executable(path, file_stat.st_dev, file_stat.st_ino)
        return executables

    @staticmethod
    def _executable_is_unchanged(executable: _Executable) -> bool:
        try:
            path = executable.path.resolve(strict=True)
            file_stat = path.stat()
        except (FileNotFoundError, OSError):
            return False
        return (
            path == executable.path
            and file_stat.st_dev == executable.device
            and file_stat.st_ino == executable.inode
        )

    async def _terminate_process_group(
        self,
        process: asyncio.subprocess.Process,
        process_wait: asyncio.Task[int] | None = None,
    ) -> None:
        if process.returncode is not None:
            return
        waiter = process_wait or asyncio.create_task(process.wait())
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        done, _ = await asyncio.wait({waiter}, timeout=self._termination_grace_seconds)
        if waiter in done:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await waiter


class WebFetchTool:
    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_bytes: int = 1_000_000,
        max_redirects: int = 5,
        resolver: HostResolver | None = None,
    ) -> None:
        self._client = client
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._resolver = resolver or self._resolve_host
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="web_fetch",
                description="Fetch public HTTP(S) data.",
                parameters={
                    "type": "object",
                    "properties": {"url": {"type": "string"}},
                    "required": ["url"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        url = str(arguments["url"])
        for redirect_index in range(self._max_redirects + 1):
            denial = await self._validate_public_url(url)
            if denial:
                return ToolResult(content=denial, is_error=True)
            async with self._client.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        return ToolResult(content="redirect has no location", is_error=True)
                    if redirect_index == self._max_redirects:
                        return ToolResult(content="redirect limit exceeded", is_error=True)
                    url = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    remaining = self._max_bytes - len(body)
                    body.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        return ToolResult(content="response exceeds size limit", is_error=True)
                return ToolResult(content=body.decode(errors="replace"))
        raise AssertionError("redirect loop terminated unexpectedly")

    async def _validate_public_url(self, url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"}:
            return "URL scheme is denied"
        if parsed.username is not None or parsed.password is not None:
            return "URL credentials are denied"
        if parsed.hostname is None:
            return "URL host is required"
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return "URL port is invalid"
        try:
            literal = ipaddress.ip_address(parsed.hostname)
            addresses: Sequence[str] = (str(literal),)
        except ValueError:
            try:
                addresses = tuple(await self._resolver(parsed.hostname, port))
            except OSError:
                return "URL host could not be resolved"
        if not addresses:
            return "URL host could not be resolved"
        try:
            if any(not ipaddress.ip_address(address).is_global for address in addresses):
                return "URL host resolves to a non-public address"
        except ValueError:
            return "URL host resolver returned an invalid address"
        return ""

    @staticmethod
    async def _resolve_host(host: str, port: int) -> Sequence[str]:
        loop = asyncio.get_running_loop()
        records = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return tuple(dict.fromkeys(str(record[4][0]) for record in records))

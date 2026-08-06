"""Bounded line-delimited JSON-RPC plugin subprocess."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import signal
import sys
from pathlib import Path
from typing import Any

from fastclaw.plugin.models import (
    PluginManifest,
    PluginProtocolError,
    PluginRPCError,
    PluginUnavailableError,
)

logger = logging.getLogger(__name__)

_MAX_MESSAGE_BYTES = 1024 * 1024


class PluginProcess:
    def __init__(
        self,
        manifest: PluginManifest,
        *,
        config: dict[str, Any],
        data_root: Path,
        environment: dict[str, str] | None = None,
        call_timeout: float = 45.0,
    ) -> None:
        self.manifest = manifest
        self.config = config
        self.data_root = data_root.resolve()
        self.environment = environment or {}
        self.call_timeout = call_timeout
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._write_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._next_id = 1
        self._stopping = False

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self.running:
                return
            argv = self._command()
            self._stopping = False
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.manifest.root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=_MAX_MESSAGE_BYTES + 1,
                env=self._environment(),
            )
            self._reader_task = asyncio.create_task(self._read_loop())
            self._stderr_task = asyncio.create_task(self._stderr_loop())
        try:
            await self.call("initialize", {"config": self.config}, timeout_seconds=10.0)
        except BaseException:
            await self.stop()
            raise

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            raise PluginUnavailableError(f"plugin {self.manifest.id!r} is not running")
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params or {}, "id": request_id},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise PluginProtocolError("plugin request exceeds the 1 MiB limit")
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                if process.returncode is not None or process.stdin.is_closing():
                    raise PluginUnavailableError(f"plugin {self.manifest.id!r} stopped")
                process.stdin.write(payload + b"\n")
                await process.stdin.drain()
            async with asyncio.timeout(timeout_seconds or self.call_timeout):
                return await future
        except TimeoutError as exc:
            await self._terminate(process)
            raise PluginUnavailableError(f"plugin {self.manifest.id!r} timed out") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            process = self._process
            if process is None:
                return
            self._stopping = True
        if process.returncode is None:
            try:
                await self.call("shutdown", timeout_seconds=2.0)
            except (PluginUnavailableError, PluginRPCError, PluginProtocolError):
                pass
            if process.stdin is not None:
                process.stdin.close()
            await self._terminate(process)
        tasks = tuple(task for task in (self._reader_task, self._stderr_task) if task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._fail_pending(PluginUnavailableError(f"plugin {self.manifest.id!r} stopped"))
        async with self._lifecycle_lock:
            self._process = None
            self._reader_task = None
            self._stderr_task = None

    async def _read_loop(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        failure: BaseException = PluginUnavailableError(f"plugin {self.manifest.id!r} exited")
        try:
            while line := await process.stdout.readline():
                if len(line) > _MAX_MESSAGE_BYTES:
                    raise PluginProtocolError("plugin response exceeds the 1 MiB limit")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise PluginProtocolError("plugin emitted malformed JSON") from exc
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise PluginProtocolError("plugin emitted an invalid JSON-RPC message")
                request_id = message.get("id")
                if not isinstance(request_id, int):
                    continue
                future = self._pending.get(request_id)
                if future is None or future.done():
                    continue
                error = message.get("error")
                if isinstance(error, dict):
                    future.set_exception(
                        PluginRPCError(
                            int(error.get("code") or -1),
                            str(error.get("message") or ""),
                        )
                    )
                elif "result" in message:
                    future.set_result(message["result"])
                else:
                    future.set_exception(PluginProtocolError("plugin response has no result"))
        except BaseException as exc:
            failure = exc
        finally:
            if process.returncode is None:
                await self._terminate(process)
            if not self._stopping:
                logger.error("plugin %s exited unexpectedly", self.manifest.id)
            self._fail_pending(failure)

    async def _stderr_loop(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while line := await process.stderr.readline():
            logger.info(
                "plugin %s stderr: %s",
                self.manifest.id,
                line.decode(errors="replace")[:500],
            )

    def _fail_pending(self, error: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(error)

    def _command(self) -> tuple[str, ...]:
        parts = shlex.split(self.manifest.command)
        if not parts:
            raise PluginProtocolError("plugin command is empty")
        executable = sys.executable if parts[0] in {"python", "python3"} else shutil.which(parts[0])
        if executable is None:
            raise PluginUnavailableError(f"plugin executable {parts[0]!r} was not found")
        resolved_executable = Path(executable).resolve(strict=True)
        arguments = list(parts[1:])
        if resolved_executable.name.lower().startswith("python"):
            if not arguments or arguments[0] in {"-c", "-m", "-"}:
                raise PluginProtocolError("Python plugins require a trusted script file")
            declared_script = self.manifest.root / arguments[0]
            if declared_script.is_symlink():
                raise PluginProtocolError("plugin script must not be a symbolic link")
            script = declared_script.resolve(strict=True)
            if not script.is_relative_to(self.manifest.root):
                raise PluginProtocolError("plugin script escapes its manifest directory")
            arguments[0] = str(script)
        return (str(resolved_executable), *arguments)

    def _environment(self) -> dict[str, str]:
        controlled = {
            "HOME": str(self.data_root),
            "FASTCLAW_DATA_ROOT": str(self.data_root),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "PATH": os.defpath,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
        }
        for name, value in self.environment.items():
            if name not in controlled and name.isidentifier():
                controlled[name] = value
        return controlled

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._signal_process_group(process, signal.SIGTERM)
        try:
            async with asyncio.timeout(1.0):
                await process.wait()
        except TimeoutError:
            self._signal_process_group(process, signal.SIGKILL)
            await process.wait()

    @staticmethod
    def _signal_process_group(process: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

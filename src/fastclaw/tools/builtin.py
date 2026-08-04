"""Minimal local file, restricted process, and HTTP data tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio
import httpx

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult


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


class ExecTool:
    def __init__(
        self,
        workspace: Path,
        *,
        allowed_commands: frozenset[str],
        max_output_bytes: int = 1_000_000,
    ) -> None:
        self._workspace = workspace.expanduser().resolve()
        self._allowed_commands = allowed_commands
        self._max_output_bytes = max_output_bytes
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
        if Path(argv[0]).name not in self._allowed_commands:
            return ToolResult(content="executable is denied by policy", is_error=True)
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=self._workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await process.communicate()
        except BaseException:
            process.terminate()
            await process.wait()
            raise
        raw_output = stdout + stderr
        output = raw_output[: self._max_output_bytes].decode(errors="replace")
        return ToolResult(
            content=output,
            is_error=process.returncode != 0,
            metadata={
                "exitCode": process.returncode,
                "truncated": len(raw_output) > self._max_output_bytes,
            },
        )


class WebFetchTool:
    def __init__(self, client: httpx.AsyncClient, *, max_bytes: int = 1_000_000) -> None:
        self._client = client
        self._max_bytes = max_bytes
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
        if urlparse(url).scheme not in {"http", "https"}:
            return ToolResult(content="URL scheme is denied", is_error=True)
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self._max_bytes:
                    return ToolResult(content="response exceeds size limit", is_error=True)
        return ToolResult(content=body.decode(errors="replace"))

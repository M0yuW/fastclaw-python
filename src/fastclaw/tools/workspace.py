"""Workspace listing and atomic write tools."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import anyio

from fastclaw.execution import ExecutionContext
from fastclaw.providers import ToolDefinition, ToolFunction
from fastclaw.tools.base import ToolResult


class ListDirTool:
    def __init__(self, root: Path, *, max_entries: int = 1000) -> None:
        self._root = root.expanduser().resolve()
        self._max_entries = max_entries
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="list_dir",
                description="List files immediately below a directory in the Agent workspace.",
                parameters={
                    "type": "object",
                    "properties": {"path": {"type": "string", "default": "."}},
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        path = await anyio.to_thread.run_sync(self._resolve, str(arguments.get("path") or "."))
        entries = await anyio.to_thread.run_sync(
            lambda: sorted(path.iterdir(), key=lambda p: p.name)
        )
        if len(entries) > self._max_entries:
            return ToolResult(content="directory exceeds entry limit", is_error=True)
        result = [
            {"name": item.name, "type": "directory" if item.is_dir() else "file"}
            for item in entries
        ]
        return ToolResult(content=json.dumps(result, ensure_ascii=False, separators=(",", ":")))

    def _resolve(self, value: str) -> Path:
        path = (self._root / value).resolve()
        if not path.is_relative_to(self._root) or not path.is_dir():
            raise ValueError("path must name a directory below the workspace root")
        return path


class WriteFileTool:
    def __init__(self, root: Path, *, max_bytes: int = 1_000_000) -> None:
        self._root = root.expanduser().resolve()
        self._max_bytes = max_bytes
        self.definition = ToolDefinition(
            function=ToolFunction(
                name="write_file",
                description="Atomically write a UTF-8 file below the Agent workspace root.",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            )
        )

    async def execute(self, arguments: dict[str, Any], context: ExecutionContext) -> ToolResult:
        del context
        content = str(arguments["content"]).encode("utf-8")
        if len(content) > self._max_bytes:
            return ToolResult(content="file exceeds size limit", is_error=True)
        path = await anyio.to_thread.run_sync(self._resolve, str(arguments["path"]))
        await anyio.to_thread.run_sync(self._atomic_write, path, content)
        return ToolResult(content=f"wrote {len(content)} bytes")

    def _resolve(self, value: str) -> Path:
        unresolved = self._root / value
        parent = unresolved.parent.resolve()
        if not parent.is_relative_to(self._root):
            raise ValueError("path must remain below the workspace root")
        if unresolved.exists() and not unresolved.resolve().is_relative_to(self._root):
            raise ValueError("path must remain below the workspace root")
        return parent / unresolved.name

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

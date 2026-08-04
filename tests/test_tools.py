from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.tools import ReadFileTool, ToolRegistry, WebFetchTool


def context() -> ExecutionContext:
    return ExecutionContext(
        user_id="user-1",
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="run-1",
    )


@pytest.mark.asyncio
async def test_read_file_is_confined_to_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fixture = workspace / "data.txt"
    fixture.write_text("safe", encoding="utf-8")
    registry = ToolRegistry([ReadFileTool(workspace)])

    result = await registry.execute("read_file", {"path": "data.txt"}, context())
    denied = await registry.execute("read_file", {"path": "../outside"}, context())

    assert result.content == "safe"
    assert denied.is_error
    assert "ValueError" in denied.content


@pytest.mark.asyncio
async def test_tool_policy_and_web_scheme_are_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="fixture", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry = ToolRegistry([WebFetchTool(client)])
        denied = await registry.execute(
            "web_fetch",
            {"url": "https://example.test/data"},
            context(),
            allowed=frozenset(),
        )
        bad_scheme = await registry.execute("web_fetch", {"url": "file:///etc/passwd"}, context())
        fetched = await registry.execute(
            "web_fetch", {"url": "https://example.test/data"}, context()
        )

    assert denied.is_error
    assert bad_scheme.is_error
    assert fetched.content == "fixture"

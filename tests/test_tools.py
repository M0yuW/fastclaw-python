from __future__ import annotations

import os
import shutil
from collections.abc import Sequence
from pathlib import Path

import anyio
import httpx
import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.tools import ExecTool, ReadFileTool, ToolRegistry, WebFetchTool


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
    assert "tool 'read_file' failed (reference " in denied.content
    assert "outside" not in denied.content


@pytest.mark.asyncio
async def test_tool_policy_and_web_scheme_are_enforced() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="fixture", request=request)

    async def public_resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("93.184.216.34",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        registry = ToolRegistry([WebFetchTool(client, resolver=public_resolver)])
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "http://[::1]/private",
        "http://169.254.169.254/latest/meta-data",
        "https://user:password@example.test/private",
    ],
)
async def test_web_fetch_rejects_non_public_and_credentialed_urls(url: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"denied URL reached transport: {request.url}")

    async def resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("127.0.0.1",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebFetchTool(client, resolver=resolver).execute({"url": url}, context())

    assert result.is_error


@pytest.mark.asyncio
async def test_web_fetch_revalidates_redirect_destinations() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "http://127.0.0.1/private"},
            request=request,
        )

    async def resolver(host: str, port: int) -> Sequence[str]:
        del host, port
        return ("93.184.216.34",)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await WebFetchTool(client, resolver=resolver).execute(
            {"url": "https://public.example/start"}, context()
        )

    assert result.is_error
    assert len(requests) == 1
    assert "non-public" in result.content


@pytest.mark.asyncio
async def test_exec_uses_pinned_absolute_executable_and_denies_argv_paths(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    echo = shutil.which("echo")
    assert echo is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"echo": Path(echo)},
        writable_roots=(),
    )

    result = await tool.execute({"argv": ["echo", "safe"]}, context())
    denied = await tool.execute({"argv": [str(workspace / "echo"), "unsafe"]}, context())

    assert result.content.strip() == "safe"
    assert not result.is_error
    assert denied.is_error
    assert "paths are denied" in denied.content


@pytest.mark.asyncio
async def test_exec_rejects_an_executable_replaced_after_validation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executable = tmp_path / "trusted-echo"
    replacement = tmp_path / "replacement"
    source = shutil.which("echo")
    assert source is not None
    shutil.copy(source, executable)
    shutil.copy(source, replacement)
    tool = ExecTool(
        workspace,
        allowed_executables={"echo": executable},
        writable_roots=(),
    )
    os.replace(replacement, executable)

    result = await tool.execute({"argv": ["echo", "unsafe"]}, context())

    assert result.is_error
    assert "changed after policy validation" in result.content


@pytest.mark.asyncio
async def test_exec_bounds_output_and_kills_a_term_ignoring_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shell = shutil.which("sh")
    assert shell is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"sh": Path(shell)},
        writable_roots=(),
        max_output_bytes=64,
        termination_grace_seconds=0.05,
    )
    started = anyio.current_time()

    result = await tool.execute(
        {"argv": ["sh", "-c", "trap '' TERM; while :; do printf xxxxxxxxxxxxxxxx; done"]},
        context(),
    )

    assert len(result.content.encode()) == 64
    assert result.metadata["truncated"] is True
    assert anyio.current_time() - started < 1


@pytest.mark.asyncio
async def test_exec_cancellation_kills_the_entire_process_group(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shell = shutil.which("sh")
    assert shell is not None
    tool = ExecTool(
        workspace,
        allowed_executables={"sh": Path(shell)},
        writable_roots=(),
        termination_grace_seconds=0.05,
    )
    started = anyio.current_time()
    process_group_file = workspace / "process-group"

    with pytest.raises(TimeoutError):
        with anyio.fail_after(0.05):
            await tool.execute(
                {
                    "argv": [
                        "sh",
                        "-c",
                        (
                            "printf '%s' $$ > process-group; "
                            "trap '' TERM; (trap '' TERM; while :; do :; done) & wait"
                        ),
                    ]
                },
                context(),
            )

    assert anyio.current_time() - started < 1
    process_group_id = int(process_group_file.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.killpg(process_group_id, 0)

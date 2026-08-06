from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from fastclaw.execution import ExecutionContext
from fastclaw.plugin import PluginManager, PluginUnavailableError

PLUGIN_SCRIPT = r"""from __future__ import annotations

import json
import os
import sys
import time


TOOLS = [{
    "name": "echo",
    "description": "Echo trusted context",
    "parameters": {"type": "object", "properties": {"value": {"type": "string"}}},
}]


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    params = request.get("params") or {}
    request_id = request.get("id")
    if method == "initialize":
        result = {"status": "ok"}
    elif method == "tool.list":
        result = {"tools": TOOLS}
    elif method == "tool.execute":
        args = params.get("args") or {}
        mode = args.get("mode")
        if mode == "crash":
            os._exit(17)
        if mode == "hang":
            time.sleep(5)
        result = {"result": json.dumps({"args": args, "context": params.get("context")})}
    elif method == "shutdown":
        result = {"status": "ok"}
    else:
        result = None
    print(json.dumps({"jsonrpc": "2.0", "result": result, "id": request_id}), flush=True)
    if method == "shutdown":
        break
"""


def create_plugin(root: Path, *, timeout_seconds: float = 1.0) -> PluginManager:
    plugin = root / "fixture"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(PLUGIN_SCRIPT, encoding="utf-8")
    (plugin / "plugin.json").write_text(
        json.dumps(
            {
                "id": "fixture",
                "name": "Fixture",
                "version": "1.0.0",
                "type": "tool",
                "command": f"{sys.executable} plugin.py",
                "capabilities": ["tool"],
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(
        (root,),
        data_root=root / "data",
        configurations={"fixture": {"timeoutSeconds": timeout_seconds}},
    )
    manager.discover()
    return manager


def context(user_id: str = "user-1") -> ExecutionContext:
    return ExecutionContext(
        user_id=user_id,
        agent_id="agent-1",
        session_id="session-1",
        root_execution_id="root-1",
        call_path=("agent-1",),
    )


async def test_plugin_manager_discovers_tools_and_injects_trusted_context(tmp_path: Path) -> None:
    manager = create_plugin(tmp_path)
    await manager.start()
    try:
        assert [tool.definition.function.name for tool in manager.tools()] == ["fixture.echo"]

        result = await manager.execute("fixture", "echo", {"value": "hello"}, context())
        payload = json.loads(result["result"])

        assert payload["args"] == {"value": "hello"}
        assert payload["context"] == {
            "userId": "user-1",
            "agentId": "agent-1",
            "sessionId": "session-1",
            "rootExecutionId": "root-1",
            "callPath": ["agent-1"],
        }
    finally:
        await manager.stop()


@pytest.mark.parametrize(
    "argument_name",
    [
        "userId",
        "user_id",
        "agentId",
        "agent_id",
        "sessionId",
        "session_id",
        "rootExecutionId",
        "root_execution_id",
        "callPath",
        "call_path",
    ],
)
async def test_plugin_tool_rejects_model_supplied_identity(
    tmp_path: Path, argument_name: str
) -> None:
    manager = create_plugin(tmp_path)
    await manager.start()
    try:
        result = await manager.tools()[0].execute(
            {"value": "hello", argument_name: "attacker"}, context()
        )
        assert result.is_error is True
        assert "Runtime-managed" in result.content
    finally:
        await manager.stop()


async def test_plugin_timeout_terminates_process_and_future_call_restarts_it(
    tmp_path: Path,
) -> None:
    manager = create_plugin(tmp_path, timeout_seconds=0.1)
    await manager.start()
    instance = manager.instances[0]
    process = instance.process._process
    assert process is not None
    first_pid = process.pid
    try:
        with pytest.raises(PluginUnavailableError, match="timed out"):
            await manager.execute("fixture", "echo", {"mode": "hang"}, context())
        restarted = instance.process._process
        assert restarted is not None
        assert restarted.pid != first_pid

        result = await manager.execute("fixture", "echo", {"value": "after"}, context())

        assert json.loads(result["result"])["args"] == {"value": "after"}
        assert instance.process._process is not None
    finally:
        await manager.stop()


async def test_plugin_crash_is_isolated_and_failed_mutation_is_not_replayed(tmp_path: Path) -> None:
    manager = create_plugin(tmp_path)
    await manager.start()
    try:
        with pytest.raises(PluginUnavailableError):
            await manager.execute("fixture", "echo", {"mode": "crash"}, context())

        result = await manager.execute("fixture", "echo", {"value": "healthy"}, context())
        assert json.loads(result["result"])["args"] == {"value": "healthy"}
    finally:
        await manager.stop()


async def test_plugin_state_can_be_tenant_isolated_by_runtime_context(tmp_path: Path) -> None:
    manager = create_plugin(tmp_path)
    await manager.start()
    try:
        first = await manager.execute("fixture", "echo", {}, context("tenant-a"))
        second = await manager.execute("fixture", "echo", {}, context("tenant-b"))
        assert json.loads(first["result"])["context"]["userId"] == "tenant-a"
        assert json.loads(second["result"])["context"]["userId"] == "tenant-b"
    finally:
        await manager.stop()

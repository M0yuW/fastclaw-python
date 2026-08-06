from __future__ import annotations

import json
import sys
from pathlib import Path

from fastclaw.execution import ExecutionContext
from fastclaw.plugin import PluginManager

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "plugins"


def execution(user_id: str) -> ExecutionContext:
    return ExecutionContext(
        user_id=user_id,
        agent_id="finance-agent",
        session_id="finance-session",
        root_execution_id="finance-root",
        call_path=("finance-agent",),
    )


async def test_finance_plugin_uses_trusted_tenant_and_optimistic_versioning(
    tmp_path: Path,
) -> None:
    manager = PluginManager(
        (PLUGIN_ROOT,),
        data_root=tmp_path,
        configurations={
            "finance-tools": {
                "finskillsPath": str(tmp_path / "skills"),
                "serenitySkillPath": str(tmp_path / "skills" / "serenity-skill"),
                "stateDbPath": str(tmp_path / "data" / "finance-tools.db"),
                "pythonBin": sys.executable,
                "timeoutSeconds": 5,
            }
        },
        enabled={"finance-tools"},
    )
    manager.discover()
    await manager.start()
    try:
        tools = {tool.definition.function.name: tool for tool in manager.tools()}
        save = tools["finance-tools.thesis_save"]
        list_theses = tools["finance-tools.thesis_list"]

        created_result = await save.execute(
            {
                "market": "CN",
                "symbol": "600519",
                "thesis": "Tenant-owned fixture thesis.",
                "status": "active",
                "conviction": 3,
                "assumptions": ["Demand remains stable"],
                "catalysts": ["Inventory falls"],
                "invalidations": ["Receivables rise"],
            },
            execution("tenant-a"),
        )
        created = json.loads(created_result.content)
        thesis = created["data"]["thesis"]
        assert created_result.is_error is False
        assert thesis["version"] == 1

        isolated = await list_theses.execute({"symbol": "600519"}, execution("tenant-b"))
        assert json.loads(isolated.content)["data"]["count"] == 0

        conflict_result = await save.execute(
            {
                "thesis_id": thesis["id"],
                "expected_version": 99,
                "conviction": 4,
            },
            execution("tenant-a"),
        )
        conflict = json.loads(conflict_result.content)
        assert conflict_result.is_error is True
        assert conflict["errors"][0]["code"] == "version_conflict"
        assert conflict["errors"][0]["details"]["actual_version"] == 1

        forged = await list_theses.execute({"userId": "tenant-a"}, execution("tenant-b"))
        assert forged.is_error is True
        assert "Runtime-managed" in forged.content
    finally:
        await manager.stop()

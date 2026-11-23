from dataclasses import replace

import pytest

from fastclaw.evidence import TaskEvidence
from fastclaw.execution import ExecutionContext
from fastclaw.providers import ChatMessage, MessageRole


def test_task_evidence_isolated_and_idempotent() -> None:
    evidence = TaskEvidence("owner", "one")
    child = ExecutionContext("owner", "a", "session", "one", ("coordinator", "a"))
    fact = {"key": "E", "content": "announcement", "source": "fixture"}
    assert evidence.publish(child, fact) == evidence.publish(child, fact)
    assert len(evidence.events) == 1
    with pytest.raises(ValueError, match="conflicting"):
        evidence.publish(child, {**fact, "content": "changed"})
    initial = (ChatMessage(role=MessageRole.TOOL, content="initial"),)
    for other in (replace(child, user_id="another"), replace(child, root_execution_id="two")):
        with pytest.raises(ValueError, match="missing delegated task"):
            evidence.publish(other, fact)
        assert evidence.prepare(replace(other, call_path=("coordinator",)), initial, 1) == initial
    clean = TaskEvidence("owner", "one")
    assert clean.prepare(replace(child, call_path=("coordinator",)), initial, 1) == initial


@pytest.mark.parametrize("status", ["unchanged", "revised", "pending"])
async def test_review_only_selected_claim(status: str) -> None:
    import asyncio
    import json
    from collections import Counter

    from fastclaw.orchestration import InProcessMessageBus, SpawnSubagentTool

    bus = InProcessMessageBus()
    evidence = TaskEvidence("owner", "root")
    root = ExecutionContext("owner", "coord", "s", "root", ("coord",), evidence=evidence)
    calls: Counter[str] = Counter()
    claim = "NVIDIA transition risk is low"

    async def handler(task: str, child: ExecutionContext) -> str:
        calls[child.agent_id] += 1
        if task == "assess":
            return claim if child.agent_id == "risk" else "last quarter revenue 100"
        assert "new management announcement" in task and "next quarter" in task
        evidence.prepare(child, (ChatMessage(role=MessageRole.USER, content=task),), 1)
        assert evidence.inputs[-1]["eventVersions"] == [2]
        if status == "pending":
            return "incomplete response"
        return json.dumps(
            {
                "status": status,
                "conclusion": claim if status == "unchanged" else "risk increased",
                "reason": "assessed the announcement",
            }
        )

    for agent in ("risk", "accounts"):
        bus.register(user_id="owner", agent_id=agent, handler=handler)
    tool = SpawnSubagentTool(bus, ("risk", "accounts"))
    try:
        risk, income = await tool.execute_many(
            tuple({"agent_id": a, "task": "assess"} for a in ("risk", "accounts")), root
        )
        evidence.publish(
            replace(root, agent_id="risk", call_path=("coord", "risk")),
            {"key": "E", "content": "new management announcement", "source": "fixture"},
        )
        req = {
            "review": {
                "resultId": risk.metadata["correlationId"],
                "claim": claim,
                "period": "next quarter",
                "eventVersion": 2,
            }
        }
        first, repeated = await asyncio.gather(
            tool.execute_many((req,), root), tool.execute(req, root)
        )
        assert first[0].content == repeated.content
        assert json.loads(repeated.content)["decision"]["status"] == status
        assert evidence.results[str(risk.metadata["correlationId"])]["result"] == claim
        assert income.content == "last quarter revenue 100"
        assert calls == {"risk": 2, "accounts": 1}
    finally:
        await bus.shutdown()

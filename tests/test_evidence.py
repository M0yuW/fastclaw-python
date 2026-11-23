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


@pytest.mark.parametrize("mode", ["summary", "targeted", "broadcast"])
async def test_delivery_boundary(mode: str) -> None:
    import asyncio
    import json

    evidence = TaskEvidence("owner", "r")
    root = ExecutionContext("owner", "coord", "s", "r", ("coord",), evidence=evidence)

    def child(agent: str) -> ExecutionContext:
        return replace(root, call_path=("coord", agent), agent_id=agent, task_id=agent)

    evidence.configure(
        root,
        {
            "mode": mode,
            "targets": ["b"] if mode == "targeted" else [],
            "asOf": "2020-01-01T00:00:00Z",
        },
    )
    base = (ChatMessage(role=MessageRole.TOOL, content="original"),)
    sent = evidence.prepare(child("b"), base, 1)
    fact = {
        "key": "first",
        "content": "announcement",
        "source": "fixture",
        "publishedAt": "2019-12-31T23:59:59Z",
    }
    for invalid in (
        {k: v for k, v in fact.items() if k != "publishedAt"},
        {**fact, "publishedAt": "2020-01-01T00:00:01Z"},
    ):
        with pytest.raises(ValueError, match="research cutoff"):
            evidence.publish(child("a"), invalid)
    gate = asyncio.Event()

    async def publish(key: str) -> str:
        await gate.wait()
        return evidence.publish(child("a"), {**fact, "key": key})

    cancelled = asyncio.create_task(publish("cancelled"))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    tasks = [asyncio.create_task(publish(k)) for k in ("first", "second", "first")]
    gate.set()
    a, b, duplicate = await asyncio.gather(*tasks)
    assert a == duplicate and {json.loads(a)["version"], json.loads(b)["version"]} == {2, 3}
    assert sent[-1].content == "original"
    for agent in ("b", "c"):
        included = mode == "broadcast" or (mode == "targeted" and agent == "b")
        updated = evidence.prepare(child(agent), base, 2)
        assert ("announcement" in str(updated[-1].content)) == included
        assert evidence.inputs[-1]["eventVersions"] == ([2, 3] if included else [])
    assert json.loads(evidence.configure(root, {"mode": "seal"})) == {"cutoff": 3}
    with pytest.raises(ValueError, match="cutoff"):
        evidence.publish(child("a"), {**fact, "key": "late"})
    assert evidence.publish(child("a"), fact) == a

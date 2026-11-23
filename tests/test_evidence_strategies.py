"""Offline policy comparison. Run with pytest -q -s tests/test_evidence_strategies.py."""

import asyncio
import json
import time
from collections import Counter

import pytest

from fastclaw.evidence import TaskEvidence
from fastclaw.execution import ExecutionContext
from fastclaw.orchestration import InProcessMessageBus, SpawnSubagentTool
from fastclaw.providers import ChatMessage, MessageRole

# A changed risk assessment requires a specialist sign-off. Revenue corrections
# are explicit numbers that a coordinator can apply directly.
CASES = (
    ("none", "", "", ""),
    ("irrelevant", "office opens", "", ""),
    ("risk", "leadership changed", "risk", "high"),
    ("correction", "corrected revenue is 90", "income", "90"),
)
INITIAL = {"risk": "low", "income": "100"}


async def run_case(
    strategy: str, case: str, text: str, affected: str, expected: str
) -> Counter[str]:
    totals: Counter[str] = Counter()
    started = time.perf_counter_ns()
    bus = InProcessMessageBus()
    store = TaskEvidence("owner", case)
    root = ExecutionContext("owner", "coord", "session", case, ("coord",), evidence=store)
    tool = SpawnSubagentTool(bus, tuple(INITIAL))
    calls: Counter[str] = Counter()
    observations: dict[str, list[tuple[str, str]]] = {role: [] for role in INITIAL}
    armed, published = asyncio.Event(), asyncio.Event()

    async def specialist(task: str, child: ExecutionContext) -> str:
        role = child.agent_id
        calls[role] += 1
        if task == "initial":
            if role == "risk":
                armed.set()
                await published.wait()
            else:
                await armed.wait()
                if text:
                    store.publish(child, {"key": case, "content": text, "source": "fixture"})
                published.set()
            answer = INITIAL[role]
        else:
            assert text in task
            answer = expected if affected == role else INITIAL[role]
            if task.startswith("Review "):
                answer = json.dumps({"status": "revised", "conclusion": answer, "reason": text})
        observations[role].append((task, answer))
        return answer

    for role in INITIAL:
        bus.register(user_id="owner", agent_id=role, handler=specialist)
    try:
        results = await asyncio.wait_for(
            tool.execute_many(tuple({"agent_id": r, "task": "initial"} for r in INITIAL), root),
            timeout=3,
        )
        final = {r: result.content for r, result in zip(INITIAL, results, strict=True)}
        store.configure(root, {"mode": "seal"})
        summary = store.prepare(
            root, (ChatMessage(role=MessageRole.TOOL, content=json.dumps(final)),), 1
        )
        summary_input = str(summary[-1].content)
        event = ""
        if "Task evidence:\n" in summary_input:
            snapshot = json.loads(summary_input.split("Task evidence:\n")[1])
            event = snapshot["evidence"][0]["fact"]["content"]
        if event and strategy == "rerun":
            updated = await tool.execute_many(
                tuple({"agent_id": r, "task": "Recompute with " + event} for r in INITIAL), root
            )
            final = {r: v.content for r, v in zip(INITIAL, updated, strict=True)}
        elif event and affected and strategy == "targeted":
            req = {
                "resultId": results[list(INITIAL).index(affected)].metadata["correlationId"],
                "claim": final[affected],
                "period": "fixture period",
                "eventVersion": 2,
            }
            result = await tool.execute({"review": req}, root)
            final[affected] = json.loads(result.content)["decision"]["conclusion"]
        elif event.startswith("corrected revenue"):
            final["income"] = event.split()[-1]
        output = json.dumps({"claims": final, "citedEvent": event})
        totals["stale"] += sum(
            value != (expected if r == affected else INITIAL[r]) for r, value in final.items()
        )
        totals["missed"] += int(bool(affected) and text not in output)
        totals["unrelated"] += sum(n - 1 for r, n in calls.items() if r != affected)
        totals["extraCalls"] += sum(calls.values()) - 2
        # One byte per fixture token. Count specialist requests/replies and the
        # summary input/final output, excluding protocol and tool definitions.
        totals["fixtureTokens"] += len((summary_input + output).encode())
        for r, log in observations.items():
            totals["fixtureTokens"] += sum(len((q + a).encode()) for q, a in log)
            totals["traceable"] += int(
                any(
                    final[r] == a
                    or (q.startswith("Review ") and json.loads(a)["conclusion"] == final[r])
                    for q, a in log
                )
                or (r == "income" and event.endswith(" " + final[r]))
            )
        totals["elapsedUs"] += (time.perf_counter_ns() - started) // 1000
    finally:
        await bus.shutdown()
    return totals


@pytest.mark.parametrize("strategy", ["summary", "targeted", "rerun"])
async def test_strategy_comparison(strategy: str) -> None:
    totals: Counter[str] = Counter()
    for case in CASES:
        totals.update(await run_case(strategy, *case))
    assert totals["stale"] == (1 if strategy == "summary" else 0)
    assert totals["missed"] == 0 and totals["traceable"] == 8
    assert totals["extraCalls"] == {"summary": 0, "targeted": 2, "rerun": 6}[strategy]
    assert totals["unrelated"] == (4 if strategy == "rerun" else 0)
    print(strategy, json.dumps(totals, sort_keys=True))

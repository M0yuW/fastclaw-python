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

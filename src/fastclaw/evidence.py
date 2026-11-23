"""Evidence held for the lifetime of one in-process team execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastclaw.execution import ExecutionContext
    from fastclaw.providers import ChatMessage


@dataclass
class TaskEvidence:
    owner: str
    root: str
    events: list[dict[str, Any]] = field(default_factory=list)
    inputs: list[dict[str, Any]] = field(default_factory=list)

    def matches(self, context: ExecutionContext) -> bool:
        return (context.user_id, context.root_execution_id) == (self.owner, self.root)

    def publish(self, context: ExecutionContext, arguments: dict[str, Any]) -> str:
        if (
            not self.matches(context)
            or len(context.call_path) < 2
            or context.call_path[-1] != context.agent_id
        ):
            raise ValueError("missing delegated task")
        fact = {key: arguments.get(key) for key in ("key", "content", "source")}
        if not all(isinstance(value, str) and value.strip() for value in fact.values()):
            raise ValueError("key, content and source are required")
        if "publishedAt" in arguments:
            value = arguments["publishedAt"]
            if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError("publishedAt requires a timestamp with timezone")
            fact["publishedAt"] = value
        for event in self.events:
            if event["agent"] == context.agent_id and event["fact"]["key"] == fact["key"]:
                if event["fact"] != fact:
                    raise ValueError("conflicting evidence key")
                return json.dumps(event)
        event = dict(
            agent=context.agent_id,
            fact=fact,
            version=len(self.events) + 2,
            receivedAt=datetime.now(UTC).isoformat(),
        )
        self.events.append(event)
        return json.dumps(event)

    def prepare(
        self, context: ExecutionContext, history: tuple[ChatMessage, ...], round_number: int
    ) -> tuple[ChatMessage, ...]:
        if not self.matches(context):
            return history
        if len(context.call_path) > 1:
            receipts = {
                msg.content
                for msg in history
                if msg.role == "tool"
                and msg.name == "publish_evidence"
                and isinstance(msg.content, str)
            }
            seen = [e["version"] for e in self.events if json.dumps(e) in receipts]
            self.inputs.append(
                dict(
                    agent=context.agent_id,
                    correlationId=context.task_id,
                    round=round_number,
                    eventVersions=seen,
                )
            )
        elif self.events and history and history[-1].role == "tool":
            summary = json.dumps(
                dict(
                    root=self.root,
                    cutoff=len(self.events) + 1,
                    evidence=self.events,
                    specialistInputs=self.inputs,
                )
            )
            last = history[-1].model_copy(
                update={"content": f"{history[-1].content}\nTask evidence:\n{summary}"}
            )
            return (*history[:-1], last)
        return history

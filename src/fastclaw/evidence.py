"""Evidence held for the lifetime of one in-process team execution."""

from __future__ import annotations

import asyncio
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

    delivery: str = "summary"
    targets: frozenset[str] = frozenset()
    as_of: datetime | None = None
    started: bool = False
    sealed: bool = False

    results: dict[str, dict[str, str]] = field(default_factory=dict)
    review_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    reviews: dict[str, str] = field(default_factory=dict)
    review_inputs: dict[str, int] = field(default_factory=dict)
    final_rounds: dict[str, int] = field(default_factory=dict)

    def record_model_reply(self, context: ExecutionContext, round_number: int) -> None:
        if self.matches(context) and len(context.call_path) > 1 and context.task_id:
            self.final_rounds[context.task_id] = round_number

    def matches(self, context: ExecutionContext) -> bool:
        return (context.user_id, context.root_execution_id) == (self.owner, self.root)

    def configure(self, context: ExecutionContext, options: dict[str, Any]) -> str:
        if not self.matches(context) or len(context.call_path) != 1:
            raise ValueError("delivery requires a coordinator task")
        mode = options.get("mode")
        if options == {"mode": "seal"}:
            self.sealed = True
            return json.dumps({"cutoff": len(self.events) + 1})
        if self.started or self.sealed:
            raise ValueError("configure delivery before delegation")
        targets = options.get("targets", [])
        if (
            mode not in ("summary", "targeted", "broadcast")
            or not isinstance(targets, list)
            or not all(isinstance(target, str) and target.strip() for target in targets)
            or (mode == "targeted") != bool(targets)
        ):
            raise ValueError("invalid delivery mode or targets")
        cutoff = None
        if "asOf" in options:
            if not isinstance(options["asOf"], str):
                raise ValueError("asOf requires a timestamp")
            cutoff = datetime.fromisoformat(options["asOf"])
            if cutoff.tzinfo is None:
                raise ValueError("asOf requires a timezone")
        self.delivery, self.targets, self.as_of = mode, frozenset(targets), cutoff
        return "Evidence delivery configured"

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
        published: datetime | None = None
        if "publishedAt" in arguments:
            value = arguments["publishedAt"]
            if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
                raise ValueError("publishedAt requires a timestamp with timezone")
            published = datetime.fromisoformat(value)
            fact["publishedAt"] = value
        for event in self.events:
            if event["agent"] == context.agent_id and event["fact"]["key"] == fact["key"]:
                if event["fact"] != fact:
                    raise ValueError("conflicting evidence key")
                return json.dumps(event)
        if self.sealed:
            raise ValueError("evidence cutoff reached")
        if self.as_of is not None and (published is None or published > self.as_of):
            raise ValueError("publication outside the research cutoff")
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
            seen = {e["version"] for e in self.events if json.dumps(e) in receipts}
            for msg in history:
                if (
                    msg.role == "user"
                    and isinstance(msg.content, str)
                    and msg.content in self.review_inputs
                ):
                    seen.add(self.review_inputs[msg.content])
            if (
                history
                and history[-1].role == "tool"
                and (
                    self.delivery == "broadcast"
                    or (self.delivery == "targeted" and context.agent_id in self.targets)
                )
            ):
                updates = [e for e in self.events if e["agent"] != context.agent_id]
                if updates:
                    last = history[-1]
                    content = str(last.content) + "".join(
                        "\nTask update: " + json.dumps(e) for e in updates
                    )
                    history = (*history[:-1], last.model_copy(update={"content": content}))
                    seen.update(e["version"] for e in updates)
            self.inputs.append(
                dict(
                    agent=context.agent_id,
                    correlationId=context.task_id,
                    round=round_number,
                    eventVersions=sorted(seen),
                )
            )
        elif self.events and history and history[-1].role == "tool":
            replies = []
            for message in history:
                if message.role != "tool" or message.name != "spawn_subagent":
                    continue
                task_id = message.metadata.get("correlationId")
                if isinstance(task_id, str) and task_id in self.final_rounds:
                    replies.append(
                        {
                            "toolCallId": message.tool_call_id,
                            "correlationId": task_id,
                            "round": self.final_rounds[task_id],
                        }
                    )
            summary = json.dumps(
                dict(
                    root=self.root,
                    cutoff=len(self.events) + 1,
                    evidence=self.events,
                    specialistInputs=self.inputs,
                    modelReplies=replies,
                    resultsForReview=self.results,
                )
            )
            last = history[-1].model_copy(
                update={"content": f"{history[-1].content}\nTask evidence:\n{summary}"}
            )
            return (*history[:-1], last)
        return history

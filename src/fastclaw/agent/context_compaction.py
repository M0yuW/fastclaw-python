"""Provider-neutral session context planning with an append-only audit history."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from fastclaw.providers import (
    ChatMessage,
    ChatRequest,
    MessageRole,
    Provider,
    ProviderContextCompactor,
)
from fastclaw.storage import SessionContextSnapshotRecord

_SUMMARY_SYSTEM = """You compress old conversation narrative for an agent runtime.
Return a concise factual continuation summary. Preserve user goals, explicit constraints,
decisions, unresolved questions, completed actions, failures, identifiers, dates, and the
next concrete step. Do not invent facts. Machine-readable checkpoint data supplied by the
runtime is authoritative and must not be contradicted. Do not call tools."""


class ContextCompactionSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    mode: str = "shadow"
    profile: str = "auto"
    native_preferred: bool = Field(default=True, alias="nativePreferred")
    trigger_tokens: int = Field(default=64_000, alias="triggerTokens", ge=1)
    trigger_bytes: int = Field(default=262_144, alias="triggerBytes", ge=1)
    target_tokens: int = Field(default=24_000, alias="targetTokens", ge=1)
    recent_user_turns: int = Field(default=6, alias="recentUserTurns", ge=1)
    recent_tool_transactions: int = Field(default=2, alias="recentToolTransactions", ge=0)
    summary_chunk_tokens: int = Field(default=12_000, alias="summaryChunkTokens", ge=512)
    summary_max_tokens: int = Field(default=1_200, alias="summaryMaxTokens", ge=128)
    context_window_tokens: int = Field(default=0, alias="contextWindowTokens", ge=0)

    @property
    def normalized_mode(self) -> str:
        return self.mode if self.mode in {"off", "shadow", "active"} else "shadow"


class ContextSnapshotPersistence(Protocol):
    async def latest_context_snapshot(
        self, user_id: str, agent_id: str, session_id: str
    ) -> SessionContextSnapshotRecord | None: ...

    async def save_context_snapshot(self, snapshot: SessionContextSnapshotRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class ContextPlan:
    messages: tuple[ChatMessage, ...]
    audit_message_count: int
    compacted: bool
    snapshot: SessionContextSnapshotRecord | None = None

    def with_delta(self, audit_history: list[ChatMessage]) -> tuple[ChatMessage, ...]:
        if len(audit_history) <= self.audit_message_count:
            return self.messages
        return (*self.messages, *audit_history[self.audit_message_count :])


def ensure_context_message_ids(
    messages: list[ChatMessage], *, session_id: str
) -> list[ChatMessage]:
    """Add stable IDs without touching provider-native assistant payloads."""

    result: list[ChatMessage] = []
    for index, message in enumerate(messages):
        if message.role is MessageRole.SYSTEM:
            result.append(message)
            continue
        metadata = dict(message.metadata)
        if not metadata.get("contextMessageId"):
            raw = json.dumps(
                message.model_dump(by_alias=True, mode="json", exclude={"timestamp"}),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest = hashlib.sha256(f"{session_id}:{index}:{raw}".encode()).hexdigest()[:24]
            metadata["contextMessageId"] = f"ctx_{digest}"
        result.append(message.model_copy(update={"metadata": metadata}))
    return result


def serialized_size(messages: tuple[ChatMessage, ...] | list[ChatMessage]) -> int:
    return len(
        json.dumps(
            [message.model_dump(by_alias=True, mode="json") for message in messages],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    )


def estimate_tokens(messages: tuple[ChatMessage, ...] | list[ChatMessage]) -> int:
    payload = json.dumps(
        [message.model_dump(by_alias=True, mode="json") for message in messages],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    cjk = sum(1 for char in payload if "\u2e80" <= char <= "\u9fff")
    return max(1, math.ceil(cjk + (len(payload) - cjk) / 4))


class ContextPlanner:
    def __init__(self, provider: Provider, persistence: ContextSnapshotPersistence) -> None:
        self._provider = provider
        self._persistence = persistence

    async def prepare(
        self,
        messages: list[ChatMessage],
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        model: str,
        settings: ContextCompactionSettings,
        scope_guard: str = "",
    ) -> ContextPlan:
        audit = ensure_context_message_ids(messages, session_id=session_id)
        mode = settings.normalized_mode
        before_bytes = serialized_size(audit)
        before_tokens = estimate_tokens(audit)
        dynamic_trigger = settings.trigger_tokens
        if settings.context_window_tokens:
            dynamic_trigger = min(
                dynamic_trigger, max(1, int(settings.context_window_tokens * 0.6))
            )
        if mode == "off" or (
            before_bytes < settings.trigger_bytes and before_tokens < dynamic_trigger
        ):
            return ContextPlan(tuple(audit), len(audit), False)

        profile = self._profile(settings.profile, scope_guard)
        latest = await self._persistence.latest_context_snapshot(user_id, agent_id, session_id)
        reused = self._reuse_snapshot(
            latest,
            audit,
            mode=mode,
            profile=profile,
            model=model,
            settings=settings,
        )
        if reused is not None:
            return reused
        boundary = self._compactable_boundary(audit, settings)
        if boundary <= 0:
            snapshot = await self._snapshot(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                mode=mode,
                status="failed",
                profile=profile,
                strategy="client",
                model=model,
                before_bytes=before_bytes,
                before_tokens=before_tokens,
                after_bytes=before_bytes,
                after_tokens=before_tokens,
                compacted_messages=0,
                retained_messages=len(audit),
                failure_code="no_safe_compaction_boundary",
            )
            return ContextPlan(tuple(audit), len(audit), False, snapshot)

        prefix = [item for item in audit[:boundary] if item.role is not MessageRole.SYSTEM]
        tail = audit[boundary:]
        checkpoint = self._checkpoint(profile, audit)
        previous_boundary = self._snapshot_boundary(
            latest, audit, mode=mode, profile=profile, model=model
        )
        summary_source = prefix
        prior_summary = ""
        if previous_boundary and previous_boundary < boundary and latest is not None:
            summary_source = [
                item
                for item in audit[previous_boundary:boundary]
                if item.role is not MessageRole.SYSTEM
            ]
            prior_summary = latest.summary
        summary, degraded = await self._summary(
            summary_source,
            settings,
            model,
            use_model=mode == "active",
            prior_summary=prior_summary,
        )
        synthetic = ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                "## Runtime session checkpoint\n"
                "The JSON checkpoint is authoritative. The narrative summary is "
                "non-authoritative.\n"
                f"{json.dumps(checkpoint, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"## Prior conversation summary\n{summary}"
            ),
            metadata={"contextCompaction": True, "profile": profile},
        )
        systems = [item for item in audit if item.role is MessageRole.SYSTEM]
        candidate = tuple([*systems, synthetic, *self._sanitize_tail(tail)])
        strategy = "client"
        native_state: dict[str, Any] = {}
        if settings.native_preferred and isinstance(self._provider, ProviderContextCompactor):
            try:
                native = await self._provider.compact_context(
                    messages=candidate,
                    model=model,
                    target_tokens=settings.target_tokens,
                )
                candidate = native.messages
                native_state = native.state
                strategy = native.strategy
            except Exception:
                degraded = True

        after_bytes = serialized_size(candidate)
        after_tokens = estimate_tokens(candidate)
        failure_code = ""
        status = "degraded" if degraded else "ready"
        if after_bytes >= before_bytes or after_tokens >= before_tokens:
            status = "failed"
            failure_code = "no_compaction_savings"
            candidate = tuple(audit)
            after_bytes = before_bytes
            after_tokens = before_tokens

        compacted_through = self._message_id(prefix[-1]) if prefix else ""
        snapshot = await self._snapshot(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            mode=mode,
            status=status,
            profile=profile,
            strategy=strategy,
            model=model,
            compacted_through=compacted_through,
            source_digest=self._digest(prefix),
            summary=summary,
            checkpoint=checkpoint,
            native_state=native_state,
            before_bytes=before_bytes,
            before_tokens=before_tokens,
            after_bytes=after_bytes,
            after_tokens=after_tokens,
            compacted_messages=len(prefix),
            retained_messages=len(candidate),
            failure_code=failure_code,
        )
        if mode == "shadow" or status == "failed":
            return ContextPlan(tuple(audit), len(audit), False, snapshot)
        return ContextPlan(candidate, len(audit), True, snapshot)

    def _reuse_snapshot(
        self,
        snapshot: SessionContextSnapshotRecord | None,
        audit: list[ChatMessage],
        *,
        mode: str,
        profile: str,
        model: str,
        settings: ContextCompactionSettings,
    ) -> ContextPlan | None:
        if (
            snapshot is None
            or snapshot.status not in {"ready", "degraded"}
            or snapshot.strategy != "client"
            or snapshot.mode != mode
            or snapshot.profile != profile
            or snapshot.provider != self._provider.name
            or snapshot.model != model
            or not snapshot.compacted_through_message_id
        ):
            return None
        boundary = self._snapshot_boundary(
            snapshot, audit, mode=mode, profile=profile, model=model
        )
        if not boundary:
            return None
        checkpoint = self._checkpoint(profile, audit)
        synthetic = ChatMessage(
            role=MessageRole.SYSTEM,
            content=(
                "## Runtime session checkpoint\n"
                "The JSON checkpoint is authoritative. The narrative summary is "
                "non-authoritative.\n"
                f"{json.dumps(checkpoint, ensure_ascii=False, separators=(',', ':'))}\n\n"
                f"## Prior conversation summary\n{snapshot.summary}"
            ),
            metadata={"contextCompaction": True, "profile": profile},
        )
        systems = [item for item in audit if item.role is MessageRole.SYSTEM]
        candidate = tuple([*systems, synthetic, *self._sanitize_tail(audit[boundary:])])
        if (
            serialized_size(candidate) >= settings.trigger_bytes
            or estimate_tokens(candidate) >= settings.trigger_tokens
        ):
            return None
        if mode == "shadow":
            return ContextPlan(tuple(audit), len(audit), False, snapshot)
        return ContextPlan(candidate, len(audit), True, snapshot)

    def _snapshot_boundary(
        self,
        snapshot: SessionContextSnapshotRecord | None,
        audit: list[ChatMessage],
        *,
        mode: str,
        profile: str,
        model: str,
    ) -> int:
        if (
            snapshot is None
            or snapshot.status not in {"ready", "degraded"}
            or snapshot.strategy != "client"
            or snapshot.mode != mode
            or snapshot.profile != profile
            or snapshot.provider != self._provider.name
            or snapshot.model != model
            or not snapshot.compacted_through_message_id
        ):
            return 0
        boundary = next(
            (
                index + 1
                for index, message in enumerate(audit)
                if self._message_id(message) == snapshot.compacted_through_message_id
            ),
            0,
        )
        if not boundary:
            return 0
        prefix = [item for item in audit[:boundary] if item.role is not MessageRole.SYSTEM]
        if self._digest(prefix) != snapshot.source_digest:
            return 0
        return boundary

    async def _summary(
        self,
        prefix: list[ChatMessage],
        settings: ContextCompactionSettings,
        model: str,
        *,
        use_model: bool,
        prior_summary: str = "",
    ) -> tuple[str, bool]:
        compact_text = self._narrative(prefix)
        if not compact_text:
            return (
                prior_summary
                or "No earlier narrative is required; authoritative state is in the checkpoint.",
                False,
            )
        if not use_model:
            current = self._deterministic_summary(prefix)
            return "\n".join(part for part in (prior_summary, current) if part)[-8_000:], False
        chunks = self._chunks(compact_text, settings.summary_chunk_tokens)
        summary = prior_summary
        try:
            for chunk in chunks:
                prompt = chunk
                if summary:
                    prompt = f"Existing summary:\n{summary}\n\nNew messages:\n{chunk}"
                response = await self._provider.chat(
                    ChatRequest(
                        messages=(
                            ChatMessage(role=MessageRole.SYSTEM, content=_SUMMARY_SYSTEM),
                            ChatMessage(role=MessageRole.USER, content=prompt),
                        ),
                        model=model,
                        tools=(),
                        max_tokens=settings.summary_max_tokens,
                        temperature=0,
                    )
                )
                text = response.content.strip()
                if not text:
                    raise ValueError("empty context summary")
                summary = text
            return summary, False
        except Exception:
            current = self._deterministic_summary(prefix)
            return "\n".join(part for part in (prior_summary, current) if part)[-8_000:], True

    @staticmethod
    def _profile(configured: str, scope_guard: str) -> str:
        if configured in {"generic", "football"}:
            return configured
        return "football" if scope_guard == "football" else "generic"

    @staticmethod
    def _compactable_boundary(
        messages: list[ChatMessage], settings: ContextCompactionSettings
    ) -> int:
        user_indexes = [
            index for index, item in enumerate(messages) if item.role is MessageRole.USER
        ]
        boundary = (
            user_indexes[-settings.recent_user_turns]
            if len(user_indexes) >= settings.recent_user_turns
            else 0
        )
        tool_indexes = [
            index for index, item in enumerate(messages) if item.role is MessageRole.TOOL
        ]
        for tool_index in tool_indexes[-settings.recent_tool_transactions :]:
            call_index = next(
                (
                    index
                    for index in range(tool_index - 1, -1, -1)
                    if messages[index].role is MessageRole.ASSISTANT
                    and messages[index].tool_calls
                ),
                tool_index,
            )
            boundary = min(boundary, call_index) if boundary else call_index
        last_call = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].role is MessageRole.ASSISTANT
                and messages[index].tool_calls
            ),
            -1,
        )
        if last_call >= 0 and not any(
            item.role is MessageRole.ASSISTANT and not item.tool_calls
            for item in messages[last_call + 1 :]
        ):
            boundary = min(boundary, last_call) if boundary else last_call
        return boundary

    @classmethod
    def _checkpoint(cls, profile: str, messages: list[ChatMessage]) -> dict[str, Any]:
        user_messages = [
            str(item.content or "") for item in messages if item.role is MessageRole.USER
        ]
        tools: list[dict[str, Any]] = []
        fixtures: dict[str, dict[str, Any]] = {}
        pending: dict[str, Any] = {}
        for item in messages:
            if item.role is MessageRole.ASSISTANT and isinstance(item.metadata, dict):
                value = item.metadata.get("checkpoint")
                if isinstance(value, dict):
                    pending = value
            if item.role is not MessageRole.TOOL:
                continue
            tools.append(
                {
                    "name": item.name or "",
                    "error": bool(item.metadata.get("isError", False)),
                    "reference": item.metadata.get("correlationId", ""),
                }
            )
            if profile != "football":
                continue
            try:
                payload = json.loads(str(item.content or ""))
            except (TypeError, json.JSONDecodeError):
                continue
            cls._collect_fixtures(payload, fixtures)
        checkpoint: dict[str, Any] = {
            "version": 1,
            "profile": profile,
            "current_goal": user_messages[-1][:4000] if user_messages else "",
            "completed_tools": tools[-50:],
            "pending": pending,
        }
        if profile == "football":
            checkpoint["fixtures"] = fixtures
            checkpoint["invariants"] = [
                "confirmed evidence cannot be downgraded",
                "ledger writes must remain idempotent",
            ]
        return checkpoint

    @classmethod
    def _collect_fixtures(cls, value: Any, fixtures: dict[str, dict[str, Any]]) -> None:
        if isinstance(value, dict):
            key = str(value.get("fixture_key") or value.get("evidence_key") or "")
            status = str(value.get("status") or "")
            if key and status in {"unknown", "unavailable", "no_match", "confirmed"}:
                current = fixtures.get(key)
                if current is None or current.get("status") != "confirmed":
                    fixtures[key] = {
                        "status": "confirmed" if status == "confirmed" else status,
                        "fixture": value.get("fixture") or value.get("confirmed_identity") or {},
                        "source_statuses": value.get("source_statuses") or [],
                    }
            for child in value.values():
                cls._collect_fixtures(child, fixtures)
        elif isinstance(value, list):
            for child in value:
                cls._collect_fixtures(child, fixtures)

    @staticmethod
    def _sanitize_tail(messages: list[ChatMessage]) -> list[ChatMessage]:
        last_tool_call = next(
            (
                index
                for index in range(len(messages) - 1, -1, -1)
                if messages[index].role is MessageRole.ASSISTANT
                and messages[index].tool_calls
            ),
            len(messages),
        )
        result: list[ChatMessage] = []
        for index, item in enumerate(messages):
            if item.role is MessageRole.ASSISTANT and index < last_tool_call:
                result.append(
                    item.model_copy(
                        update={"thinking": None, "thinking_signature": None, "raw_assistant": None}
                    )
                )
            else:
                result.append(item)
        return result

    @staticmethod
    def _narrative(messages: list[ChatMessage]) -> str:
        lines: list[str] = []
        for item in messages:
            if item.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
                continue
            content = str(item.content or "").strip()
            if content:
                lines.append(f"{item.role.value}: {content}")
        return "\n".join(lines)

    @staticmethod
    def _chunks(text: str, target_tokens: int) -> list[str]:
        limit = max(2048, target_tokens * 3)
        return [text[index : index + limit] for index in range(0, len(text), limit)]

    @staticmethod
    def _deterministic_summary(messages: list[ChatMessage]) -> str:
        lines: list[str] = []
        for item in messages:
            if item.role not in {MessageRole.USER, MessageRole.ASSISTANT}:
                continue
            content = re.sub(r"\s+", " ", str(item.content or "")).strip()
            if content:
                lines.append(f"{item.role.value}: {content[:500]}")
        return "\n".join(lines[-20:])[:8_000] or "Earlier narrative omitted."

    async def _snapshot(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        mode: str,
        status: str,
        profile: str,
        strategy: str,
        model: str,
        before_bytes: int,
        before_tokens: int,
        after_bytes: int,
        after_tokens: int,
        compacted_messages: int,
        retained_messages: int,
        compacted_through: str = "",
        source_digest: str = "",
        summary: str = "",
        checkpoint: dict[str, Any] | None = None,
        native_state: dict[str, Any] | None = None,
        failure_code: str = "",
    ) -> SessionContextSnapshotRecord:
        latest = await self._persistence.latest_context_snapshot(user_id, agent_id, session_id)
        generation = (latest.generation + 1) if latest else 1
        metrics = {
            "beforeBytes": before_bytes,
            "beforeTokens": before_tokens,
            "afterBytes": after_bytes,
            "afterTokens": after_tokens,
            "savingsRatio": (
                round(1 - (after_bytes / before_bytes), 4) if before_bytes else 0
            ),
            "compactedMessages": compacted_messages,
            "retainedMessages": retained_messages,
        }
        snapshot = SessionContextSnapshotRecord(
            id=f"ctxsnap_{uuid4().hex}",
            user_id=user_id,
            agent_id=agent_id,
            session_key=session_id,
            generation=generation,
            mode=mode,
            status=status,
            profile=profile,
            strategy=strategy,
            provider=self._provider.name,
            model=model,
            compacted_through_message_id=compacted_through,
            source_digest=source_digest,
            summary=summary,
            checkpoint=checkpoint or {},
            native_state=native_state or {},
            metrics=metrics,
            failure_code=failure_code,
            created_at=datetime.now(UTC),
        )
        await self._persistence.save_context_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _message_id(message: ChatMessage) -> str:
        return str(message.metadata.get("contextMessageId") or "")

    @staticmethod
    def _digest(messages: list[ChatMessage]) -> str:
        payload = [
            {
                "id": item.metadata.get("contextMessageId", ""),
                "role": item.role.value,
                "content": item.content,
                "toolCallId": item.tool_call_id,
            }
            for item in messages
        ]
        return hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        ).hexdigest()

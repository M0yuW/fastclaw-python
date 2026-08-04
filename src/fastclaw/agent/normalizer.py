"""Repair malformed historical tool-call sequences before provider replay."""

from __future__ import annotations

import json

from fastclaw.providers import ChatMessage, MessageRole, ToolCall


def normalize_messages(messages: tuple[ChatMessage, ...]) -> tuple[ChatMessage, ...]:
    """Drop orphan/duplicate results and synthesize results for unanswered calls."""

    normalized: list[ChatMessage] = []
    pending: dict[str, ToolCall] = {}
    aliases: dict[str, str] = {}
    used_ids: set[str] = set()

    def flush_pending() -> None:
        for call_id, call in pending.items():
            normalized.append(
                ChatMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=call_id,
                    name=call.function.name,
                    content=json.dumps(
                        {"error": "missing tool result repaired during replay"},
                        separators=(",", ":"),
                    ),
                )
            )
        pending.clear()
        aliases.clear()

    for message_index, message in enumerate(messages):
        if message.role is MessageRole.ASSISTANT:
            flush_pending()
            calls: list[ToolCall] = []
            for call_index, call in enumerate(message.tool_calls):
                original_id = call.id
                call_id = original_id
                if not call_id or call_id in used_ids:
                    call_id = f"tool-{message_index}-{call_index}"
                used_ids.add(call_id)
                normalized_call = call.model_copy(update={"id": call_id})
                calls.append(normalized_call)
                pending[call_id] = normalized_call
                if original_id and original_id not in aliases:
                    aliases[original_id] = call_id
            normalized.append(message.model_copy(update={"tool_calls": tuple(calls)}))
            continue

        if message.role is MessageRole.TOOL:
            requested = message.tool_call_id or ""
            call_id = requested if requested in pending else aliases.get(requested, "")
            if not call_id and not requested and len(pending) == 1:
                call_id = next(iter(pending))
            if call_id in pending:
                call = pending.pop(call_id)
                normalized.append(
                    message.model_copy(
                        update={
                            "tool_call_id": call_id,
                            "name": message.name or call.function.name,
                        }
                    )
                )
            continue

        flush_pending()
        normalized.append(message)

    flush_pending()
    return tuple(normalized)

"""A cancellable, streaming ReAct loop with atomic final persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastclaw.agent.models import AgentEvent, AgentEventType, AgentRunError, AgentRunRequest
from fastclaw.agent.normalizer import normalize_messages
from fastclaw.agent.persistence import SessionPersistence
from fastclaw.execution import ExecutionContext, use_execution
from fastclaw.providers import (
    ChatMessage,
    ChatRequest,
    MessageRole,
    Provider,
    ProviderEventType,
    ProviderStream,
    ToolCall,
)
from fastclaw.storage import SessionRecord
from fastclaw.tools import ToolRegistry

logger = logging.getLogger(__name__)


class AgentStream(AsyncIterator[AgentEvent]):
    def __init__(self, source: AsyncIterator[AgentEvent]) -> None:
        self._source = source
        self._result: ChatMessage | None = None
        self._error = ""
        self._closed = False

    def __aiter__(self) -> AgentStream:
        return self

    async def __anext__(self) -> AgentEvent:
        if self._closed:
            raise StopAsyncIteration
        try:
            event = await anext(self._source)
        except StopAsyncIteration:
            self._closed = True
            raise
        if event.type is AgentEventType.ERROR:
            self._error = event.error
        if event.type is AgentEventType.DONE:
            self._result = event.message
        return event

    def result(self) -> ChatMessage:
        if self._error:
            raise AgentRunError(self._error)
        if self._result is None:
            raise AgentRunError("agent stream did not complete successfully")
        return self._result

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            close = getattr(self._source, "aclose", None)
            if close is not None:
                await close()


class AgentRunner:
    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        persistence: SessionPersistence,
    ) -> None:
        self._provider = provider
        self._tools = tools
        self._persistence = persistence

    def stream(self, request: AgentRunRequest, context: ExecutionContext) -> AgentStream:
        return AgentStream(self._run(request, context))

    async def chat(self, request: AgentRunRequest, context: ExecutionContext) -> ChatMessage:
        stream = self.stream(request, context)
        async for _ in stream:
            pass
        return stream.result()

    async def _run(
        self, request: AgentRunRequest, context: ExecutionContext
    ) -> AsyncIterator[AgentEvent]:
        turn_id = str(uuid4())
        message_id = str(uuid4())
        seq = 0
        round_index = 0
        failed_tool_rounds = 0
        provider_stream: ProviderStream | None = None
        current_stage = "session_load"
        started_at = asyncio.get_running_loop().time()

        def event(event_type: AgentEventType, **values: Any) -> AgentEvent:
            nonlocal seq
            emitted = AgentEvent(
                type=event_type,
                turn_id=turn_id,
                message_id=message_id,
                round=round_index,
                seq=seq,
                **values,
            )
            seq += 1
            return emitted

        try:
            stored = await self._persistence.load(
                context.user_id, context.agent_id, context.session_id
            )
            history = self._history(stored)
            if request.system_prompt and not any(
                item.role is MessageRole.SYSTEM for item in history
            ):
                history.insert(
                    0,
                    ChatMessage(role=MessageRole.SYSTEM, content=request.system_prompt),
                )
            history.append(ChatMessage(role=MessageRole.USER, content=request.message))
            history = list(normalize_messages(tuple(history)))
            clarification = self._scope_clarification(request.scope_guard, history)
            if clarification:
                final = ChatMessage(
                    role=MessageRole.ASSISTANT,
                    content=clarification,
                    metadata={"scopeGate": request.scope_guard},
                )
                history.append(final)
                await self._persistence.save(
                    self._session_record(request, context, history, stored)
                )
                yield event(
                    AgentEventType.CONTENT,
                    content=clarification,
                    message=final,
                )
                yield event(AgentEventType.DONE, message=final)
                return
            with use_execution(context):
                for current_round in range(request.max_rounds):
                    round_index = current_round
                    current_stage = "provider_stream"
                    provider_stream = self._provider.stream(
                        ChatRequest(
                            messages=tuple(history),
                            model=request.model,
                            tools=self._tools.definitions(request.allowed_tools),
                            max_tokens=request.max_tokens,
                            temperature=request.temperature,
                            thinking_budget_tokens=request.thinking_budget_tokens,
                        )
                    )
                    async for provider_event in provider_stream:
                        if provider_event.type is ProviderEventType.CONTENT_DELTA:
                            yield event(
                                AgentEventType.CONTENT_DELTA,
                                content=provider_event.content,
                            )
                    response = provider_stream.result()
                    provider_stream = None
                    current_stage = "provider_response"
                    assistant = ChatMessage(
                        role=MessageRole.ASSISTANT,
                        content=response.content,
                        tool_calls=response.tool_calls,
                        thinking=response.thinking or None,
                        thinking_signature=response.thinking_signature or None,
                        raw_assistant=response.raw_assistant,
                    )
                    history.append(assistant)

                    if not assistant.tool_calls:
                        await self._persistence.save(
                            self._session_record(request, context, history, stored)
                        )
                        yield event(
                            AgentEventType.CONTENT,
                            content=response.content,
                            message=assistant,
                        )
                        yield event(AgentEventType.DONE, message=assistant)
                        return

                    parsed_calls = [self._parse_arguments(call) for call in assistant.tool_calls]
                    call_names = tuple(call.function.name for call in assistant.tool_calls)
                    if all(not error for _, error in parsed_calls) and self._tools.supports_batch(
                        call_names, allowed=request.allowed_tools
                    ):
                        current_stage = f"tool_batch:{call_names[0]}"
                        for call in assistant.tool_calls:
                            yield event(AgentEventType.TOOL_CALL, tool_call=call)
                        batch_results = await self._tools.execute_batch(
                            call_names[0],
                            tuple(arguments for arguments, _ in parsed_calls),
                            context,
                            allowed=request.allowed_tools,
                            timeout_seconds=(
                                request.delegation_timeout
                                if call_names[0] == "spawn_subagent"
                                else request.tool_timeout
                            ),
                        )
                        for call, result in zip(assistant.tool_calls, batch_results, strict=True):
                            message_metadata = dict(result.metadata)
                            if result.is_error:
                                message_metadata["isError"] = True
                            history.append(
                                ChatMessage(
                                    role=MessageRole.TOOL,
                                    content=result.content,
                                    tool_call_id=call.id,
                                    name=call.function.name,
                                    metadata=message_metadata,
                                )
                            )
                            yield event(
                                AgentEventType.TOOL_RESULT,
                                tool_call=call,
                                tool_result=result.content,
                                tool_metadata=result.metadata,
                                is_error=result.is_error,
                            )
                        if batch_results and all(result.is_error for result in batch_results):
                            failed_tool_rounds += 1
                        else:
                            failed_tool_rounds = 0
                        if (
                            request.max_failed_tool_rounds
                            and failed_tool_rounds >= request.max_failed_tool_rounds
                        ):
                            final = ChatMessage(
                                role=MessageRole.ASSISTANT,
                                content=request.failed_tool_message,
                                metadata={"evidenceGate": "all_tools_failed"},
                            )
                            history.append(final)
                            await self._persistence.save(
                                self._session_record(request, context, history, stored)
                            )
                            yield event(
                                AgentEventType.CONTENT,
                                content=request.failed_tool_message,
                                message=final,
                            )
                            yield event(AgentEventType.DONE, message=final)
                            return
                        continue

                    round_results: list[bool] = []
                    for call, (arguments, parse_error) in zip(
                        assistant.tool_calls, parsed_calls, strict=True
                    ):
                        current_stage = f"tool:{call.function.name}"
                        yield event(AgentEventType.TOOL_CALL, tool_call=call)
                        result_metadata: dict[str, Any] = {}
                        if parse_error:
                            result_content = parse_error
                            is_error = True
                        else:
                            result = await self._tools.execute(
                                call.function.name,
                                arguments,
                                context,
                                allowed=request.allowed_tools,
                                timeout_seconds=(
                                    request.delegation_timeout
                                    if call.function.name == "spawn_subagent"
                                    else request.tool_timeout
                                ),
                            )
                            result_content = result.content
                            is_error = result.is_error
                            direct_return = result.direct_return
                            result_metadata = result.metadata
                        if parse_error:
                            direct_return = False
                        round_results.append(is_error)
                        message_metadata = dict(result_metadata)
                        if is_error:
                            message_metadata["isError"] = True
                        history.append(
                            ChatMessage(
                                role=MessageRole.TOOL,
                                content=result_content,
                                tool_call_id=call.id,
                                name=call.function.name,
                                metadata=message_metadata,
                            )
                        )
                        yield event(
                            AgentEventType.TOOL_RESULT,
                            tool_call=call,
                            tool_result=result_content,
                            tool_metadata=result_metadata,
                            is_error=is_error,
                        )
                        if direct_return and not is_error:
                            final = ChatMessage(
                                role=MessageRole.ASSISTANT,
                                content=result_content,
                            )
                            history.append(final)
                            await self._persistence.save(
                                self._session_record(request, context, history, stored)
                            )
                            yield event(
                                AgentEventType.CONTENT,
                                content=result_content,
                                message=final,
                            )
                            yield event(AgentEventType.DONE, message=final)
                            return

                    if round_results and all(round_results):
                        failed_tool_rounds += 1
                    else:
                        failed_tool_rounds = 0
                    if (
                        request.max_failed_tool_rounds
                        and failed_tool_rounds >= request.max_failed_tool_rounds
                    ):
                        final = ChatMessage(
                            role=MessageRole.ASSISTANT,
                            content=request.failed_tool_message,
                            metadata={"evidenceGate": "all_tools_failed"},
                        )
                        history.append(final)
                        await self._persistence.save(
                            self._session_record(request, context, history, stored)
                        )
                        yield event(
                            AgentEventType.CONTENT,
                            content=request.failed_tool_message,
                            message=final,
                        )
                        yield event(AgentEventType.DONE, message=final)
                        return

            raise AgentRunError(f"agent exceeded {request.max_rounds} rounds")
        except asyncio.CancelledError:
            logger.warning(
                "agent run cancelled (root=%s agent=%s stage=%s round=%d duration_ms=%d)",
                context.root_execution_id,
                context.agent_id,
                current_stage,
                round_index,
                int((asyncio.get_running_loop().time() - started_at) * 1000),
            )
            raise
        except AgentRunError as exc:
            yield event(AgentEventType.ERROR, error=str(exc), is_error=True)
            yield event(AgentEventType.DONE, is_error=True)
        except Exception as exc:
            yield event(
                AgentEventType.ERROR,
                error=f"{type(exc).__name__}: {exc}",
                is_error=True,
            )
            yield event(AgentEventType.DONE, is_error=True)
        finally:
            if provider_stream is not None:
                await provider_stream.aclose()

    @staticmethod
    def _history(stored: SessionRecord | None) -> list[ChatMessage]:
        if stored is None:
            return []
        return [ChatMessage.model_validate(message) for message in stored.messages]

    @staticmethod
    def _parse_arguments(call: ToolCall) -> tuple[dict[str, Any], str]:
        try:
            arguments = json.loads(call.function.arguments)
        except json.JSONDecodeError as exc:
            return {}, f"invalid tool arguments: {exc.msg}"
        if not isinstance(arguments, dict):
            return {}, "invalid tool arguments: expected an object"
        return arguments, ""

    @staticmethod
    def _scope_clarification(scope_guard: str, history: list[ChatMessage]) -> str:
        if scope_guard != "football":
            return ""
        user_text = " ".join(
            str(message.content or "")
            for message in history
            if message.role is MessageRole.USER and isinstance(message.content, str)
        )
        has_match = bool(
            re.search(r"vs\.?|versus|\bv\.\b|对阵|对战|迎战", user_text, re.IGNORECASE)
        )
        has_competition = bool(
            re.search(
                r"联赛|杯|资格赛|淘汰赛|小组赛|瑞典超|英超|西甲|意甲|德甲|法甲|"
                r"中超|挪超|美职联|欧冠|欧联|亚冠|league|cup|allsvenskan|"
                r"championship|liga|serie",
                user_text,
                re.IGNORECASE,
            )
        )
        has_time = bool(
            re.search(
                r"\b20\d{2}\b|\d{1,2}[-/.月]\d{1,2}|今天|明天|今晚|本轮|"
                r"第\s*(?:\d+|[一二三四五六七八九十百]+)\s*轮|首回合|次回合|赛季",
                user_text,
                re.IGNORECASE,
            )
        )
        missing = []
        if not has_match:
            missing.append("对阵双方")
        if not has_competition:
            missing.append("赛事名称")
        if not has_time:
            missing.append("比赛日期、赛季或轮次")
        if not missing:
            return ""
        return "请先补充" + "、".join(missing) + "。范围确认后我再调用专家并生成预测。"

    @staticmethod
    def _clean_title(value: str, limit: int = 100) -> str:
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        candidate = next(
            (line for line in lines if not re.fullmatch(r"[-_*#~`>\s]+", line)),
            "",
        )
        candidate = re.sub(r"^[#>*_~`\-\s]+", "", candidate)
        candidate = re.sub(r"[*_~`]+", "", candidate)
        return re.sub(r"\s+", " ", candidate).strip()[:limit]

    @staticmethod
    def _session_record(
        request: AgentRunRequest,
        context: ExecutionContext,
        history: list[ChatMessage],
        stored: SessionRecord | None,
    ) -> SessionRecord:
        now = datetime.now(UTC)
        return SessionRecord(
            user_id=context.user_id,
            agent_id=context.agent_id,
            key=context.session_id,
            channel=stored.channel if stored else "web",
            account_id=stored.account_id if stored else "",
            chat_id=stored.chat_id if stored else context.session_id,
            project_id=stored.project_id if stored else "",
            title=(
                stored.title
                if stored is not None and stored.title
                else AgentRunner._clean_title(request.message)
            ),
            messages=[message.model_dump(by_alias=True, mode="json") for message in history],
            message_count=len(history),
            chatter_user_id=stored.chatter_user_id if stored else context.user_id,
            created_at=stored.created_at if stored else now,
            updated_at=now,
        )

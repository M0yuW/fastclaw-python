"""Application-scoped Agent execution manager.

The manager is the single entry point for root chat runs and delegated runs.  It
owns the in-process queue/message bus and keeps provider and tool lifecycles out
of the HTTP gateway.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import text

from fastclaw.agent.models import AgentEvent, AgentEventType, AgentRunError, AgentRunRequest
from fastclaw.agent.persistence import DatabaseSessionPersistence
from fastclaw.agent.runner import AgentRunner
from fastclaw.credentials import CredentialCipher
from fastclaw.execution import ExecutionContext, SharedExecutionState
from fastclaw.orchestration import (
    AsyncTaskQueue,
    InProcessMessageBus,
    SpawnSubagentTool,
    TaskResult,
    TaskSnapshot,
    WaitTicket,
)
from fastclaw.plugin import PluginManager
from fastclaw.providers import ChatMessage, Provider, create_provider
from fastclaw.runtime import Runtime, RuntimeState
from fastclaw.skills import Skill, SkillCatalog, SkillError
from fastclaw.storage import AgentRecord, ConfigRecord, Database, UnitOfWork
from fastclaw.tools import (
    FootballContextTool,
    FootballDataTool,
    FootballLedgerTool,
    FootballOddsTool,
    ListDirTool,
    ReadFileTool,
    SkillScriptTool,
    ToolRegistry,
    WebFetchTool,
    WorldCupLedgerTool,
    WriteFileTool,
)

_STANDARD_PROVIDERS: dict[str, tuple[str, str]] = {
    "deepseek": ("https://api.deepseek.com", "openai-compatible"),
    "openrouter": ("https://openrouter.ai/api/v1", "openai-compatible"),
}

_DELEGATION_TOOL_PROMPT = """## Runtime delegation contract

`spawn_subagent` accepts exactly two arguments: `agent_id` and `task`. Tenant,
session, root execution, and call-path identity are injected by the Runtime and
must not be supplied by the model. To delegate several independent tasks in
parallel, emit several ordinary `spawn_subagent` tool calls in the same
assistant turn. Do not use legacy `delegations`, `sharedContext`, or `agentId`
wrapper fields.
"""

_EV_REQUEST_PATTERN = re.compile(
    r"(?:\bev\b|expected\s+value|sporttery|odds?|bet(?:ting)?|wager|stake|price|"
    r"赔率|盘口|投注|下注|体彩|期望值|去水|市场价格|敏感性)",
    re.IGNORECASE,
)


def _explicit_ev_request(message: str) -> bool:
    """Return whether the current user request explicitly asks for EV/market data."""
    return bool(_EV_REQUEST_PATTERN.search(message.strip()))


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    data_root: Path
    legacy_data_root: Path = Path.home() / ".fastclaw"
    default_provider_name: str = ""
    default_provider_api_key: str = ""
    default_provider_api_base: str = ""
    default_provider_api_type: str = "openai-compatible"
    default_model: str = ""
    master_key: str = ""
    max_concurrent: int = 8
    max_pending: int = 256
    enable_plugins: bool = True


@dataclass(frozen=True, slots=True)
class AgentRuntimeProfile:
    agent: AgentRecord
    system_prompt: str
    allowed_tools: frozenset[str] | None
    skills: tuple[Skill, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    name: str
    api_key: str
    api_base: str
    api_type: str
    model: str
    source: str
    config_id: str = ""


class AgentManagerShutdownError(RuntimeError):
    """Raised after every Agent manager resource has been asked to stop."""

    def __init__(self, errors: list[BaseException]) -> None:
        super().__init__(f"Agent manager shutdown completed with {len(errors)} error(s)")
        self.errors = tuple(errors)


class ToolFactory(Protocol):
    def __call__(
        self,
        profile: AgentRuntimeProfile,
        bus: InProcessMessageBus,
        runtime: Runtime,
        data_root: Path,
        legacy_data_root: Path,
        catalog: SkillCatalog,
        plugins: PluginManager,
    ) -> ToolRegistry: ...


def _default_tools(
    profile: AgentRuntimeProfile,
    bus: InProcessMessageBus,
    runtime: Runtime,
    data_root: Path,
    legacy_data_root: Path,
    catalog: SkillCatalog,
    plugins: PluginManager,
) -> ToolRegistry:
    workspace = data_root / "workspaces" / profile.agent.id
    configured_targets = profile.agent.config.get("teamSpecialistIds")
    team_targets = (
        tuple(str(agent_id) for agent_id in configured_targets)
        if isinstance(configured_targets, list)
        else None
    )
    team_data_agent_id = str(profile.agent.config.get("teamDataAgentId") or "")
    football_mode = str(profile.agent.config.get("footballDataMode") or "")
    web_fetch = WebFetchTool(
        runtime.web_http_client,
        blocked_hosts=frozenset({"thesportsdb.com"})
        if football_mode == "context"
        else frozenset(),
    )
    agent_role = str(profile.agent.config.get("teamRole") or "").casefold()
    agent_name = profile.agent.name.casefold()
    allow_sporttery = agent_role == "ev-analyst" or "ev analyst" in agent_name
    tools: list[Any] = [
        ReadFileTool(workspace),
        ListDirTool(workspace),
        WriteFileTool(workspace),
        web_fetch,
        FootballContextTool(),
        FootballDataTool(
            web_fetch,
            allow_sporttery=allow_sporttery,
            role_mode=(
                "data"
                if football_mode == "data"
                else "ev"
                if football_mode == "ev"
                else "full"
            ),
        ),
        FootballOddsTool(),
        SpawnSubagentTool(bus, team_targets, data_agent_id=team_data_agent_id),
        FootballLedgerTool(data_root),
        WorldCupLedgerTool(data_root),
    ]
    if profile.skills:
        tools.append(
            SkillScriptTool(
                catalog,
                profile.skills,
                forbidden_roots=(legacy_data_root,),
                forbidden_scripts=() if allow_sporttery else ("sporttery_data.py",),
            )
        )
    tools.extend(plugins.tools())
    return ToolRegistry(tools)


class ManagedAgentStream(AsyncIterator[AgentEvent]):
    """A root run whose producer is owned by the shared task queue."""

    _STOP = object()

    def __init__(
        self,
        *,
        manager: AgentRuntimeManager,
        context: ExecutionContext,
        model: str,
        producer: Callable[[Callable[[AgentEvent], None]], Awaitable[TaskResult]],
    ) -> None:
        self._manager = manager
        self.context = context
        self.model = model
        self._events: asyncio.Queue[AgentEvent | object] = asyncio.Queue()
        self._result: ChatMessage | None = None
        self._error = ""
        self._closed = False
        self._detached = False
        self._saw_error = False
        self._saw_done = False

        def emit(event: AgentEvent) -> None:
            if event.type is AgentEventType.ERROR:
                self._saw_error = True
            if event.type is AgentEventType.DONE:
                self._saw_done = True
            # The HTTP consumer may disappear while the root run is still
            # executing.  Do not let a disconnected SSE client turn the
            # unbounded event queue into a memory leak.
            if self._detached:
                return
            self._events.put_nowait(event)

        async def supervise() -> None:
            try:
                await producer(emit)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._saw_error:
                    turn_id = str(uuid4())
                    message_id = str(uuid4())
                    reason = AgentRuntimeManager.safe_error(exc)
                    emit(
                        AgentEvent(
                            type=AgentEventType.ERROR,
                            turn_id=turn_id,
                            message_id=message_id,
                            round=0,
                            seq=0,
                            error=reason,
                            is_error=True,
                        )
                    )
                    emit(
                        AgentEvent(
                            type=AgentEventType.DONE,
                            turn_id=turn_id,
                            message_id=message_id,
                            round=0,
                            seq=1,
                            is_error=True,
                        )
                    )
            finally:
                self._manager._untrack_root(self.context)
                if not self._saw_done:
                    self._error = self._error or "agent stream ended without a terminal event"
                if not self._detached:
                    self._events.put_nowait(self._STOP)

        self._manager._track_root(self.context)
        self._task = asyncio.create_task(supervise())

    def __aiter__(self) -> ManagedAgentStream:
        return self

    async def __anext__(self) -> AgentEvent:
        if self._closed:
            raise StopAsyncIteration
        item = await self._events.get()
        if item is self._STOP:
            self._closed = True
            raise StopAsyncIteration
        assert isinstance(item, AgentEvent)
        if item.type is AgentEventType.ERROR:
            self._error = item.error
        if item.type is AgentEventType.DONE and item.message is not None:
            self._result = item.message
        return item

    def result(self) -> ChatMessage:
        if self._error:
            raise AgentRunError(self._error)
        if self._result is None:
            raise AgentRunError("agent stream did not complete successfully")
        return self._result

    @property
    def detached(self) -> bool:
        return self._detached

    def detach(self) -> None:
        """Detach the event consumer without cancelling the root execution.

        SSE is a presentation channel, not the lifetime owner of an Agent
        run.  A browser refresh, network interruption, or proxy timeout must
        therefore leave the producer and its queue job alive.  Once detached,
        future events are discarded and already queued events are drained so
        a long-running run cannot accumulate an unbounded in-memory backlog.
        Explicit cancellation continues to use :meth:`aclose`.
        """

        if self._detached:
            return
        self._detached = True
        self._closed = True
        while True:
            try:
                self._events.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def aclose(self) -> None:
        if self._closed and self._task.done():
            return
        self._closed = True
        await self._manager.cancel_root(self.context.root_execution_id)
        if not self._task.done():
            self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)


class AgentRuntimeManager:
    """Own profiles, providers, tools, queueing, delegation, and cancellation."""

    def __init__(
        self,
        database: Database,
        runtime: Runtime,
        config: AgentRuntimeConfig,
        *,
        tool_factory: ToolFactory = _default_tools,
    ) -> None:
        self.database = database
        self.runtime = runtime
        self.config = config
        self.credential_cipher = CredentialCipher(config.data_root, config.master_key)
        self._queue = AsyncTaskQueue(
            max_concurrent=config.max_concurrent,
            max_pending=config.max_pending,
        )
        self.bus = InProcessMessageBus(self._queue)
        self._tool_factory = tool_factory
        self._profiles: dict[str, AgentRuntimeProfile] = {}
        self._agent_roots: dict[str, set[str]] = {}
        self._session_roots: dict[tuple[str, str, str], set[str]] = {}
        self.skill_catalog = SkillCatalog(config.data_root / "skills")
        package_plugins = Path(__file__).resolve().parents[1] / "bundled_plugins"
        checkout_plugins = Path(__file__).resolve().parents[3] / "plugins"
        bundled_plugins = package_plugins if package_plugins.is_dir() else checkout_plugins
        self.plugin_manager = PluginManager(
            (bundled_plugins,),
            data_root=config.data_root,
            enabled={"finance-tools"},
        )
        self._skill_errors: dict[str, str] = {}
        self._profile_lock = asyncio.Lock()
        self._started = False
        self._closing = False

    @property
    def started(self) -> bool:
        return self._started and not self._closing

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    @property
    def pending_count(self) -> int:
        return self._queue.pending_count

    def recent_tasks(self) -> tuple[TaskSnapshot, ...]:
        return self._queue.recent_tasks()

    @property
    def profiles(self) -> Mapping[str, AgentRuntimeProfile]:
        return MappingProxyType(self._profiles)

    @property
    def skill_errors(self) -> Mapping[str, str]:
        return MappingProxyType(self._skill_errors)

    async def start(self) -> None:
        if self._started:
            return
        if self.runtime.state is not RuntimeState.RUNNING:
            raise RuntimeError("Agent manager requires a running Runtime")
        self.skill_catalog.discover()
        if self.config.enable_plugins:
            plugin_config, plugin_environment, enabled_plugins = await self._plugin_settings()
            self.plugin_manager.configurations = plugin_config
            self.plugin_manager.environments = plugin_environment
            self.plugin_manager.enabled = enabled_plugins
        else:
            self.plugin_manager.enabled = set()
        self.plugin_manager.discover()
        if self.config.enable_plugins:
            await self.plugin_manager.start()
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            users = await store.list_users()
            agents = [agent for user in users for agent in await store.list_agents(user.id)]
        for agent in agents:
            await self.ensure_profile(agent)
        self._started = True

    async def stop(self) -> None:
        if self._closing:
            return
        self._closing = True
        errors: list[BaseException] = []
        try:
            await self.bus.shutdown()
        except BaseException as exc:
            errors.append(exc)
        try:
            await self.plugin_manager.stop()
        except BaseException as exc:
            errors.append(exc)
        self._started = False
        if errors:
            raise AgentManagerShutdownError(errors)

    async def ensure_profile(self, agent: AgentRecord) -> AgentRuntimeProfile:
        async with self._profile_lock:
            current = self._profiles.get(agent.id)
            if current is not None and current.agent.updated_at == agent.updated_at:
                return current
            profile = await self._load_profile(agent)
            if current is None:
                self.bus.register(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    handler=partial(self._delegated_chat, agent.id),
                )
            self._profiles[agent.id] = profile
            return profile

    async def reload_profile(self, agent: AgentRecord) -> AgentRuntimeProfile:
        async with self._profile_lock:
            profile = await self._load_profile(agent)
            if agent.id not in self._profiles:
                self.bus.register(
                    user_id=agent.user_id,
                    agent_id=agent.id,
                    handler=partial(self._delegated_chat, agent.id),
                )
            self._profiles[agent.id] = profile
            return profile

    async def reload_profiles(self) -> None:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            users = await store.list_users()
            agents = [agent for user in users for agent in await store.list_agents(user.id)]
        for agent in agents:
            await self.reload_profile(agent)

    def remove_profile(self, agent_id: str) -> None:
        self._profiles.pop(agent_id, None)
        self.bus.unregister(agent_id)

    async def profile(self, agent_id: str, user_id: str) -> AgentRuntimeProfile:
        async with UnitOfWork(self.database) as unit:
            agent = await unit.require_store().get_agent(agent_id)
        if agent is None or agent.user_id != user_id:
            raise LookupError("agent not found")
        return await self.ensure_profile(agent)

    async def stream(
        self,
        *,
        user_id: str,
        agent_id: str,
        session_id: str,
        message: str,
        requested_model: str = "",
        root_execution_id: str = "",
    ) -> ManagedAgentStream:
        self._require_running()
        profile = await self.profile(agent_id, user_id)
        if not await self._agent_accepts_tasks(agent_id, user_id):
            raise AgentRunError("agent team is archived")
        selection = await self.provider_selection(profile, requested_model)
        context = ExecutionContext(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            root_execution_id=root_execution_id or f"run_{uuid4().hex}",
            call_path=(agent_id,),
            shared_state=SharedExecutionState(ev_requested=_explicit_ev_request(message)),
        )
        request = self._request(profile, selection.model, message)

        async def producer(emit: Callable[[AgentEvent], None]) -> TaskResult:
            ticket: WaitTicket | None = None

            async def execute() -> TaskResult:
                value = await self._run(profile, selection, request, context, emit)
                return TaskResult(correlation_id=f"run_{uuid4().hex}", value=value)

            try:
                ticket = await self._queue.submit(
                    target=(user_id, agent_id),
                    dedup_key=(user_id, context.root_execution_id, agent_id, message),
                    root_execution_id=context.root_execution_id,
                    inherit_slot=False,
                    handler=execute,
                )
                return await ticket.result()
            except asyncio.CancelledError:
                if ticket is not None:
                    await ticket.release(cancel=True)
                raise
            finally:
                if ticket is not None:
                    await ticket.release()

        return ManagedAgentStream(
            manager=self,
            context=context,
            model=selection.model,
            producer=producer,
        )

    async def chat(self, **values: str) -> ChatMessage:
        stream = await self.stream(**values)
        try:
            async for _ in stream:
                pass
            return stream.result()
        except asyncio.CancelledError:
            # The compatibility non-streaming endpoint is still request-bound
            # at the HTTP layer.  If that request is cancelled because the
            # browser disconnected, preserve the Agent run just like the SSE
            # endpoint does.  Explicit stop/archive paths call cancel_root
            # directly and are unaffected.
            stream.detach()
            raise
        finally:
            if not stream.detached:
                await stream.aclose()

    async def cancel_root(self, root_execution_id: str) -> None:
        await self.bus.cancel_root(root_execution_id)

    async def cancel_agent_roots(self, agent_ids: tuple[str, ...]) -> None:
        roots = {root for agent_id in agent_ids for root in self._agent_roots.get(agent_id, set())}
        await asyncio.gather(*(self.cancel_root(root) for root in roots))

    async def cancel_session_roots(self, *, user_id: str, agent_id: str, session_id: str) -> int:
        """Cancel all active roots for one tenant/agent/session tuple."""
        key = (user_id, agent_id, session_id)
        roots = tuple(self._session_roots.get(key, ()))
        await asyncio.gather(*(self.cancel_root(root) for root in roots))
        return len(roots)

    def _track_root(self, context: ExecutionContext) -> None:
        self._agent_roots.setdefault(context.agent_id, set()).add(context.root_execution_id)
        key = (context.user_id, context.agent_id, context.session_id)
        self._session_roots.setdefault(key, set()).add(context.root_execution_id)

    def _untrack_root(self, context: ExecutionContext) -> None:
        roots = self._agent_roots.get(context.agent_id)
        if roots is not None:
            roots.discard(context.root_execution_id)
            if not roots:
                self._agent_roots.pop(context.agent_id, None)
        key = (context.user_id, context.agent_id, context.session_id)
        session_roots = self._session_roots.get(key)
        if session_roots is not None:
            session_roots.discard(context.root_execution_id)
            if not session_roots:
                self._session_roots.pop(key, None)

    async def readiness(self) -> dict[str, bool]:
        database_ready = False
        try:
            async with self.database.session() as session:
                database_ready = (await session.scalar(text("SELECT 1"))) == 1
        except Exception:
            database_ready = False
        provider_status = await self.runtime.readiness()
        provider_ready = any(provider_status.values())
        if not provider_ready and self._profiles:
            checks = await asyncio.gather(
                *(self._selection_is_configured(profile) for profile in self._profiles.values())
            )
            provider_ready = bool(checks) and all(checks)
        return {
            "database": database_ready,
            "agent_manager": self.started,
            "providers": provider_ready,
            "skills": not self._skill_errors
            and all(
                self.skill_catalog.is_prepared(skill)
                for profile in self._profiles.values()
                for skill in profile.skills
            ),
            "plugins": all(
                not instance.enabled or instance.process.running
                for instance in self.plugin_manager.instances
            ),
        }

    async def provider_selection(
        self, profile: AgentRuntimeProfile, requested_model: str = ""
    ) -> ProviderSelection:
        agent = profile.agent
        model = requested_model or str(agent.config.get("model") or self.config.default_model)
        provider_name = str(agent.config.get("provider") or "").strip() or (
            model.split("/", 1)[0] if "/" in model else self.config.default_provider_name
        )
        if provider_name in self.runtime.providers and model:
            return ProviderSelection(
                name=provider_name,
                api_key="",
                api_base="",
                api_type="runtime",
                model=model,
                source="runtime",
            )
        configs = await self._provider_configs(agent.user_id, agent.id)
        selected = configs.get(provider_name)
        if selected is not None:
            data = selected.data
            api_key = self.provider_credential(
                selected.name,
                selected.data,
                credential_context=selected.id,
            )
            standard = _STANDARD_PROVIDERS.get(selected.name, ("", "openai-compatible"))
            api_base = str(data.get("apiBase") or standard[0])
            api_type = str(data.get("apiType") or standard[1])
            configured_model = str(data.get("model") or "")
            model = model or configured_model
            if api_key and api_base and model:
                return ProviderSelection(
                    name=selected.name,
                    api_key=api_key,
                    api_base=api_base,
                    api_type=api_type,
                    model=model,
                    source=selected.scope,
                    config_id=selected.id,
                )
        default_provider = _STANDARD_PROVIDERS.get(provider_name)
        if default_provider is not None and model:
            key = self.provider_environment_key(provider_name)
            if key:
                return ProviderSelection(
                    name=provider_name,
                    api_key=key,
                    api_base=default_provider[0],
                    api_type=default_provider[1],
                    model=model,
                    source="environment-default",
                )
        if (
            self.config.default_provider_name
            and self.config.default_provider_api_base
            and model
            and (not provider_name or provider_name == self.config.default_provider_name)
        ):
            key = self.config.default_provider_api_key or self.provider_environment_key(
                provider_name
            )
            if key:
                return ProviderSelection(
                    name=self.config.default_provider_name,
                    api_key=key,
                    api_base=self.config.default_provider_api_base,
                    api_type=self.config.default_provider_api_type,
                    model=model,
                    source="environment",
                )
        raise RuntimeError("no usable provider is configured for this agent")

    async def _selection_is_configured(self, profile: AgentRuntimeProfile) -> bool:
        try:
            await self.provider_selection(profile)
        except RuntimeError:
            return False
        return True

    async def _provider_configs(self, user_id: str, agent_id: str) -> dict[str, ConfigRecord]:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            layers = [
                await store.list_configs(kind="provider", user_id="", agent_id=""),
                await store.list_configs(kind="provider", user_id=user_id, agent_id=""),
                await store.list_configs(kind="provider", user_id=user_id, agent_id=agent_id),
            ]
        configs: dict[str, ConfigRecord] = {}
        for layer in layers:
            configs.update({item.name: item for item in layer if item.enabled})
        return configs

    async def _agent_accepts_tasks(self, agent_id: str, user_id: str) -> bool:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            agent = await store.get_agent(agent_id)
            if agent is None or agent.user_id != user_id:
                return False
            for team in await store.list_teams(user_id):
                members = await store.list_team_members(team.id)
                member = next((member for member in members if member.agent_id == agent_id), None)
                if member is not None:
                    return team.status == "active" and member.status == "active"
        return True

    async def _delegated_chat(self, agent_id: str, task: str, context: ExecutionContext) -> str:
        if not await self._agent_accepts_tasks(agent_id, context.user_id):
            raise AgentRunError("agent team is archived")
        if len(context.call_path) > 1:
            source_id = context.call_path[-2]
            async with UnitOfWork(self.database) as unit:
                store = unit.require_store()
                permitted = False
                source_is_team_member = False
                target_is_team_member = False
                for team in await store.list_teams(context.user_id):
                    members = await store.list_team_members(team.id)
                    source = next(
                        (member for member in members if member.agent_id == source_id), None
                    )
                    target = next(
                        (member for member in members if member.agent_id == agent_id), None
                    )
                    source_is_team_member = source_is_team_member or source is not None
                    target_is_team_member = target_is_team_member or target is not None
                    if team.status != "active":
                        continue
                    if (
                        source
                        and target
                        and source.member_type == "coordinator"
                        and target.member_type == "specialist"
                        and target.status == "active"
                    ):
                        permitted = True
                        break
            if (source_is_team_member or target_is_team_member) and not permitted:
                raise AgentRunError("team delegation is restricted to active specialists")
        profile = self._profiles[agent_id]
        target_role = str(profile.agent.config.get("teamRole") or "").casefold()
        if target_role == "ev-analyst" and not context.shared_state.ev_requested:
            raise AgentRunError(
                "EV analyst is only available when the user explicitly requests odds or EV analysis"
            )
        selection = await self.provider_selection(profile)
        shared_evidence = context.shared_state.render_football_base()
        if shared_evidence and "## Shared football base evidence" not in task:
            task = (
                f"{task}\n\n## Shared football base evidence\n"
                "The data analyst already confirmed this fixture. Reuse this evidence; "
                "do not re-query TheSportsDB or replace the confirmed fixture.\n"
                f"{shared_evidence}"
            )
        request = self._request(profile, selection.model, task)
        return await self._run(profile, selection, request, context, lambda event: None)

    async def _run(
        self,
        profile: AgentRuntimeProfile,
        selection: ProviderSelection,
        request: AgentRunRequest,
        context: ExecutionContext,
        emit: Callable[[AgentEvent], None],
    ) -> str:
        provider, owned = await self._provider(selection)
        tools = self._tool_factory(
            profile,
            self.bus,
            self.runtime,
            self.config.data_root,
            self.config.legacy_data_root,
            self.skill_catalog,
            self.plugin_manager,
        )
        runner = AgentRunner(provider, tools, DatabaseSessionPersistence(self.database))
        final: ChatMessage | None = None
        stream = runner.stream(request, context)
        try:
            async for event in stream:
                emit(event)
                if event.type is AgentEventType.DONE and event.message is not None:
                    final = event.message
            if final is None:
                stream.result()
            assert final is not None
            return str(final.content or "")
        finally:
            await stream.aclose()
            if owned:
                await provider.stop()

    async def _provider(self, selection: ProviderSelection) -> tuple[Provider, bool]:
        shared = self.runtime.providers.get(selection.name)
        if shared is not None:
            return shared, False
        provider = create_provider(
            name=selection.name,
            api_key=selection.api_key,
            api_base=selection.api_base,
            api_type=selection.api_type,
        )
        await provider.start(self.runtime.http_client)
        return provider, True

    async def _load_profile(self, agent: AgentRecord) -> AgentRuntimeProfile:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            records = await store.list_agent_files(agent.id, agent.user_id)
            teams = await store.list_teams(agent.user_id)
            team_members = [(team, await store.list_team_members(team.id)) for team in teams]
            default_layers = [
                await store.list_configs(kind="setting", user_id="", agent_id=""),
                await store.list_configs(kind="setting", user_id=agent.user_id, agent_id=""),
                await store.list_configs(kind="setting", user_id=agent.user_id, agent_id=agent.id),
            ]
            roster_agents = {
                member.agent_id: await store.get_agent(member.agent_id)
                for _, members in team_members
                for member in members
            }
        files: dict[str, bytes] = {}
        asset_root = self.config.data_root / "agents" / agent.id
        for filename in (
            "agent.json",
            "SOUL.md",
            "IDENTITY.md",
            "USER.md",
            "MEMORY.md",
            "TOOLS.md",
            "BOOTSTRAP.md",
            "HEARTBEAT.md",
            "AGENTS.md",
        ):
            path = asset_root / filename
            if path.is_file() and not path.is_symlink():
                files[filename] = path.read_bytes()
        # Database edits are deliberate runtime overrides; imported files stay
        # byte-for-byte unchanged on disk for source-hash auditing.
        files.update({record.filename: record.data for record in records})
        file_config: dict[str, Any] = {}
        raw_config = files.get("agent.json")
        if raw_config:
            try:
                parsed = json.loads(raw_config)
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = None
            if isinstance(parsed, dict):
                file_config = parsed
        defaults: dict[str, Any] = {}
        for layer in default_layers[:2]:
            for record in layer:
                if record.enabled and record.name == "agents.defaults":
                    defaults.update(record.data)
        agent_overrides: dict[str, Any] = {}
        for record in default_layers[2]:
            if record.enabled and record.name == "agents.defaults":
                agent_overrides.update(record.data)
        # Go's effective runtime order applies system/user defaults first,
        # then the compatibility agent.json layer, followed by explicit
        # Agent-scoped database settings and finally agents.config.  Keeping
        # the Agent scope after agent.json prevents a stale imported system
        # file from reviving an older Provider selection.
        effective_config = {
            **defaults,
            **file_config,
            **agent_overrides,
            **agent.config,
        }
        prompt_parts: list[str] = []
        for filename in ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"):
            raw = files.get(filename)
            if not raw:
                continue
            try:
                content = raw.decode("utf-8").strip()
            except UnicodeDecodeError:
                continue
            if content:
                prompt_parts.append(f"## {filename}\n\n{content}")
        configured_soul = str(effective_config.get("soul") or "").strip()
        if configured_soul and "SOUL.md" not in files:
            prompt_parts.insert(0, f"## SOUL.md\n\n{configured_soul}")
        configured_skills = effective_config.get("skills")
        always_load = (
            configured_skills.get("alwaysLoad", []) if isinstance(configured_skills, dict) else []
        )
        skills: list[Skill] = []
        for value in always_load if isinstance(always_load, list) else []:
            name = str(value)
            try:
                skill = self.skill_catalog.require(name)
            except SkillError as exc:
                self._skill_errors[f"{agent.id}:{name}"] = str(exc)
                continue
            self._skill_errors.pop(f"{agent.id}:{name}", None)
            skills.append(skill)
            prompt_parts.append(
                self.skill_catalog.prompt(
                    skill,
                    source_root=self.config.legacy_data_root,
                    target_root=self.config.data_root,
                )
            )
        policy = str(effective_config.get("policy") or "").strip()
        configured_allowed_tools = effective_config.get("allowedTools")
        if policy == "no-tools":
            allowed_tools: frozenset[str] | None = frozenset()
        elif policy == "delegate-only":
            allowed_tools = frozenset({"spawn_subagent"})
        else:
            configured = configured_allowed_tools
            if isinstance(configured, list):
                allowed_tools = frozenset(str(item) for item in configured)
            elif skills:
                allowed = {"exec", "read_file", "web_fetch", "list_dir", "write_file"}
                if any(skill.name.startswith("findata-toolkit") for skill in skills):
                    allowed.update(
                        tool.definition.function.name for tool in self.plugin_manager.tools()
                    )
                allowed_tools = frozenset(allowed)
            elif "coordinator" in agent.name.lower():
                coordinator_tools = {"spawn_subagent"}
                if agent.name.lower() == "coordinator-wc":
                    coordinator_tools.add("worldcup_ledger")
                allowed_tools = frozenset(coordinator_tools)
            else:
                allowed_tools = frozenset({"read_file", "web_fetch", "list_dir", "write_file"})
        if allowed_tools is None or "spawn_subagent" in allowed_tools:
            prompt_parts.append(_DELEGATION_TOOL_PROMPT.strip())
        for team, members in team_members:
            current = next((member for member in members if member.agent_id == agent.id), None)
            if current is None:
                continue
            roster = "\n".join(
                f"- {member.role_key}: "
                f"{(roster_agents[member.agent_id] or agent).name} ({member.agent_id})"
                for member in members
                if member.status == "active"
            )
            prompt_parts.append(
                "## Team roster\n\n"
                f"Team: {team.name}\nStatus: {team.status}\n{roster}\n\n"
                "Only the coordinator may delegate, and only to active specialists in this roster."
            )
            if current.member_type == "coordinator":
                effective_config["teamSpecialistIds"] = [
                    member.agent_id
                    for member in members
                    if member.member_type == "specialist" and member.status == "active"
                ]
                data_member = next(
                    (
                        member
                        for member in members
                        if member.member_type == "specialist"
                        and member.role_key == "data-analyst"
                        and member.status == "active"
                    ),
                    None,
                )
                if data_member is not None:
                    effective_config["teamDataAgentId"] = data_member.agent_id
            break
        competition_team_tools = {"football_data", "football_odds", "football_context"}
        is_competition_team = (
            isinstance(configured_allowed_tools, list)
            and bool(competition_team_tools.intersection(configured_allowed_tools))
        )
        role_key = str(effective_config.get("teamRole") or "")
        if is_competition_team and role_key in {
            "data-analyst",
            "odds-analyst",
            "tactics-analyst",
            "history-analyst",
            "risk-officer",
            "ev-analyst",
        }:
            if role_key == "data-analyst":
                effective_config["footballDataMode"] = "data"
                allowed_tools = frozenset({"football_data", "football_context"})
            elif role_key == "odds-analyst":
                effective_config["footballDataMode"] = "odds"
                allowed_tools = frozenset({"football_odds", "football_context"})
            elif role_key == "ev-analyst":
                effective_config["footballDataMode"] = "ev"
                allowed_tools = frozenset({"football_data", "football_context"})
            else:
                effective_config["footballDataMode"] = "context"
                allowed_tools = frozenset({"football_context"})
        if is_competition_team and role_key == "coordinator":
            prompt_parts.append(
                "## Football evidence order\n\n"
                "Delegate the data-analyst first and wait for its base_evidence result. "
                "Only after that result succeeds may you delegate tactics, odds, history, "
                "or risk. Those specialists receive the shared evidence and must not repeat "
                "the primary football-data lookup. EV is optional and must only be delegated "
                "when the original user explicitly requests odds, market prices, betting, "
                "Sporttery, EV, expected value, or sensitivity."
            )
        system_prompt = "\n\n".join(prompt_parts).replace(
            str(self.config.legacy_data_root), str(self.config.data_root)
        )
        return AgentRuntimeProfile(
            agent=agent.model_copy(update={"config": effective_config}),
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            skills=tuple(skills),
        )

    async def _plugin_settings(
        self,
    ) -> tuple[
        dict[str, dict[str, Any]],
        dict[str, dict[str, str]],
        set[str],
    ]:
        config: dict[str, Any] = {
            "finskillsPath": str(self.config.data_root / "skills"),
            "serenitySkillPath": str(self.config.data_root / "skills" / "serenity-skill"),
            "stateDbPath": str(self.config.data_root / "data" / "finance-tools.db"),
            "pythonBin": sys.executable,
            "timeoutSeconds": 45,
        }
        environment: dict[str, str] = {}
        for skill_name, config_name in (
            ("findata-toolkit-us", "usPythonBin"),
            ("findata-toolkit-cn", "cnPythonBin"),
            ("serenity-skill", "serenityPythonBin"),
        ):
            try:
                skill = self.skill_catalog.require(skill_name)
            except SkillError:
                continue
            try:
                config[config_name] = str(self.skill_catalog.interpreter(skill))
            except SkillError:
                pass
            for name in skill.environment_names:
                value = os.environ.get(name)
                if value:
                    environment[name] = value
        odds_key = os.environ.get("ODDS_API_KEY")
        if odds_key:
            environment["ODDS_API_KEY"] = odds_key
        async with UnitOfWork(self.database) as unit:
            records = await unit.require_store().list_configs(
                kind="plugin", user_id="", agent_id=""
            )
        record = next((item for item in records if item.name == "finance-tools"), None)
        if record is not None:
            timeout = record.data.get("timeoutSeconds")
            if isinstance(timeout, int) and 1 <= timeout <= 300:
                config["timeoutSeconds"] = timeout
        enabled = {"finance-tools"} if record is None or record.enabled else set()
        return {"finance-tools": config}, {"finance-tools": environment}, enabled

    @staticmethod
    def _request(profile: AgentRuntimeProfile, model: str, message: str) -> AgentRunRequest:
        config = profile.agent.config
        configured_max_failed_tool_rounds = config.get("maxFailedToolRounds")
        if configured_max_failed_tool_rounds is None:
            # Older persisted football teams predate the fail-closed specialist
            # policy.  Their profiles do not have maxFailedToolRounds, so keep
            # an unavailable external data source from consuming the entire
            # coordinator delegation budget with model retries.  An explicit
            # value always wins, including 0 for an intentionally unlimited
            # retry policy.
            default_max_failed_tool_rounds = int(
                config.get("teamMemberType") == "specialist"
                and profile.allowed_tools is not None
                and "football_data" in profile.allowed_tools
            )
        else:
            default_max_failed_tool_rounds = int(configured_max_failed_tool_rounds or 0)
        return AgentRunRequest(
            model=model,
            message=message,
            system_prompt=profile.system_prompt or str(config.get("soul") or ""),
            allowed_tools=profile.allowed_tools,
            max_rounds=int(config.get("maxToolIterations") or 8),
            max_tokens=int(config.get("maxTokens") or 4096),
            temperature=float(
                config["temperature"] if config.get("temperature") is not None else 0.7
            ),
            thinking_budget_tokens=(
                int(config["thinkingBudgetTokens"]) if config.get("thinkingBudgetTokens") else None
            ),
            delegation_timeout=float(config.get("delegationTimeoutSeconds") or 120),
            max_failed_tool_rounds=default_max_failed_tool_rounds,
            scope_guard=str(config.get("scopeGuard") or ""),
        )

    @staticmethod
    def provider_environment_key(name: str) -> str:
        normalized = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
        return os.environ.get(f"FASTCLAW_PROVIDER_{normalized}_API_KEY", "")

    def provider_credential(
        self,
        name: str,
        data: Mapping[str, Any] | None = None,
        *,
        credential_context: str = "",
    ) -> str:
        configured = self.provider_environment_key(name)
        if configured:
            return configured
        if name == self.config.default_provider_name:
            configured = self.config.default_provider_api_key
            if configured:
                return configured
        encrypted = str((data or {}).get("encryptedApiKey") or "")
        if encrypted:
            return self.credential_cipher.decrypt(encrypted, context=credential_context)
        return ""

    @staticmethod
    def safe_error(error: BaseException) -> str:
        if isinstance(error, (LookupError, RuntimeError, AgentRunError)):
            return str(error).replace("\n", " ")[:240]
        return "agent execution failed"

    def _require_running(self) -> None:
        if not self.started or self.runtime.state is not RuntimeState.RUNNING:
            raise RuntimeError("Agent runtime manager is not running")

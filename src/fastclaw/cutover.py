"""Read-only release audit for a prepared FastClaw data copy."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from fastclaw.agent.manager import AgentRuntimeManager, AgentRuntimeProfile
from fastclaw.storage import AgentFileRecord, AgentRecord, Database, UnitOfWork

_ROLE_FILES = ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md")

_PRODUCTION_AGENTS = frozenset(
    {
        "LEO",
        "coordinator",
        "coordinator-wc",
        "data-analyst",
        "ev-analyst",
        "history-analyst",
        "news-analyst",
        "news-analyst-us",
        "odds-analyst",
        "risk-officer",
        "stock-screener",
        "stock-screener-us",
        "tactics-analyst",
    }
)
_BENCHMARK_AGENTS = frozenset(
    {
        "Benchmark Investigator",
        "Benchmark Observer",
        "Benchmark Operator",
        "Benchmark Policy",
        "Finance Accounting Analyst",
        "Finance Governance Specialist",
        "Finance Methodology Specialist",
        "Finance Research Coordinator",
        "Finance Retrieval Specialist",
        "Finance Risk Analyst",
        "Finance Solo Researcher",
        "Finance Source Specialist",
        "Finance Trend Analyst",
        "Runtime Benchmark Coordinator",
    }
)


@dataclass(frozen=True, slots=True)
class CutoverExpectations:
    """Dataset-specific expectations; no secrets or mutable state are included."""

    agents_by_username: Mapping[str, frozenset[str]]
    required_role_profile_count: int
    default_model_agents: frozenset[tuple[str, str]] = frozenset()
    require_sessions: bool = True
    require_all_providers: bool = True
    require_odds_when_declared: bool = True
    require_plugins: bool = True


PRODUCTION_27_EXPECTATIONS = CutoverExpectations(
    agents_by_username=MappingProxyType(
        {
            "M0yuW": _PRODUCTION_AGENTS,
            "fastclaw-runtime-benchmark": _BENCHMARK_AGENTS,
        }
    ),
    required_role_profile_count=26,
    default_model_agents=frozenset({("M0yuW", "LEO")}),
)


class AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentAudit(AuditModel):
    id: str
    username: str
    name: str
    model: str
    model_source: Literal["database", "agent-file", "system-default", "missing"]
    policy: Literal["no-tools", "delegate-only", "custom"]
    tools: tuple[str, ...]
    role_files: tuple[str, ...]
    skills: tuple[str, ...]
    skills_prepared: bool
    provider: str
    provider_configured: bool


class PluginAudit(AuditModel):
    id: str
    enabled: bool
    running: bool
    error: str
    tools: tuple[str, ...]


class CutoverAuditReport(AuditModel):
    ready: bool
    counts: dict[str, int]
    agents_by_username: dict[str, int]
    providers: dict[str, bool]
    odds_required: bool
    odds_configured: bool
    agents: tuple[AgentAudit, ...]
    plugins: tuple[PluginAudit, ...]
    blockers: tuple[str, ...]


async def audit_cutover(
    database: Database,
    manager: AgentRuntimeManager,
    *,
    expectations: CutoverExpectations = PRODUCTION_27_EXPECTATIONS,
) -> CutoverAuditReport:
    """Inspect an already-started manager backed by a disposable data copy."""

    if not manager.started:
        raise RuntimeError("cutover audit requires a started AgentRuntimeManager")

    async with UnitOfWork(database) as unit:
        store = unit.require_store()
        users = tuple(await store.list_users())
        agent_records: list[AgentRecord] = []
        for user in users:
            agent_records.extend(await store.list_agents(user.id))
        agents = tuple(agent_records)
        records_by_agent = {
            agent.id: tuple(await store.list_agent_files(agent.id, agent.user_id))
            for agent in agents
        }

    counts = await _database_counts(database)
    usernames = {user.id: user.username for user in users}
    agent_audits: list[AgentAudit] = []
    provider_status: dict[str, bool] = {}
    for agent in sorted(agents, key=lambda item: (usernames[item.user_id], item.name, item.id)):
        profile = manager.profiles[agent.id]
        files = _profile_files(manager, agent, records_by_agent[agent.id])
        model = str(profile.agent.config.get("model") or "")
        model_source = _model_source(agent, files) if model else "missing"
        provider_name = model.split("/", 1)[0] if "/" in model else ""
        try:
            selected = await manager.provider_selection(profile)
        except RuntimeError:
            configured = False
        else:
            configured = True
            provider_name = selected.name
        if provider_name:
            provider_status[provider_name] = provider_status.get(provider_name, True) and configured
        skills_prepared = all(manager.skill_catalog.is_prepared(skill) for skill in profile.skills)
        agent_audits.append(
            AgentAudit(
                id=agent.id,
                username=usernames[agent.user_id],
                name=agent.name,
                model=model,
                model_source=model_source,
                policy=_policy(profile),
                tools=tuple(sorted(profile.allowed_tools or ())),
                role_files=tuple(name for name in _ROLE_FILES if files.get(name)),
                skills=tuple(skill.name for skill in profile.skills),
                skills_prepared=skills_prepared,
                provider=provider_name,
                provider_configured=configured,
            )
        )

    plugins = tuple(
        PluginAudit(
            id=instance.manifest.id,
            enabled=instance.enabled,
            running=instance.process.running,
            error=instance.error,
            tools=tuple(tool.name for tool in instance.tools),
        )
        for instance in manager.plugin_manager.instances
    )
    odds_required = any(
        "ODDS_API_KEY" in skill.environment_names
        for profile in manager.profiles.values()
        for skill in profile.skills
    )
    odds_configured = bool(os.environ.get("ODDS_API_KEY"))
    agents_by_username = {
        username: sum(item.username == username for item in agent_audits)
        for username in sorted(set(usernames.values()))
    }
    blockers = _blockers(
        counts=counts,
        agents=tuple(agent_audits),
        plugins=plugins,
        provider_status=provider_status,
        odds_required=odds_required,
        odds_configured=odds_configured,
        expectations=expectations,
    )
    return CutoverAuditReport(
        ready=not blockers,
        counts=counts,
        agents_by_username=agents_by_username,
        providers=dict(sorted(provider_status.items())),
        odds_required=odds_required,
        odds_configured=odds_configured,
        agents=tuple(agent_audits),
        plugins=plugins,
        blockers=blockers,
    )


async def _database_counts(database: Database) -> dict[str, int]:
    tables = {
        "users": "users",
        "agents": "agents",
        "sessions": "sessions",
        "agentFiles": "agent_files",
        "configs": "configs",
        "apiKeys": "apikeys",
        "apiKeyAclEdges": "apikey_agents",
    }
    async with database.session() as session:
        counts = {
            label: int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)
            for label, table in tables.items()
        }
        counts["foreignKeyErrors"] = len(await database.sqlite_integrity_errors(session))
        counts["channelCredentialKeys"] = int(
            await session.scalar(
                text("SELECT count(*) FROM configs WHERE kind = 'channel' AND credential_key <> ''")
            )
            or 0
        )
    return counts


def _profile_files(
    manager: AgentRuntimeManager,
    agent: AgentRecord,
    records: tuple[AgentFileRecord, ...],
) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    root = manager.config.data_root / "agents" / agent.id
    for name in ("agent.json", *_ROLE_FILES):
        path = root / name
        if path.is_file() and not path.is_symlink():
            files[name] = path.read_bytes()
    for record in records:
        filename = record.filename
        if filename in {"agent.json", *_ROLE_FILES}:
            files[filename] = record.data
    return files


def _model_source(
    agent: AgentRecord, files: Mapping[str, bytes]
) -> Literal["database", "agent-file", "system-default", "missing"]:
    if agent.config.get("model"):
        return "database"
    raw = files.get("agent.json")
    if raw:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("model"):
            return "agent-file"
    return "system-default"


def _policy(profile: AgentRuntimeProfile) -> Literal["no-tools", "delegate-only", "custom"]:
    tools = profile.allowed_tools
    if not tools:
        return "no-tools"
    if tools == frozenset({"spawn_subagent"}):
        return "delegate-only"
    return "custom"


def _blockers(
    *,
    counts: Mapping[str, int],
    agents: tuple[AgentAudit, ...],
    plugins: tuple[PluginAudit, ...],
    provider_status: Mapping[str, bool],
    odds_required: bool,
    odds_configured: bool,
    expectations: CutoverExpectations,
) -> tuple[str, ...]:
    blockers: list[str] = []
    actual = {
        username: frozenset(item.name for item in agents if item.username == username)
        for username in {item.username for item in agents}
    }
    for username, expected_names in expectations.agents_by_username.items():
        if actual.get(username, frozenset()) != expected_names:
            blockers.append(f"agent set mismatch for {username}")
    if set(actual) != set(expectations.agents_by_username):
        blockers.append("user set does not match the cutover manifest")
    if counts["foreignKeyErrors"]:
        blockers.append("database contains foreign-key violations")
    if counts["channelCredentialKeys"]:
        blockers.append("migrated channel credential_key values are not empty")
    if expectations.require_sessions and not counts["sessions"]:
        blockers.append("no migrated sessions were found")
    if any(not item.model for item in agents):
        blockers.append("one or more Agent profiles have no model")
    allowed_defaults = expectations.default_model_agents
    unexpected_defaults = [
        item
        for item in agents
        if item.model_source == "system-default"
        and (item.username, item.name) not in allowed_defaults
    ]
    if unexpected_defaults:
        blockers.append("one or more Agent profiles unexpectedly use the system default model")
    expected_defaults = {
        key
        for key in allowed_defaults
        if any(
            (item.username, item.name) == key and item.model_source == "system-default"
            for item in agents
        )
    }
    if expected_defaults != allowed_defaults:
        blockers.append("the declared default-model Agent set does not match")
    role_count = sum(bool(item.role_files) for item in agents)
    if role_count != expectations.required_role_profile_count:
        blockers.append(
            "role-file profile count is "
            f"{role_count}, expected {expectations.required_role_profile_count}"
        )
    benchmark = [item for item in agents if item.username == "fastclaw-runtime-benchmark"]
    if benchmark:
        invalid_policies = [
            item
            for item in benchmark
            if item.policy != ("delegate-only" if item.name.endswith("Coordinator") else "no-tools")
        ]
        if invalid_policies:
            blockers.append("benchmark coordinator/specialist tool policies do not match")
    if any(not item.skills_prepared for item in agents):
        blockers.append("one or more required Skill environments are not prepared")
    if expectations.require_all_providers and (
        not provider_status or not all(provider_status.values())
    ):
        blockers.append("one or more required Providers are not configured")
    if expectations.require_odds_when_declared and odds_required and not odds_configured:
        blockers.append("ODDS_API_KEY is required but not configured")
    if expectations.require_plugins and (
        not plugins or any(item.enabled and (not item.running or item.error) for item in plugins)
    ):
        blockers.append("one or more required plugins are unavailable")
    return tuple(blockers)


__all__ = [
    "PRODUCTION_27_EXPECTATIONS",
    "AgentAudit",
    "CutoverAuditReport",
    "CutoverExpectations",
    "PluginAudit",
    "audit_cutover",
]

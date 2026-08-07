"""Team templates and atomic persistence for coordinator-led Agent teams."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from fastclaw.storage import (
    AgentRecord,
    AgentTeamMemberRecord,
    AgentTeamRecord,
    Database,
    UnitOfWork,
)


class TeamValidationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TeamRole:
    key: str
    name: str
    member_type: str
    description: str = ""
    soul: str = ""
    skills: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TeamTemplate:
    key: str
    version: str
    name: str
    roles: tuple[TeamRole, ...]
    public: bool = True


FINANCE_MARKET_RESEARCH = TeamTemplate(
    "finance-market-research",
    "v1",
    "Finance market research",
    (
        TeamRole(
            "coordinator",
            "Research coordinator",
            "coordinator",
            soul=(
                "Coordinate market research. Delegate evidence collection, reconcile "
                "conflicting sources, and clearly separate facts from assumptions."
            ),
            allowed_tools=("spawn_subagent",),
        ),
        TeamRole(
            "news-analyst",
            "News analyst",
            "specialist",
            soul="Analyze mainland China market news with dated source evidence and uncertainty.",
            skills=("findata-toolkit-cn",),
            allowed_tools=("exec", "web_fetch", "finance-tools.market_events"),
        ),
        TeamRole(
            "news-analyst-us",
            "US news analyst",
            "specialist",
            soul="Analyze US market news with dated source evidence and uncertainty.",
            skills=("findata-toolkit-us",),
            allowed_tools=("exec", "web_fetch", "finance-tools.market_events"),
        ),
        TeamRole(
            "stock-screener",
            "Stock screener",
            "specialist",
            soul="Screen mainland China securities from current, cited market and financial data.",
            skills=("findata-toolkit-cn",),
            allowed_tools=(
                "exec",
                "web_fetch",
                "finance-tools.stock_snapshot",
                "finance-tools.screen_stocks",
            ),
        ),
        TeamRole(
            "stock-screener-us",
            "US stock screener",
            "specialist",
            soul="Screen US securities from current, cited market and financial data.",
            skills=("findata-toolkit-us",),
            allowed_tools=(
                "exec",
                "web_fetch",
                "finance-tools.stock_snapshot",
                "finance-tools.screen_stocks",
            ),
        ),
    ),
)
WORLD_CUP_ANALYSIS = TeamTemplate(
    "world-cup-analysis",
    "v1",
    "World Cup analysis",
    (
        TeamRole(
            "coordinator",
            "World Cup coordinator",
            "coordinator",
            soul=(
                "Coordinate World Cup analysis. Delegate factual research, keep predictions "
                "conditional, and synthesize specialist evidence without inventing live facts."
            ),
            allowed_tools=("spawn_subagent", "worldcup_ledger"),
        ),
        TeamRole(
            "data-analyst",
            "Data analyst",
            "specialist",
            soul=(
                "Analyze match data and state the date, source, and limits of every factual claim."
            ),
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
        TeamRole(
            "tactics-analyst",
            "Tactics analyst",
            "specialist",
            soul="Analyze tactical matchups from observed team style and current availability.",
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
        TeamRole(
            "odds-analyst",
            "Odds analyst",
            "specialist",
            soul=(
                "Analyze odds, implied probabilities, and uncertainty without presenting "
                "betting advice."
            ),
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
        TeamRole(
            "history-analyst",
            "History analyst",
            "specialist",
            soul=(
                "Analyze relevant historical evidence and distinguish it from current-team "
                "evidence."
            ),
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
        TeamRole(
            "risk-officer",
            "Risk officer",
            "specialist",
            soul="Identify uncertainty, missing information, and risk controls in the analysis.",
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
        TeamRole(
            "ev-analyst",
            "EV analyst",
            "specialist",
            soul="Assess expected-value assumptions and sensitivity; never claim certainty.",
            skills=("match-data-toolkit",),
            allowed_tools=("exec", "web_fetch"),
        ),
    ),
)
BENCHMARK_FINANCE = TeamTemplate(
    "benchmark-finance",
    "v1",
    "Benchmark finance",
    (
        TeamRole(
            "coordinator",
            "Finance Research Coordinator",
            "coordinator",
            allowed_tools=("spawn_subagent",),
        ),
        TeamRole("accounting", "Finance Accounting Analyst", "specialist"),
        TeamRole("governance", "Finance Governance Specialist", "specialist"),
        TeamRole("methodology", "Finance Methodology Specialist", "specialist"),
        TeamRole("retriever", "Finance Retrieval Specialist", "specialist"),
        TeamRole("risk", "Finance Risk Analyst", "specialist"),
    ),
    public=False,
)
BENCHMARK_RUNTIME = TeamTemplate(
    "benchmark-runtime",
    "v1",
    "Benchmark runtime",
    (
        TeamRole(
            "coordinator",
            "Runtime Benchmark Coordinator",
            "coordinator",
            allowed_tools=("spawn_subagent",),
        ),
        TeamRole("investigator", "Benchmark Investigator", "specialist"),
        TeamRole("observer", "Benchmark Observer", "specialist"),
        TeamRole("operator", "Benchmark Operator", "specialist"),
        TeamRole("policy", "Benchmark Policy", "specialist"),
    ),
    public=False,
)
_TEMPLATES = {
    template.key: template
    for template in (
        FINANCE_MARKET_RESEARCH,
        WORLD_CUP_ANALYSIS,
        BENCHMARK_FINANCE,
        BENCHMARK_RUNTIME,
    )
}


def public_templates() -> tuple[TeamTemplate, ...]:
    return tuple(template for template in _TEMPLATES.values() if template.public)


def resolve_template(key: str, custom_roles: Sequence[TeamRole] = ()) -> TeamTemplate:
    if key == "custom":
        return TeamTemplate("custom", "v1", "Custom", tuple(custom_roles))
    try:
        return _TEMPLATES[key]
    except KeyError as exc:
        raise TeamValidationError(f"unknown team template: {key}") from exc


def validate_roles(roles: Sequence[TeamRole]) -> None:
    coordinators = [role for role in roles if role.member_type == "coordinator"]
    specialists = [role for role in roles if role.member_type == "specialist"]
    keys = [role.key for role in roles]
    if len(coordinators) != 1:
        raise TeamValidationError("a team must contain exactly one coordinator")
    if not 1 <= len(specialists) <= 12:
        raise TeamValidationError("a team must contain between 1 and 12 specialists")
    if len(keys) != len(set(keys)) or any(not key.strip() for key in keys):
        raise TeamValidationError("team role keys must be non-empty and unique")


class TeamService:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def create(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        template_key: str,
        client_request_id: str,
        model: str = "",
        provider_name: str = "",
        custom_roles: Sequence[TeamRole] = (),
    ) -> tuple[AgentTeamRecord, tuple[AgentTeamMemberRecord, ...]]:
        if not name.strip() or not client_request_id.strip():
            raise TeamValidationError("team name and clientRequestId are required")
        template = resolve_template(template_key, custom_roles)
        validate_roles(template.roles)
        try:
            return await self._create_once(
                user_id=user_id,
                name=name,
                description=description,
                template=template,
                client_request_id=client_request_id,
                model=model,
                provider_name=provider_name,
            )
        except IntegrityError:
            # The unique (user_id, client_request_id) constraint resolves a
            # concurrent retry to the first committed team rather than leaking
            # a database conflict to the caller.
            async with UnitOfWork(self.database) as unit:
                store = unit.require_store()
                existing = await store.get_team_by_request(user_id, client_request_id)
                if existing is not None:
                    return existing, tuple(await store.list_team_members(existing.id))
            raise

    async def _create_once(
        self,
        *,
        user_id: str,
        name: str,
        description: str,
        template: TeamTemplate,
        client_request_id: str,
        model: str,
        provider_name: str,
    ) -> tuple[AgentTeamRecord, tuple[AgentTeamMemberRecord, ...]]:
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            existing = await store.get_team_by_request(user_id, client_request_id)
            if existing is not None:
                return existing, tuple(await store.list_team_members(existing.id))
            now = datetime.now(UTC)
            team = AgentTeamRecord(
                id=f"team_{uuid4().hex}",
                user_id=user_id,
                name=name.strip(),
                description=description,
                template_key=template.key,
                template_version=template.version,
                status="provisioning",
                client_request_id=client_request_id,
                created_at=now,
                updated_at=now,
            )
            await store.save_team(team)
            members: list[AgentTeamMemberRecord] = []
            for position, role in enumerate(template.roles):
                agent = AgentRecord(
                    id=f"agt_{uuid4().hex[:20]}",
                    user_id=user_id,
                    name=role.name,
                    config={
                        **({"model": model} if model else {}),
                        **({"provider": provider_name} if provider_name else {}),
                        "description": role.description,
                        "soul": role.soul,
                        **({"skills": {"alwaysLoad": list(role.skills)}} if role.skills else {}),
                        "teamRole": role.key,
                        "teamMemberType": role.member_type,
                        **(
                            {"allowedTools": list(role.allowed_tools)} if role.allowed_tools else {}
                        ),
                    },
                    created_at=now,
                    updated_at=now,
                )
                await store.save_agent(agent)
                member = AgentTeamMemberRecord(
                    team_id=team.id,
                    agent_id=agent.id,
                    role_key=role.key,
                    member_type=role.member_type,
                    display_order=position,
                )
                await store.save_team_member(member)
                members.append(member)
            team = team.model_copy(update={"status": "active", "updated_at": datetime.now(UTC)})
            await store.save_team(team)
            return team, tuple(members)

    async def add_specialist(
        self, *, team_id: str, user_id: str, revision: int, role: TeamRole, model: str = ""
    ) -> tuple[AgentTeamRecord, AgentTeamMemberRecord]:
        if role.member_type != "specialist" or not role.key.strip() or not role.name.strip():
            raise TeamValidationError("a specialist requires a non-empty role key and name")
        async with UnitOfWork(self.database) as unit:
            store = unit.require_store()
            team = await store.get_team(team_id)
            if team is None or team.user_id != user_id:
                raise LookupError("team not found")
            if team.revision != revision:
                raise TeamValidationError("team revision conflict")
            if team.status != "active":
                raise TeamValidationError("archived teams cannot add members")
            members = await store.list_team_members(team_id)
            if len([member for member in members if member.member_type == "specialist"]) >= 12:
                raise TeamValidationError("a team cannot contain more than 12 specialists")
            if any(member.role_key == role.key for member in members):
                raise TeamValidationError("team role key already exists")
            now = datetime.now(UTC)
            agent = AgentRecord(
                id=f"agt_{uuid4().hex[:20]}",
                user_id=user_id,
                name=role.name,
                config={
                    **({"model": model} if model else {}),
                    "description": role.description,
                    "soul": role.soul,
                    "teamRole": role.key,
                    "teamMemberType": "specialist",
                },
                created_at=now,
                updated_at=now,
            )
            await store.save_agent(agent)
            member = AgentTeamMemberRecord(
                team_id=team_id,
                agent_id=agent.id,
                role_key=role.key,
                member_type="specialist",
                display_order=len(members),
            )
            await store.save_team_member(member)
            updated = team.model_copy(update={"revision": team.revision + 1, "updated_at": now})
            await store.save_team(updated)
            return updated, member

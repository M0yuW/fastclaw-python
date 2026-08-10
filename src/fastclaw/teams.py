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
    delegation_timeout_seconds: int | None = None
    max_failed_tool_rounds: int | None = None
    scope_guard: str = ""


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
FOOTBALL_COMPETITION_ANALYSIS = TeamTemplate(
    "football-competition-analysis",
    "v1",
    "Football competition analysis",
    (
        TeamRole(
            "coordinator",
            "Football analysis coordinator",
            "coordinator",
            soul=(
                "Coordinate evidence-based analysis for any named football competition. "
                "Before delegation, resolve the competition, season or edition, stage, match, "
                "kickoff time, timezone, venue, and leg or aggregate context. Ask the user when "
                "scope is ambiguous. If competition plus date/season are missing, ask a concise "
                "clarifying question and do not call tools or predict yet. Delegate independently, "
                "reject evidence from the wrong "
                "competition or season, keep predictions conditional, and never invent live "
                "facts. If every required tool fails, stop and report that no evidence-backed "
                "prediction is available; never substitute model memory. Record predictions with "
                "football_ledger only after reconciling all specialist results."
            ),
            allowed_tools=("spawn_subagent", "football_ledger"),
            delegation_timeout_seconds=120,
            max_failed_tool_rounds=1,
            scope_guard="football",
        ),
        TeamRole(
            "data-analyst",
            "Competition data analyst",
            "specialist",
            soul=(
                "Verify the exact competition, season, stage, fixture identity, kickoff time, "
                "venue, score state, standings, and recent form. Use dated primary or reputable "
                "sources. Resolve the named competition through football_data before using a "
                "provider identifier; never construct an ESPN slug yourself. Never mix "
                "competitions or seasons, and mark unavailable data unknown. "
                "Return as_of, competition, season, match, lean, confidence, evidence, and URLs."
            ),
            allowed_tools=("football_data", "web_fetch"),
        ),
        TeamRole(
            "tactics-analyst",
            "Tactics and lineup analyst",
            "specialist",
            soul=(
                "Analyze formations, matchup mechanisms, lineup availability, rotation, and the "
                "competition format. Separate confirmed lineup or injury facts from tactical "
                "inference, cite dated sources, and account for two-leg or extra-time rules."
            ),
            allowed_tools=("football_data", "web_fetch"),
        ),
        TeamRole(
            "odds-analyst",
            "Football odds analyst",
            "specialist",
            soul=(
                "Analyze timestamped 1X2 and totals prices only for the requested fixture and "
                "competition. State bookmaker or market source, remove vig when possible, expose "
                "missing coverage, and provide price calibration rather than betting advice."
            ),
            allowed_tools=("football_data", "web_fetch"),
        ),
        TeamRole(
            "history-analyst",
            "Football history analyst",
            "specialist",
            soul=(
                "Assess relevant head-to-head and competition history without treating old squads, "
                "managers, formats, or venues as current evidence. Cite dates and sources and make "
                "the limits of historical transfer explicit."
            ),
            allowed_tools=("football_data", "web_fetch"),
        ),
        TeamRole(
            "risk-officer",
            "Football risk officer",
            "specialist",
            soul=(
                "Challenge the favored interpretation. Check source freshness, fixture identity, "
                "injuries, suspensions, fatigue, weather, travel, rotation, format rules, and data "
                "gaps. Never turn an unverified absence or rumor into a confirmed fact."
            ),
            allowed_tools=("football_data", "web_fetch"),
        ),
        TeamRole(
            "ev-analyst",
            "Football EV analyst",
            "specialist",
            soul=(
                "Compare the coordinator's stated probabilities with timestamped market prices, "
                "show assumptions and sensitivity, and report whether price already reflects the "
                "evidence. Do not change the evidence confidence and do not give stake advice."
            ),
            allowed_tools=("football_data", "web_fetch"),
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
        TeamRole("source", "Finance Source Specialist", "specialist"),
        TeamRole("trend", "Finance Trend Analyst", "specialist"),
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
        FOOTBALL_COMPETITION_ANALYSIS,
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
    unsupported_types = sorted(
        {
            role.member_type
            for role in roles
            if role.member_type not in {"coordinator", "specialist"}
        }
    )
    if unsupported_types:
        raise TeamValidationError(f"unsupported team member type: {', '.join(unsupported_types)}")
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
        specialist_model: str = "",
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
                specialist_model=specialist_model,
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
        specialist_model: str,
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
                role_model = (
                    specialist_model
                    if role.member_type == "specialist" and specialist_model
                    else model
                )
                agent = AgentRecord(
                    id=f"agt_{uuid4().hex[:20]}",
                    user_id=user_id,
                    name=role.name,
                    config={
                        **({"model": role_model} if role_model else {}),
                        **({"provider": provider_name} if provider_name else {}),
                        "description": role.description,
                        "soul": role.soul,
                        **({"skills": {"alwaysLoad": list(role.skills)}} if role.skills else {}),
                        "teamRole": role.key,
                        "teamMemberType": role.member_type,
                        **(
                            {"allowedTools": list(role.allowed_tools)} if role.allowed_tools else {}
                        ),
                        **(
                            {"delegationTimeoutSeconds": role.delegation_timeout_seconds}
                            if role.delegation_timeout_seconds is not None
                            else {}
                        ),
                        **(
                            {"maxFailedToolRounds": role.max_failed_tool_rounds}
                            if role.max_failed_tool_rounds is not None
                            else {}
                        ),
                        **({"scopeGuard": role.scope_guard} if role.scope_guard else {}),
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

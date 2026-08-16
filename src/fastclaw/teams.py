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
                "Analyze schedule, results, standings, recent form, and confirmed match facts. "
                "Use match-data-toolkit and ESPN-supported data sources, and state the date, "
                "source, and limits of every factual claim. Do not use Sporttery, betting odds, "
                "or market prices; if a fact is unavailable, mark it unknown."
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
                "Analyze timestamped bookmaker or exchange odds, implied probabilities, and "
                "uncertainty without presenting betting advice. Use web sources or "
                "odds_data.py from match-data-toolkit. Do not call Sporttery or use its "
                "getMatchCalculatorV1 endpoint."
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
            soul=(
                "Assess expected-value assumptions and sensitivity; never claim certainty. "
                "You are the only analyst role permitted to use Sporttery. Use "
                "match-data-toolkit/scripts/sporttery_data.py for official SP prices and "
                "compare them with the coordinator's model probabilities. Do not give stake "
                "advice."
            ),
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
                "Act as a general football conversation coordinator, not a prediction-only bot. "
                "Answer ordinary questions, football concepts, system behavior, source policy, "
                "and workflow questions directly without assuming that the user wants a match "
                "prediction. For non-prediction football requests, do not demand opposing teams, "
                "competition, date, season, or round merely because those details are absent. "
                "Only when the user explicitly asks for a prediction, pre-match analysis, 1X2, "
                "totals, odds, EV, or to record/correct a prediction should you resolve the "
                "competition, season or edition, stage, match, kickoff time, timezone, venue, "
                "and leg or aggregate context; ask a concise clarification only when that "
                "explicit workflow is underspecified. Delegate independently, "
                "reject evidence from the wrong "
                "competition or season, keep predictions conditional, and never invent live "
                "facts. If every required tool fails, stop and report that no evidence-backed "
                "prediction is available; never substitute model memory. Do not call prediction "
                "specialists or football_ledger for ordinary conversation. Record predictions with "
                "football_ledger only after reconciling all specialist results. For football "
                "teams, delegate the data analyst first and wait for its confirmed base evidence; "
                "only then delegate tactics, odds, history, and risk with that evidence. For a "
                "normal prediction, odds analysis is mandatory after base evidence; only an "
                "explicit user request to exclude odds may record odds_not_requested. EV is "
                "optional: do not delegate it unless the original user explicitly requests "
                "odds, market prices, betting, Sporttery, EV, expected value, or sensitivity. "
                "After reconciliation of an explicit prediction request, include a final "
                "prediction-direction section "
                "with one valid 1X2 lean per requested match, confidence, evidence basis, and "
                "invalidation conditions. If evidence is incomplete, lower confidence or use "
                "no clear edge only when no valid directional evidence exists; do not suppress "
                "a supported low-confidence lean. 1X2 must be exactly home win, draw, or away "
                "win; 1X (home-or-draw), X2, and 12 are double-chance markets and must be "
                "reported separately, never relabeled as 1X2. Under/Over is a separate totals "
                "market. When recording a prediction, put all required "
                "football_ledger append fields inside entry using competition, season, date, "
                "match, our_pred, and our_confidence. When correcting an existing row, use "
                "football_ledger update with the same competition, date, and match key; do not "
                "append a duplicate. Use settle only for actual_result or actual_score. When the "
                "user asks to view the ledger, "
                "call football_ledger report and show its fixed Markdown table directly; do "
                "not export the ledger JSON to the customer. If the user only asks to review, "
                "inspect, or 复盘 the ledger without naming a fixture, call report first using "
                "the current Agent's ledger; do not ask for teams, competition, or date. For "
                "复盘, treat report as phase one: delegate the data analyst to verify finished "
                "matches, settle only verified actual_result/actual_score fields, then call "
                "report again. Do not settle future, unfinished, unmatched, or unavailable "
                "matches. This is analysis, not stake advice."
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
                "sources. Use football_data base_evidence so TheSportsDB confirms the fixture "
                "and base data once; never provide a URL, league ID, slug, sport key, or API "
                "key. Do not use Sporttery or betting-market data. Never mix "
                "competitions or seasons, and mark unavailable data unknown. "
                "A previous report or pasted evidence is not a fresh confirmation: for every "
                "fixture in a new delegation, call football_data action=base_evidence and do "
                "not report completion until the Runtime has published the machine-readable "
                "bundle. "
                "Once base_evidence succeeds, stop calling football_data. Do not issue a "
                "second evidence/ESPN/H2H request for historical dates or adjacent divisions: "
                "the published historical_context already contains H2H searches, prior-season "
                "formal matches, promotion-boundary evidence, and preseason friendlies. "
                "A ledger review is the one explicit exception: when the task is to review "
                "or settle existing ledger rows, call football_data action=results to verify "
                "finished matches; do not call base_evidence, odds, or Sporttery, and do not "
                "settle a row without a verified final score. "
                "For a new-season opener, use the base bundle's explicitly labeled cross-season "
                "form and previous_match_details fallback; never relabel it as current-season "
                "form. Prefer historical_context.same_competition_prior_matches and "
                "historical_context.prior_division_matches for formal historical claims, and "
                "keep historical_context.preseason_friendlies as weak warm-up context. "
                "Return as_of, competition, season, match, lean, confidence, evidence, and "
                "URLs."
            ),
            allowed_tools=("football_data", "football_context"),
            max_failed_tool_rounds=1,
        ),
        TeamRole(
            "tactics-analyst",
            "Tactics and lineup analyst",
            "specialist",
            soul=(
                "Analyze formations, matchup mechanisms, lineup availability, rotation, and the "
                "competition format. Read football_context first and reuse its confirmed fixture "
                "and base evidence. Use previous_match_details as historical context only; it "
                "does not prove the upcoming lineup or current injuries. Do not open arbitrary web "
                "pages or re-query TheSportsDB; if current lineup or injury evidence is absent, "
                "mark it unknown. Separate confirmed facts from tactical inference and account "
                "for two-leg or extra-time rules."
            ),
            allowed_tools=("football_context",),
            max_failed_tool_rounds=1,
        ),
        TeamRole(
            "odds-analyst",
            "Football odds analyst",
            "specialist",
            soul=(
                "Analyze timestamped 1X2 and totals prices only for the requested fixture and "
                "competition. State bookmaker or market source, remove vig when possible, expose "
                "missing coverage, and provide price calibration rather than betting advice. "
                "Read football_context first, then use football_odds for the independent Odds "
                "API market request. Do not call TheSportsDB or Sporttery, and do not let odds "
                "redefine the primary fixture."
            ),
            allowed_tools=("football_odds", "football_context"),
            max_failed_tool_rounds=1,
        ),
        TeamRole(
            "history-analyst",
            "Football history analyst",
            "specialist",
            soul=(
                "Assess relevant head-to-head and competition history without treating old squads, "
                "managers, formats, or venues as current evidence. Read football_context first and "
                "reuse its confirmed fixture and base data, especially historical_context. For a "
                "new-season opener, use same_competition_prior_matches and prior_division_matches "
                "for formal historical evidence; use preseason_friendlies only as a separately "
                "labeled weak context. Never call a friendly a current-season form result, and "
                "never infer promotion/relegation from team reputation or league absence: use "
                "competition_boundary only when it says source_supported_*. Do not open arbitrary "
                "web pages or re-query TheSportsDB; if historical evidence is absent, mark it "
                "unknown. Cite dates and competitions already present in shared evidence and "
                "make the limits of historical transfer explicit. If the shared historical_context "
                "is present, do not call football_context again; proceed from that evidence."
            ),
            allowed_tools=("football_context",),
            max_failed_tool_rounds=1,
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
            allowed_tools=("football_context",),
            max_failed_tool_rounds=1,
        ),
        TeamRole(
            "ev-analyst",
            "Football EV analyst",
            "specialist",
            soul=(
                "Compare the coordinator's stated probabilities with timestamped market prices, "
                "show assumptions and sensitivity, and report whether price already reflects the "
                "evidence. You are only invoked when the user explicitly asks for odds, market "
                "prices, Sporttery, EV, expected value, or sensitivity. You are the only analyst "
                "role permitted to use Sporttery; use the Runtime-managed source for official SP "
                "prices. Do not open arbitrary web pages, change evidence confidence, or give "
                "stake advice."
            ),
            allowed_tools=("football_data", "football_context"),
            max_failed_tool_rounds=1,
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

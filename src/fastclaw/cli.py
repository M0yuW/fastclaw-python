"""FastClaw command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

import typer

from fastclaw.agent.manager import AgentRuntimeConfig, AgentRuntimeManager
from fastclaw.cutover import audit_cutover
from fastclaw.migration import AssetImportConflictError, import_assets, import_go_database
from fastclaw.runtime import Runtime
from fastclaw.skills import SkillCatalog
from fastclaw.storage import AgentTeamMemberRecord, AgentTeamRecord, Database
from fastclaw.teams import TeamValidationError, resolve_template

_DEFAULT_DATA_ROOT = Path.home() / ".fastclaw-python"

app = typer.Typer(help="FastClaw Python runtime tools.")
migrate_app = typer.Typer(help="One-way data migration commands.")
skills_app = typer.Typer(help="Discover and explicitly prepare Skill environments.")
providers_app = typer.Typer(help="Check centralized Provider and Skill credentials.")
cutover_app = typer.Typer(help="Audit a disposable copy before release cutover.")
app.add_typer(migrate_app, name="migrate")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")
app.add_typer(cutover_app, name="cutover")


@migrate_app.command("import-go")
def import_go(
    source: Annotated[Path, typer.Option(exists=True, dir_okay=False, resolve_path=True)],
    target: Annotated[str, typer.Option(help="Async SQLAlchemy target URL.")],
    dry_run: Annotated[
        bool, typer.Option(help="Inspect without creating or updating the target.")
    ] = False,
    orphan_policy: Annotated[
        Literal["reject", "quarantine"],
        typer.Option(help="Reject orphan rows or quarantine their identifiers."),
    ] = "reject",
) -> None:
    """Import a Go FastClaw SQLite database into an independent target."""

    report = asyncio.run(
        import_go_database(
            source=source,
            target_url=target,
            dry_run=dry_run,
            orphan_policy=orphan_policy,
        )
    )
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


@migrate_app.command("import-assets")
def import_runtime_assets(
    source_root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
    target_root: Annotated[Path, typer.Option(file_okay=False, resolve_path=True)],
    database_url: Annotated[str, typer.Option(help="Target Python database URL.")],
    dry_run: Annotated[bool, typer.Option(help="Inspect without writing target assets.")] = False,
) -> None:
    """Copy only valid Agent/workspace assets and shared Skills from Go storage."""

    try:
        report = asyncio.run(
            import_assets(
                source_root=source_root,
                target_root=target_root,
                database_url=database_url,
                dry_run=dry_run,
            )
        )
    except AssetImportConflictError as exc:
        typer.echo(json.dumps(exc.report.model_dump(mode="json"), indent=2, sort_keys=True))
        raise typer.Exit(code=2) from exc
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))


@migrate_app.command("backfill-teams")
def backfill_teams(
    database_url: Annotated[str, typer.Option(help="Python database URL.")],
    dry_run: Annotated[bool, typer.Option(help="Report candidate teams without writing.")] = False,
) -> None:
    """Idempotently group known imported finance and World Cup teams by their Agent names."""

    async def run() -> dict[str, object]:
        database = Database(database_url)
        await database.create_schema()
        manifest: list[dict[str, object]] = []
        try:
            from fastclaw.storage import UnitOfWork

            async with UnitOfWork(database) as unit:
                store = unit.require_store()
                users = await store.list_users()
                for user in users:
                    agents = await store.list_agents(user.id)
                    candidates = {
                        "finance-market-research": (
                            "coordinator",
                            "news-analyst",
                            "news-analyst-us",
                            "stock-screener",
                            "stock-screener-us",
                        ),
                        "world-cup-analysis": (
                            "coordinator-wc",
                            "data-analyst",
                            "tactics-analyst",
                            "odds-analyst",
                            "history-analyst",
                            "risk-officer",
                            "ev-analyst",
                        ),
                        "benchmark-finance": (
                            "Finance Research Coordinator",
                            "Finance Accounting Analyst",
                            "Finance Governance Specialist",
                            "Finance Methodology Specialist",
                            "Finance Retrieval Specialist",
                            "Finance Risk Analyst",
                        ),
                        "benchmark-runtime": (
                            "Runtime Benchmark Coordinator",
                            "Benchmark Investigator",
                            "Benchmark Observer",
                            "Benchmark Operator",
                            "Benchmark Policy",
                        ),
                    }
                    by_name = {agent.name.casefold(): agent for agent in agents}
                    for key, names in candidates.items():
                        template = resolve_template(key)
                        selected = [by_name.get(name.casefold()) for name in names]
                        if any(agent is None for agent in selected):
                            manifest.append(
                                {"userId": user.id, "template": key, "status": "conflict"}
                            )
                            continue
                        assigned = [agent for agent in selected if agent is not None]
                        coordinator = assigned[0]
                        request_id = f"backfill:{key}:{user.id}"
                        existing = await store.get_team_by_request(user.id, request_id)
                        entry: dict[str, object] = {
                            "userId": user.id,
                            "template": key,
                            "agentIds": [agent.id for agent in assigned],
                            "status": "existing" if existing else "candidate",
                        }
                        manifest.append(entry)
                        if existing is None and not dry_run:
                            now = datetime.now(UTC)
                            team = AgentTeamRecord(
                                id=f"team_{uuid4().hex}",
                                user_id=user.id,
                                name=template.name,
                                template_key=key,
                                template_version=template.version,
                                status="active",
                                client_request_id=request_id,
                                created_at=now,
                                updated_at=now,
                            )
                            await store.save_team(team)
                            await store.save_team_member(
                                AgentTeamMemberRecord(
                                    team_id=team.id,
                                    agent_id=coordinator.id,
                                    role_key="coordinator",
                                    member_type="coordinator",
                                )
                            )
                            for order, (role, agent) in enumerate(
                                zip(template.roles[1:], assigned[1:], strict=True), 1
                            ):
                                await store.save_team_member(
                                    AgentTeamMemberRecord(
                                        team_id=team.id,
                                        agent_id=agent.id,
                                        role_key=role.key,
                                        member_type="specialist",
                                        display_order=order,
                                    )
                                )
                            entry["status"] = "created"
            return {"dryRun": dry_run, "manifest": manifest, "count": len(manifest)}
        finally:
            await database.close()

    try:
        payload = asyncio.run(run())
    except TeamValidationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@skills_app.command("list")
def list_skills(
    data_root: Annotated[Path, typer.Option(file_okay=False)] = _DEFAULT_DATA_ROOT,
) -> None:
    """List installed Skills by frontmatter name and preparation state."""

    catalog = SkillCatalog(data_root / "skills")
    skills = catalog.discover()
    typer.echo(
        json.dumps(
            [
                {
                    "name": skill.name,
                    "directory": skill.root.name,
                    "requirementsHash": skill.requirements_hash,
                    "prepared": catalog.is_prepared(skill),
                    "environment": list(skill.environment_names),
                }
                for skill in skills
            ],
            indent=2,
            sort_keys=True,
        )
    )


@skills_app.command("prepare")
def prepare_skills(
    names: Annotated[list[str] | None, typer.Argument()] = None,
    data_root: Annotated[Path, typer.Option(file_okay=False)] = _DEFAULT_DATA_ROOT,
) -> None:
    """Create requirements-hashed Skill environments explicitly."""

    catalog = SkillCatalog(data_root / "skills")
    installed = catalog.discover()
    selected = installed if not names else tuple(catalog.require(name) for name in names)

    async def prepare_all() -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        for skill in selected:
            path = await catalog.prepare(skill)
            results.append({"name": skill.name, "path": str(path)})
        return results

    typer.echo(json.dumps(asyncio.run(prepare_all()), indent=2, sort_keys=True))


@providers_app.command("check")
def check_providers(
    database_url: Annotated[str, typer.Option(help="Python database URL.")],
    data_root: Annotated[Path, typer.Option(file_okay=False)] = _DEFAULT_DATA_ROOT,
) -> None:
    """Report centralized credential requirements without printing secret values."""

    async def inspect() -> dict[str, object]:
        database = Database(database_url)
        runtime = Runtime()
        manager = AgentRuntimeManager(
            database,
            runtime,
            AgentRuntimeConfig(data_root=data_root, enable_plugins=False),
        )
        await runtime.start()
        try:
            await manager.start()
            providers: dict[str, dict[str, object]] = {}
            for profile in manager.profiles.values():
                model = str(profile.agent.config.get("model") or "")
                name = model.split("/", 1)[0] if "/" in model else ""
                if not name or name in providers:
                    continue
                try:
                    selected = await manager.provider_selection(profile)
                except RuntimeError:
                    configured = False
                    source = ""
                else:
                    configured = True
                    source = selected.source
                env_name = f"FASTCLAW_PROVIDER_{name.upper().replace('-', '_')}_API_KEY"
                providers[name] = {
                    "configured": configured,
                    "source": source,
                    "environment": env_name,
                }
            odds_required = any(
                skill.name == "match-data-toolkit"
                for profile in manager.profiles.values()
                for skill in profile.skills
            )
            required_skills = {
                skill.name for profile in manager.profiles.values() for skill in profile.skills
            }
            return {
                "agentProfiles": manager.profile_count,
                "providers": providers,
                "odds": {
                    "required": odds_required,
                    "configured": bool(os.environ.get("ODDS_API_KEY")),
                    "environment": "ODDS_API_KEY",
                },
                "skills": {
                    skill.name: {
                        "required": skill.name in required_skills,
                        "prepared": manager.skill_catalog.is_prepared(skill),
                    }
                    for skill in manager.skill_catalog.skills
                },
            }
        finally:
            await manager.stop()
            await runtime.stop()
            await database.close()

    typer.echo(json.dumps(asyncio.run(inspect()), indent=2, sort_keys=True))


@cutover_app.command("audit")
def cutover_audit(
    database_url: Annotated[str, typer.Option(help="Disposable Python database URL.")],
    data_root: Annotated[Path, typer.Option(exists=True, file_okay=False, resolve_path=True)],
) -> None:
    """Audit the locked 2-user/27-Agent production migration manifest."""

    async def inspect() -> dict[str, object]:
        database = Database(database_url)
        runtime = Runtime()
        manager = AgentRuntimeManager(database, runtime, AgentRuntimeConfig(data_root=data_root))
        await runtime.start()
        try:
            await manager.start()
            report = await audit_cutover(database, manager)
            return report.model_dump(mode="json")
        finally:
            await manager.stop()
            await runtime.stop()
            await database.close()

    payload = asyncio.run(inspect())
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    if not payload["ready"]:
        raise typer.Exit(code=2)

"""FastClaw command-line entry point."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated, Literal

import typer

from fastclaw.agent.manager import AgentRuntimeConfig, AgentRuntimeManager
from fastclaw.migration import AssetImportConflictError, import_assets, import_go_database
from fastclaw.runtime import Runtime
from fastclaw.skills import SkillCatalog
from fastclaw.storage import Database

_DEFAULT_DATA_ROOT = Path.home() / ".fastclaw-python"

app = typer.Typer(help="FastClaw Python runtime tools.")
migrate_app = typer.Typer(help="One-way data migration commands.")
skills_app = typer.Typer(help="Discover and explicitly prepare Skill environments.")
providers_app = typer.Typer(help="Check centralized Provider and Skill credentials.")
app.add_typer(migrate_app, name="migrate")
app.add_typer(skills_app, name="skills")
app.add_typer(providers_app, name="providers")


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
            AgentRuntimeConfig(data_root=data_root),
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

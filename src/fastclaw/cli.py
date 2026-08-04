"""FastClaw command-line entry point."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from fastclaw.migration import import_go_database

app = typer.Typer(help="FastClaw Python runtime tools.")
migrate_app = typer.Typer(help="One-way data migration commands.")
app.add_typer(migrate_app, name="migrate")


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

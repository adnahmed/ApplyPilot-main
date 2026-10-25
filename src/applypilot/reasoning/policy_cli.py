"""Manual policy-only transfer; never restores the application database."""
from __future__ import annotations

import json
from pathlib import Path
from applypilot import postgres
from typing import Optional

import typer

app = typer.Typer(help="Export/import DSPy instruction policies without jobs, corpus, credentials or browser state.", no_args_is_help=True)


def _run(operation):
    from applypilot.config import load_env
    from .policies import PolicyError
    load_env()
    try:
        result = operation()
    except PolicyError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1) from None
    except (ValueError, OSError, postgres.Error) as exc:
        typer.echo("Policy operation failed (" + type(exc).__name__ + "); check the local file and permissions.", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("export")
def export_policy(output: Path = typer.Argument(..., help="Private JSON output path."),
                  task: Optional[str] = typer.Option(None, "--task", help="Export just one registered task."),
                  force: bool = typer.Option(False, "--force", help="Replace an existing export file.")):
    """Export current deployed instructions, compatibility metadata and rollback history."""
    from .policies import export_file
    _run(lambda: export_file(output, task=task, force=force))


@app.command("import")
def import_policy(path: Path = typer.Argument(..., help="An ApplyPilot policy JSON export."),
                  trust: bool = typer.Option(False, "--trust", help="Accept reviewed instructions and their recorded deployment decisions; does not reevaluate them."),
                  replace: bool = typer.Option(False, "--replace", help="Replace conflicting deployments for the imported tasks only.")):
    """Merge trusted policies without changing jobs, private examples or learning settings."""
    from .policies import install_bundle, load
    _run(lambda: install_bundle(load(path), trust=trust, replace=replace))

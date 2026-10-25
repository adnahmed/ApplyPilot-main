"""CLI access to native career reports and interview sessions."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
from typing import Annotated

import typer

from . import service, store

app = typer.Typer(help='Evaluate roles, prepare interviews, review offers and learn from outcomes.',
                  no_args_is_help=True)


def _connection():
    from applypilot.config import load_env
    from applypilot.database import get_connection, init_db
    load_env()
    init_db()
    return get_connection()


def _print(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command('run')
def run_report(mode: str = typer.Argument(..., help='Workflow ID; see career-ops reports for mode labels.'),
               job_url: str = typer.Option('', '--job-url'),
               input_json: Annotated[Path | None, typer.Option('--input-json', exists=True, dir_okay=False)] = None):
    """Run a workflow with a saved job and/or a JSON file containing supported inputs."""
    data = {}
    if input_json is not None:
        if input_json.stat().st_size > 350000:
            raise typer.BadParameter('Career input JSON must be at most 350 KB')
        try:
            data = json.loads(input_json.read_text(encoding='utf-8-sig'))
        except (ValueError, OSError) as exc:
            raise typer.BadParameter('Cannot read a valid JSON input object') from exc
        if not isinstance(data, dict):
            raise typer.BadParameter('Career input JSON must contain an object')
    if job_url:
        data['job_url'] = job_url
    try:
        _connection()
        from applypilot.orchestration.integration_client import submit_report
        report = asyncio.run(submit_report(mode, data))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _print(report)


@app.command('reports')
def reports(job_url: str = typer.Option('', '--job-url')):
    """List saved reports, interview sessions, story drafts and supported workflow IDs."""
    conn = _connection()
    value = store.overview(conn, job_url=job_url or None)
    value['reports'] = [store.get(conn, report['id']) for report in value['reports']]
    _print(value)


@app.command('retry')
def retry(report_id: str = typer.Option(..., '--id', help='ID of an errored or cancelled report.')):
    """Retry the original saved input without changing any learning configuration."""
    conn = _connection()
    try:
        prior = store.get(conn, report_id)
        from applypilot.orchestration.integration_client import submit_report
        result = asyncio.run(submit_report(prior['mode'], {'retry_report_id': report_id}))
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    _print(result)

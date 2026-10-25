"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__
from applypilot import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("pgembed").setLevel(logging.WARNING)

app = typer.Typer(
    name="applypilot",
    help="AI-powered concurrent job application services.",
    no_args_is_help=True,
)
from applypilot.tracking.feedback_cli import app as feedback_app
app.add_typer(feedback_app, name="feedback")
from applypilot.referrals.cli import app as referrals_app
app.add_typer(referrals_app, name="referrals")
from applypilot.integrations.cli import app as sources_app
app.add_typer(sources_app, name="sources")
from applypilot.unattended.cli import app as unattended_app
app.add_typer(unattended_app, name="unattended")
from applypilot.reasoning.policy_cli import app as policy_app
app.add_typer(policy_app, name="policy")
from applypilot.career_ops.cli import app as career_ops_app
app.add_typer(career_ops_app, name='career-ops')
from applypilot.orchestration.cli import app as temporal_app
app.add_typer(temporal_app, name='temporal')


console = Console()
log = logging.getLogger(__name__)

# Selectable job services (display order, not execution dependencies)
VALID_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf", "brief")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered concurrent job application services."""
    if ctx.invoked_subcommand not in {None, "init", "start", "stop", "restart", "pause", "resume", "status", "reset-data", "blockers", "evidence", "temporal"}:
        from applypilot.operations.runtime import initialize
        try:
            initialize(ctx.invoked_subcommand)
        except Exception:
            pass  # Monitoring never prevents the ordinary command from starting.


@app.command("blockers")
def blockers(
    db: Optional[str] = typer.Option(None, "--db", help="Existing database; opened read-only."),
    min_score: Optional[int] = typer.Option(None, "--min-score", min=0, max=10),
    max_age_days: Optional[int] = typer.Option(None, "--max-age-days", min=0),
    doc_format: str = typer.Option("pdf", "--doc-format"),
    route_kind: Optional[str] = typer.Option(None, "--route-kind", help="Optional worker route filter."),
    output: Optional[str] = typer.Option(None, "--output", help="New JSON file with every job's blockers."),
    timeout: float = typer.Option(30.0, "--timeout", min=0.1),
):
    """Reconcile all high-fit jobs against preparation and application selection."""
    from applypilot.blocker_census import main as census_main
    arguments = ["--db", db or str(config.DB_PATH), "--doc-format", doc_format, "--timeout", str(timeout)]
    for key, value in (("--min-score", min_score), ("--max-age-days", max_age_days), ("--output", output), ("--route-kind", route_kind)):
        if value is not None:
            arguments.extend([key, str(value)])
    raise typer.Exit(census_main(arguments))


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command("reset-data")
def reset_data(
    scope: Optional[str] = typer.Option(None, "--scope", help="Required: documents (preserve history) or all (wipe database history)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Inspect a running database read-only; never start or initialize one."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation for the explicitly selected scope."),
) -> None:
    """Clear unsubmitted documents or explicitly reset all PostgreSQL stores."""
    if scope is None:
        console.print("Choose an explicit reset scope. No data was changed.")
        console.print("  applypilot reset-data --scope documents --dry-run")
        console.print("  applypilot reset-data --scope documents -y")
        console.print("  applypilot reset-data --scope all")
        return
    if scope not in {"documents", "all"}:
        raise typer.BadParameter("Choose documents or all", param_hint="--scope")
    from applypilot.config import load_env
    from applypilot.data_reset import reset_derived_data

    load_env()
    root = config.APP_DIR
    preview = reset_derived_data(root, scope=scope, dry_run=True)
    console.print(f"[bold]Reset scope: {scope}[/bold]")
    if preview.inventory_complete:
        console.print(f"Jobs affected: {preview.affected_jobs:,}; generated files selected: {preview.selected_files:,}")
        console.print(f"Protected files: {preview.protected_files:,}; files with uncertain ownership retained: {preview.unowned_files:,}")
    for warning in preview.warnings:
        console.print(warning)
    if scope == "documents":
        console.print("Preserves submitted/active application documents, all application history, accounts, contacts, learning, profile and source evidence.")
        console.print("Clears unsubmitted document pointers and files; normal future preparation generates replacements.")
    else:
        console.print("[bold red]Deletes ALL relational history and generated documents, including submitted copies.[/bold red]")
        console.print("Application duplicate protection, database accounts, contacts, Q&A, learning records and telemetry are removed.")
        console.print("Native database backups are created first. Profile, original resume, credentials, sessions and impact ledger files remain.")
    if dry_run:
        return
    if not yes and not typer.confirm(f"Apply the {scope} reset now?", default=False):
        console.print("Reset cancelled; nothing was changed.")
        return
    try:
        report = reset_derived_data(root, scope=scope, dry_run=False)
    except Exception as exc:
        console.print(f"[red]Reset interrupted: {exc}[/red]")
        console.print(f"Inspect {root / 'resets' / (scope + '.json')} for the last durable phase; rerun the same scope to reconcile cleanup.")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Reset phase: {report.phase}[/green]")
    console.print(f"Jobs affected: {report.affected_jobs:,}; jobs preserved: {report.preserved_jobs:,}; jobs removed: {report.removed_jobs:,}")
    console.print(f"Files removed: {report.removed_files:,}; protected: {report.protected_files:,}; unowned retained: {report.unowned_files:,}; skipped: {report.skipped_files:,}")
    if report.backup:
        console.print(f"Database backup: {report.backup}")
    console.print(f"Cleanup journal: {report.journal}")
    for warning in report.warnings:
        console.print(warning)
    if report.phase != "complete":
        raise typer.Exit(code=1)

@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(None),
    min_score: int = typer.Option(config.DEFAULTS['min_score'], '--min-score'),
    max_age_days: int = typer.Option(config.DEFAULTS['max_job_age_days'], '--max-age-days'),
    doc_format: str = typer.Option('pdf', '--doc-format'),
    dry_run: bool = typer.Option(False, '--dry-run'),
    source: Optional[list[str]] = typer.Option(None, '--source', '-s'),
    list_sources: bool = typer.Option(False, '--list-sources'),
    continuous: bool = typer.Option(False, '--continuous/--once'),
) -> None:
    """Start durable preparation/discovery, or install recurring Temporal Schedules."""
    import asyncio
    from applypilot.discovery.catalog import DISCOVERY_SOURCES, resolve_source_names
    from applypilot.orchestration.client import run_work, install
    from applypilot.orchestration.contracts import JOB_PHASES
    if list_sources:
        console.print_json(data=DISCOVERY_SOURCES)
        return
    selected = stages or ['all']
    if set(selected) - {'all', *VALID_STAGES, 'company_research'}:
        raise typer.BadParameter('Unknown stage')
    if doc_format not in {'pdf', 'docx'}:
        raise typer.BadParameter('Choose pdf or docx')
    config.load_env()
    chosen = set(VALID_STAGES if 'all' in selected else selected)
    phases = [phase for phase in JOB_PHASES if phase in chosen or
              (phase == 'company_research' and 'enrich' in chosen)]
    options = {'min_score': min_score, 'max_age_days': max_age_days,
               'doc_format': doc_format, 'dry_run': dry_run,
               'sources': resolve_source_names(source) if source else None}
    if phases:
        options['job_phases'] = phases
    if dry_run:
        console.print_json(data={'preview': options, 'stages': sorted(chosen), 'work_started': False})
        return
    async def submit():
        if continuous:
            return await install(options, start=True)
        result = {}
        if 'discover' in chosen:
            result['discovery'] = await run_work('discover', options)
        if phases:
            result['preparation'] = await run_work('jobs', options)
        return result
    console.print_json(data=asyncio.run(submit()))


@app.command()
def discover(
    plan: bool = typer.Option(False, '--plan'),
    source: Optional[list[str]] = typer.Option(None, '--source', '-s'),
) -> None:
    """Inspect the configured provider plan or start durable provider discovery."""
    config.load_env()
    if plan:
        from applypilot.discovery.context import build_context
        from applypilot.discovery.planner import plan_report, plan_tasks
        context = build_context(sources=source)
        console.print_json(data=plan_report(context, plan_tasks(context)))
        return
    import asyncio
    from applypilot.orchestration.client import run_work
    console.print_json(data=asyncio.run(run_work('discover', {'sources': source})))


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", min=0, help="Maximum job entities selected for this finite request; not a guaranteed number of submissions."),
    workers: Optional[int] = typer.Option(
        None, "--browser-workers", min=1,
        help="Fixed BROWSER resource limit; defaults to APPLYPILOT_BROWSER_WORKERS or two. LinkedIn remains serialized.",
    ),
    min_score: int = typer.Option(
        config.DEFAULTS["min_score"], "--min-score",
        help=f"Minimum fit score for job selection (default: {config.DEFAULTS['min_score']}).",
    ),
    max_age_days: int = typer.Option(
        config.DEFAULTS["max_job_age_days"], "--max-age-days",
        help=(
            "Skip jobs whose discovered_at is older than this many days. "
            "0 = no age filter. "
            f"Default: {config.DEFAULTS['max_job_age_days']}."
        ),
    ),
    max_score: Optional[int] = typer.Option(None, "--max-score", help="Maximum fit score for job selection (useful for testing on lower-score jobs)."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Install recurring Temporal Schedules."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    doc_format: str = typer.Option("pdf", "--doc-format", help="Document format for resumes/cover letters: pdf (default) or docx."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    fresh_sessions: bool = typer.Option(False, "--fresh-sessions", help="Refresh Chrome session cookies from your real profile before launching."),
    no_focus: bool = typer.Option(False, "--no-focus", help="Prevent Chrome windows from stealing keyboard focus (Linux/GNOME only). Windows stay visible but won't interrupt your active app."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
    reset_category: Optional[str] = typer.Option(None, "--reset-category", help="Reset all jobs in a category for retry (e.g., blocked_technical)."),
    prune_attempts: bool = typer.Option(False, "--prune-attempts", help="Remove stale application_attempts that block jobs from re-queueing."),
    sessions: bool = typer.Option(False, "--sessions", help="List saved ATS sessions."),
    clear_session: Optional[str] = typer.Option(None, "--clear-session", help="Clear a saved ATS session (e.g., workday)."),
    browser_runtime: Optional[str] = typer.Option(
        None, "--browser-runtime",
        help="Browser runtime: auto, browseros, or playwright. Omitted uses Settings / APPLYPILOT_BROWSER_RUNTIME.",
    ),
) -> None:
    """Submit native application requests, or perform an explicit local utility."""
    config.load_env()
    if any((mark_applied, mark_failed, reset_failed, reset_category, prune_attempts, sessions, clear_session)):
        _bootstrap()

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if reset_category:
        from applypilot.database import reset_by_category
        count = reset_by_category(reset_category)
        console.print(f"[green]Reset {count} job(s) in category '{reset_category}' for retry.[/green]")
        return

    if prune_attempts:
        from applypilot.database import prune_stale_attempts
        count = prune_stale_attempts()
        if count:
            console.print(f"[green]Pruned {count} safely expired application attempt(s).[/green]")
        else:
            console.print("[yellow]No attempt records were pruned; uncertain submission evidence is retained.[/yellow]")
        return

    if sessions:
        from applypilot.apply.chrome import list_ats_sessions
        ats_sessions = list_ats_sessions()
        if not ats_sessions:
            console.print("[dim]No saved ATS sessions.[/dim]")
            return
        from rich.table import Table
        t = Table(title="Saved ATS Sessions", show_header=True, header_style="bold cyan")
        t.add_column("ATS")
        t.add_column("Cookies")
        t.add_column("Age")
        for s in ats_sessions:
            age_str = f"{s['age_hours']:.1f}h" if s["age_hours"] is not None else "n/a"
            t.add_row(s["slug"], "yes" if s["has_cookies"] else "no", age_str)
        console.print(t)
        return

    if clear_session:
        from applypilot.apply.chrome import clear_ats_session
        if clear_ats_session(clear_session):
            console.print(f"[green]Cleared ATS session: {clear_session}[/green]")
        else:
            console.print(f"[yellow]No session found for: {clear_session}[/yellow]")
        return

    # Submissions run on Temporal workers, not on this CLI process.
    if workers is not None or fresh_sessions or no_focus or headless:
        raise typer.BadParameter('Configure browser/session resources on the Temporal Activity worker deployment')
    if limit is not None and continuous:
        raise typer.BadParameter('--limit applies to a finite request, not recurring Schedules')
    if limit is not None and url and limit != 1:
        raise typer.BadParameter('An exact --url request accepts only --limit 1 or no limit')
    if not 0 <= min_score <= 10 or (max_score is not None and not min_score <= max_score <= 10):
        raise typer.BadParameter('Scores must satisfy 0 <= min-score <= max-score <= 10')
    if doc_format not in {'pdf', 'docx'}:
        raise typer.BadParameter('Choose pdf or docx')
    import asyncio
    from applypilot.orchestration.client import run_job, run_work, install
    options = {'apply': True, 'dry_run': dry_run, 'min_score': min_score,
               'max_score': max_score, 'limit': limit,
               'max_age_days': max_age_days, 'doc_format': doc_format,
               'browser_runtime': browser_runtime or 'auto'}
    async def submit():
        if url:
            return await run_job(url, options)
        if continuous:
            return await install(options, start=True)
        return await run_work('apply', options)
    console.print_json(data=asyncio.run(submit()))


@app.command()
def autopilot(
    apply_jobs: bool = typer.Option(False, '--apply', help='Authorize the guarded application stage'),
    dry_run: bool = typer.Option(False, '--dry-run'),
    source: Optional[list[str]] = typer.Option(None, '--source', '-s'),
) -> None:
    """Compatibility command: install/resume Temporal Schedules; never launch workers."""
    import asyncio
    from applypilot.orchestration.client import install
    config.load_env()
    console.print_json(data=asyncio.run(install({'apply': apply_jobs, 'dry_run': dry_run,
                                               'sources': source}, start=True)))


@app.command()
def doctor(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable diagnostics."),
) -> None:
    """Inspect source, routing, guard, and browser readiness without repairing data."""
    _bootstrap()
    from applypilot.apply.runtime import browseros_health, browseros_url
    from applypilot.database import get_connection
    from applypilot.discovery.catalog import configured_source_names

    conn = get_connection()
    healthy, browser_reason = browseros_health()
    payload = {
        "configured_sources": configured_source_names(),
        "browseros": {
            "url": browseros_url(),
            "healthy": healthy,
            "detail": browser_reason,
        },
        "routes": {
            row["route"]: row["count"]
            for row in conn.execute(
                "SELECT route, COUNT(*) AS count FROM application_routes GROUP BY route"
            ).fetchall()
        },
        "guard_states": {
            row["status"]: row["count"]
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM application_attempts GROUP BY status"
            ).fetchall()
        },
        "legacy_sentinel_urls": conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE lower(trim(COALESCE(application_url,''))) "
            "IN ('none','null','nan','n/a','na','undefined')"
        ).fetchone()[0],
    }
    if json_output:
        import json as _json
        console.print_json(_json.dumps(payload, default=str))
        return
    console.print("\n[bold]ApplyPilot Doctor[/bold]\n")
    console.print(f"Sources:   {', '.join(payload['configured_sources'] or ['package defaults'])}")
    console.print(
        f"BrowserOS: {'[green]healthy[/green]' if healthy else '[yellow]fallback required[/yellow]'} "
        f"({browser_reason})"
    )
    console.print(f"Routes:    {payload['routes'] or 'none (legacy database)'}")
    console.print(f"Guards:    {payload['guard_states'] or 'none'}")
    console.print(f"Sentinel:  {payload['legacy_sentinel_urls']} sentinel URLs")


@app.command("linkedin-guard")
def linkedin_guard(
    clear_manual_block: bool = typer.Option(
        False, "--clear-manual-block",
        help="Operator-only: clear a persistent LinkedIn security/MFA block.",
    ),
) -> None:
    """Show or explicitly clear the LinkedIn Easy Apply governor."""
    _bootstrap()
    from applypilot.apply.linkedin_governor import (
        clear_manual_block as clear_block,
        status as governor_status,
    )

    if clear_manual_block:
        clear_block()
        console.print("[yellow]LinkedIn manual block cleared by operator.[/yellow]")
    console.print_json(data=governor_status())


@app.command("domain-guard")
def domain_guard(
    domain: str = typer.Argument(..., help="Domain to inspect."),
    clear_open: bool = typer.Option(False, "--clear", help="Operator-only: clear this circuit."),
) -> None:
    """Inspect or explicitly clear a per-domain circuit breaker."""
    _bootstrap()
    from applypilot.apply.circuit_breaker import clear, status as domain_status

    if clear_open:
        clear(domain)
        console.print(f"[yellow]Cleared domain circuit: {domain}[/yellow]")
    console.print_json(data=domain_status(domain))


def _control_command(command: str, *, wait: int = 300, json_output: bool = False,
                     open_browser: bool | None = None) -> dict:
    from applypilot import control
    config.load_env()
    result = (control.status(config.APP_DIR, live=True) if command == 'status'
              else control.action(config.APP_DIR, {'action': command, **({'wait_seconds': wait} if command == 'stop' else {})}))
    console.print_json(data=result)
    return result


@app.command()
def start(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable runtime status."),
    open_browser: Optional[bool] = typer.Option(None, "--open/--no-open", help="Open the dashboard; defaults to APPLYPILOT_OPEN_DASHBOARD (true)."),
) -> None:
    """Start the full saved workflow and print its authenticated dashboard URL."""
    _control_command("start", json_output=json_output, open_browser=open_browser)


@app.command()
def stop(
    wait: int = typer.Option(300, "--wait", min=0, max=3600, help="Seconds to allow cancellation before ending idle workflows."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable runtime status."),
) -> None:
    """Stop future work and cancel existing operations in this workspace."""
    _control_command("stop", wait=wait, json_output=json_output)


@app.command()
def restart(
    wait: int = typer.Option(300, "--wait", min=0, max=3600, help="Seconds to wait for shutdown before starting a replacement."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable runtime status."),
) -> None:
    """Stop the old controller, then start in the background with saved settings."""
    _control_command("restart", wait=wait, json_output=json_output)


@app.command()
def pause(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable runtime status."),
) -> None:
    """Pause Autopilot workers while keeping the controller available."""
    _control_command("pause", json_output=json_output)


@app.command()
def resume(
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable runtime status."),
) -> None:
    """Resume Autopilot in the background using saved settings."""
    _control_command("resume", json_output=json_output)


@app.command()
def status(
    runtime: bool = typer.Option(False, "--runtime", help="Show only controller/worker status, without querying job inventory."),
    json_output: bool = typer.Option(False, "--json", help="Emit runtime status as JSON, without job inventory."),
) -> None:
    """Show Autopilot runtime status and job-state statistics."""
    _control_command("status", json_output=json_output)
    if runtime or json_output:
        return
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Interview briefs", str(stats.get("with_brief", 0)))
    summary.add_row("Pending briefs", str(stats.get("pending_brief", 0)))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row(
        "Submission verification",
        str(
            stats.get("guard_states", {}).get("verification_required", 0)
            + stats.get("guard_states", {}).get("verification_quarantined", 0)
        ),
    )
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # Score breakdown of saved job states
    funnel = stats.get("score_funnel", [])
    if funnel:
        funnel_table = Table(
            title="\nPipeline Funnel by Score",
            show_header=True,
            header_style="bold magenta",
        )
        funnel_table.add_column("Score",        justify="center", style="bold")
        funnel_table.add_column("Cover Ready",  justify="right", style="green")
        funnel_table.add_column("Tailored",     justify="right", style="cyan")
        funnel_table.add_column("Needs Tailor", justify="right", style="yellow")
        funnel_table.add_column("Applied",      justify="right", style="dim green")
        funnel_table.add_column("Errors",       justify="right", style="red")

        for row in funnel:
            score = row["score"]
            score_str = f"[bold {'green' if score >= 9 else 'yellow' if score >= 7 else 'white'}]{score}[/]"
            funnel_table.add_row(
                score_str,
                str(row["cover_ready"])  if row["cover_ready"]  else "[dim]—[/]",
                str(row["tailored"])     if row["tailored"]     else "[dim]—[/]",
                str(row["needs_tailor"]) if row["needs_tailor"] else "[dim]—[/]",
                str(row["applied"])      if row["applied"]      else "[dim]—[/]",
                str(row["errors"])       if row["errors"]       else "[dim]—[/]",
            )

        console.print(funnel_table)

    # Apply categories with per-score breakdown
    by_cat = stats.get("by_category", {})
    if by_cat:
        cat_table = Table(title="\nApply Categories", show_header=True, header_style="bold blue")
        cat_table.add_column("Category", style="bold")
        cat_table.add_column("Total", justify="right")
        cat_table.add_column("10",  justify="right", style="bold green")
        cat_table.add_column("9",   justify="right", style="green")
        cat_table.add_column("8",   justify="right", style="yellow")
        cat_table.add_column("7",   justify="right", style="yellow")
        cat_table.add_column("6",   justify="right", style="dim")
        cat_table.add_column("<6",  justify="right", style="dim")
        cat_table.add_column("Action")

        cat_display = {
            "applied":             ("green",   "Done"),
            "pending":             ("white",   "In queue"),
            "in_progress":         ("cyan",    "Running now"),
            "blocked_auth":        ("yellow",  "Authentication failed"),
            "blocked_technical":   ("yellow",  "Retryable: applypilot reset-category blocked_technical"),
            "archived_ineligible": ("dim",     "Location/salary/type mismatch"),
            "archived_expired":    ("dim",     "Job no longer available"),
            "archived_platform":   ("red",     "Unsupported platform"),
            "archived_no_url":     ("dim",     "No application URL"),
            "manual_only":         ("dim",     "Manual ATS (no automation)"),
        }

        def _score_cell(d: dict, key: str) -> str:
            """Format a score count cell — blank if zero."""
            v = d.get(key, 0) if isinstance(d, dict) else 0
            return str(v) if v else "[dim]—[/dim]"

        order = ["applied", "pending", "in_progress",
                 "blocked_auth", "blocked_technical",
                 "archived_ineligible", "archived_expired",
                 "archived_platform", "archived_no_url", "manual_only"]
        for cat in order:
            d = by_cat.get(cat)
            if not d:
                continue
            total = d["total"] if isinstance(d, dict) else d
            color, action = cat_display.get(cat, ("white", ""))
            cat_table.add_row(
                f"[{color}]{cat}[/{color}]",
                str(total),
                _score_cell(d, "10"),
                _score_cell(d, "9"),
                _score_cell(d, "8"),
                _score_cell(d, "7"),
                _score_cell(d, "6"),
                _score_cell(d, "<6"),
                action,
            )

        # Any unknown categories
        for cat, d in sorted(by_cat.items(), key=lambda x: -(x[1]["total"] if isinstance(x[1], dict) else x[1])):
            if cat not in cat_display:
                total = d["total"] if isinstance(d, dict) else d
                if total > 0:
                    cat_table.add_row(
                        cat, str(total),
                        _score_cell(d, "10"), _score_cell(d, "9"),
                        _score_cell(d, "8"),  _score_cell(d, "7"),
                        _score_cell(d, "6"),  _score_cell(d, "<6"),
                        "",
                    )

        console.print(cat_table)

        # Retry hint for high-score retryable jobs
        retryable_tech = by_cat.get("blocked_technical", {})
        if isinstance(retryable_tech, dict):
            high_score_retryable = retryable_tech.get("10", 0) + retryable_tech.get("9", 0)
            if high_score_retryable > 0:
                console.print(
                    f"[bold yellow]  ↳ {high_score_retryable} score 9-10 jobs in blocked_technical are retryable.[/bold yellow]"
                    f" Run: [bold]applypilot reset-category blocked_technical[/bold]"
                )

    # By site (group all HN: * sites under "HackerNews")
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        grouped: dict[str, int] = {}
        for site, count in stats["by_site"]:
            key = "HackerNews" if (site or "").startswith("HN:") else (site or "Unknown")
            grouped[key] = grouped.get(key, 0) + count
        for site, count in sorted(grouped.items(), key=lambda x: -x[1]):
            site_table.add_row(site, str(count))

        console.print(site_table)

    # ── Funnel diagnostics ────────────────────────────────────────
    if stats.get("skipped_stale"):
        max_age = config.DEFAULTS["max_job_age_days"]
        console.print(
            f"\n[dim]Skipped as stale (>{max_age}d old): "
            f"{stats['skipped_stale']} ready-to-apply jobs[/dim]"
        )

    bbc = stats.get("blocked_by_cap") or {}
    if bbc.get("count"):
        console.print(
            f"\n[yellow]Blocked by company cap:[/yellow] "
            f"{bbc['count']} company/companies"
        )
        if bbc.get("companies"):
            preview = ", ".join(bbc["companies"][:10])
            more = f" (+{bbc['count'] - 10} more)" if bbc["count"] > 10 else ""
            console.print(f"  [dim]{preview}{more}[/dim]")

    # ── Pipeline state distribution (2026-04-24 state machine) ────
    from applypilot.database import get_connection as _get_conn
    _sc_conn = _get_conn()
    state_rows = _sc_conn.execute(
        "SELECT state, COUNT(*) FROM jobs WHERE state IS NOT NULL "
        "GROUP BY state ORDER BY COUNT(*) DESC"
    ).fetchall()
    if state_rows:
        state_table = Table(
            title="\nPipeline State Distribution",
            show_header=True, header_style="bold cyan",
        )
        state_table.add_column("State", style="bold")
        state_table.add_column("Count", justify="right")
        # Color-code by lifecycle phase.
        TERMINAL_OK = {"applied", "responded", "interview", "offer"}
        TERMINAL_BAD = {"rejected", "ghosted", "archived", "apply_failed",
                        "enrich_failed", "score_failed", "tailor_failed",
                        "cover_failed", "low_score"}
        ACTIVE = {"applying", "tailoring", "cover_writing", "ready_to_apply"}
        for r in state_rows:
            st = r[0]
            n = r[1]
            if st in TERMINAL_OK:
                color = "green"
            elif st in TERMINAL_BAD:
                color = "dim"
            elif st in ACTIVE:
                color = "yellow"
            else:
                color = "cyan"
            state_table.add_row(f"[{color}]{st}[/{color}]", str(n))
        console.print(state_table)

    console.print()


@app.command()
def track(
    days: int = typer.Option(14, "--days", "-d", help="Email look-back period in days."),
    setup: bool = typer.Option(False, "--setup", help="Verify Gmail MCP connectivity."),
    actions: bool = typer.Option(False, "--actions", "-a", help="Show pending action items."),
    ghosted_days: int | None = typer.Option(None, "--ghosted-days", min=1, max=365, help="Override the configured provisional silence reminder; never a rejection."),
    limit: int = typer.Option(100, "--limit", "-l", help="Max emails to fetch."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fetch + classify but don't write DB/files."),
    relabel: bool = typer.Option(False, "--relabel", help="Apply 'ap-track' label to all emails already in the DB (backfill)."),
    remap_stubs: bool = typer.Option(False, "--remap-stubs", help="Re-match emails under multi-company stubs to correct per-company jobs."),
    record_outcome: str | None = typer.Option(
        None, "--record-outcome",
        help="Manually record recruiter_response, screen, technical_interview, offer, or rejection.",
    ),
    job_url: str | None = typer.Option(None, "--job-url", help="Job URL for --record-outcome."),
    submission_id: str | None = typer.Option(None, "--submission-id", help="Exact submitted attempt for an ambiguous job."),
    outcome_notes: str = typer.Option("", "--outcome-notes", help="Optional manual outcome notes."),
    occurred_at: str | None = typer.Option(None, "--occurred-at", help="Optional ISO timestamp for the outcome."),
    cohorts: bool = typer.Option(False, "--cohorts", help="Show mature outcome cohorts without interpreting fit as probability."),
    maturity_days: int = typer.Option(14, "--maturity-days", help="Minimum submission age for --cohorts."),
) -> None:
    """Track application responses from Gmail."""
    _bootstrap()

    if record_outcome:
        if not job_url:
            raise typer.BadParameter("--job-url is required with --record-outcome")
        from applypilot.database import record_application_outcome
        try:
            saved = record_application_outcome(
                job_url, record_outcome, occurred_at=occurred_at,
                source="manual", notes=outcome_notes, submission_id=submission_id,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        from applypilot.tracking.feedback_learning import build_model
        from applypilot.database import get_connection
        build_model(get_connection())
        console.print(
            f"[green]Recorded {saved['event_type']} for {saved['job_url']} at {saved['occurred_at']}[/green]"
        )
        return

    if cohorts:
        from applypilot.database import get_outcome_cohort_stats
        rows = get_outcome_cohort_stats(maturity_days)
        table = Table(title=f"Mature application outcomes ({maturity_days}+ days)")
        for column in ("Role family", "Market", "Variant", "Sent", "Responses", "Screens", "Technical", "Offers"):
            table.add_column(column)
        for row in rows:
            table.add_row(
                str(row["role_family"]), str(row["market"]), str(row["document_variant"]),
                str(row["mature_submissions"]), str(row["recruiter_responses"]),
                str(row["screens"]), str(row["technical_interviews"]), str(row["offers"]),
            )
        console.print(table)
        return

    if setup:
        import asyncio
        from applypilot.tracking.gmail_client import check_gmail_setup, verify_connection

        ok, msg = check_gmail_setup()
        if not ok:
            console.print(f"[red]{msg}[/red]")
            raise typer.Exit(code=1)

        console.print("[dim]Testing Gmail MCP connection...[/dim]")
        connected = asyncio.run(verify_connection())
        if connected:
            console.print("[green]Gmail MCP connected successfully.[/green]")
        else:
            console.print("[red]Gmail MCP connection failed.[/red]")
            console.print("[dim]Check that gcp-oauth.keys.json is valid and OAuth is authorized.[/dim]")
            raise typer.Exit(code=1)
        return

    if actions:
        from applypilot.tracking import show_action_items
        show_action_items()
        return

    if relabel:
        from applypilot.tracking import relabel_all_tracked
        relabel_all_tracked()
        return

    if remap_stubs:
        from applypilot.tracking import remap_stubs as _remap_stubs
        _remap_stubs()
        return

    from applypilot.tracking import run_tracking

    result = run_tracking(
        days=days,
        ghosted_days=ghosted_days,
        
        dry_run=dry_run,
    )

    if result.get("errors", 0) > 0:
        raise typer.Exit(code=1)


@app.command()
def dashboard(
    port: int = typer.Option(8765, "--port", min=1024, max=65535),
    open_browser: bool = typer.Option(False, "--open/--no-open", help="Explicitly open the system browser; default prints the private link only."),
    read_only: bool = typer.Option(False, "--read-only", help="Disable website settings and cancellation actions."),
    snapshot: bool = typer.Option(False, "--snapshot", help="Generate the previous static HTML export instead."),
) -> None:
    """Open the live website; never launch a second application runtime."""
    if snapshot:
        _bootstrap()
        from applypilot.view import open_dashboard
        open_dashboard()
        return
    from applypilot.web.server import serve
    serve(port=port, open_browser=open_browser, read_only=read_only)


# ---------------------------------------------------------------------------
# Q&A knowledge base commands
# ---------------------------------------------------------------------------

qa_app = typer.Typer(name="qa", help="Manage the Q&A knowledge base for screening questions.")
app.add_typer(qa_app)

# ---------------------------------------------------------------------------
# Evidence commands
# ---------------------------------------------------------------------------

from applypilot.evidence.cli import evidence_app  # noqa: E402
app.add_typer(evidence_app)


# ---------------------------------------------------------------------------
# Impact Evidence Ledger commands
# ---------------------------------------------------------------------------

from applypilot.impact.cli import impact_app  # noqa: E402
app.add_typer(impact_app)


@qa_app.command("list")
def qa_list(
    limit: int = typer.Option(50, "--limit", "-l", help="Max Q&A pairs to show."),
) -> None:
    """Show stored Q&A pairs from past applications."""
    _bootstrap()

    from applypilot.database import get_all_qa

    pairs = get_all_qa()
    if not pairs:
        console.print("[dim]No Q&A pairs stored yet. Apply to jobs to build the knowledge base.[/dim]")
        return

    t = Table(title=f"Q&A Knowledge Base ({len(pairs)} total)", show_header=True, header_style="bold cyan")
    t.add_column("Question", max_width=50)
    t.add_column("Answer", max_width=30)
    t.add_column("Source")
    t.add_column("Outcome")
    t.add_column("Type")

    for qa in pairs[:limit]:
        outcome_color = {"accepted": "green", "rejected": "red"}.get(qa["outcome"], "dim")
        t.add_row(
            qa["question_text"][:50],
            qa["answer_text"][:30],
            qa["answer_source"],
            f"[{outcome_color}]{qa['outcome']}[/{outcome_color}]",
            qa.get("field_type") or "",
        )

    console.print(t)


@qa_app.command("stats")
def qa_stats() -> None:
    """Show Q&A knowledge base statistics."""
    _bootstrap()

    from applypilot.database import get_qa_stats

    stats = get_qa_stats()
    if stats["total"] == 0:
        console.print("[dim]No Q&A pairs stored yet.[/dim]")
        return

    console.print("\n[bold]Q&A Knowledge Base Stats[/bold]\n")
    console.print(f"  Total pairs:    {stats['total']}")
    console.print(f"  Unique Qs:      {stats['unique_questions']}")

    if stats["by_source"]:
        console.print("\n  [bold]By source:[/bold]")
        for src, cnt in stats["by_source"].items():
            console.print(f"    {src}: {cnt}")

    if stats["by_outcome"]:
        console.print("\n  [bold]By outcome:[/bold]")
        for out, cnt in stats["by_outcome"].items():
            color = {"accepted": "green", "rejected": "red"}.get(out, "dim")
            console.print(f"    [{color}]{out}[/{color}]: {cnt}")

    if stats.get("by_ats"):
        console.print("\n  [bold]By ATS:[/bold]")
        for ats, cnt in stats["by_ats"].items():
            console.print(f"    {ats or 'unknown'}: {cnt}")

    console.print()


@qa_app.command("export")
def qa_export(
    output: str = typer.Option("qa_export.yaml", "--output", "-o", help="Output YAML file path."),
) -> None:
    """Export Q&A pairs to YAML for review and editing."""
    _bootstrap()

    from applypilot.database import export_qa_yaml

    content = export_qa_yaml()
    if not content:
        console.print("[dim]No Q&A pairs to export.[/dim]")
        return

    from pathlib import Path
    out_path = Path(output)
    out_path.write_text(content, encoding="utf-8")
    console.print(f"[green]Exported Q&A to:[/green] {out_path.resolve()}")


@qa_app.command("import")
def qa_import(
    file: str = typer.Argument(..., help="YAML file with Q&A pairs to import."),
) -> None:
    """Import Q&A pairs from a YAML file."""
    _bootstrap()

    from pathlib import Path
    import yaml
    from applypilot.database import store_qa

    path = Path(file)
    if not path.exists():
        console.print(f"[red]File not found: {file}[/red]")
        raise typer.Exit(code=1)

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        console.print("[red]Expected a YAML list of Q&A objects.[/red]")
        raise typer.Exit(code=1)

    count = 0
    for item in data:
        q = item.get("question", "").strip()
        a = item.get("answer", "").strip()
        if q and a:
            store_qa(
                q, a,
                source=item.get("source", "human"),
                field_type=item.get("field_type"),
                ats_slug=item.get("ats"),
            )
            count += 1

    console.print(f"[green]Imported {count} Q&A pair(s).[/green]")


# ---------------------------------------------------------------------------
# creds — site credential CRUD
# ---------------------------------------------------------------------------

creds_app = typer.Typer(name="creds", help="Manage saved site credentials (usernames / passwords).")
app.add_typer(creds_app)


@creds_app.command("list")
def creds_list(
    show: bool = typer.Option(False, "--show", "-s", help="Show passwords in plaintext."),
) -> None:
    """List all saved site credentials."""
    _bootstrap()

    from applypilot.database import get_all_accounts

    rows = get_all_accounts()
    if not rows:
        console.print("[dim]No credentials saved yet. Use [bold]applypilot creds add[/bold] to add one.[/dim]")
        return

    t = Table(title=f"Site Credentials ({len(rows)} entries)", show_header=True, header_style="bold cyan")
    t.add_column("Domain",   min_width=28)
    t.add_column("Site",     min_width=12)
    t.add_column("Email",    min_width=24)
    t.add_column("Password", min_width=16)
    t.add_column("Notes",    max_width=30)
    t.add_column("Saved",    min_width=10)

    for row in rows:
        pwd = row["password"] or ""
        pwd_display = pwd if show else (("*" * min(len(pwd), 8)) if pwd else "[dim]—[/dim]")
        saved_date = (row["created_at"] or "")[:10]
        t.add_row(
            row["domain"],
            row["site"] or "",
            row["email"],
            pwd_display,
            row["notes"] or "",
            saved_date,
        )

    console.print(t)
    if not show:
        console.print("[dim]Passwords are masked. Use [bold]--show[/bold] to reveal them.[/dim]")


@creds_app.command("show")
def creds_show(
    domain: str = typer.Argument(..., help="Domain to show credentials for (e.g. linkedin.com)."),
) -> None:
    """Show full (unmasked) credentials for a single domain."""
    _bootstrap()

    from applypilot.database import get_all_accounts

    rows = [r for r in get_all_accounts() if r["domain"] == domain]
    if not rows:
        console.print(f"[red]No credentials found for domain:[/red] {domain}")
        raise typer.Exit(code=1)

    row = rows[0]
    console.print(f"\n  [bold]Domain:[/bold]   {row['domain']}")
    console.print(f"  [bold]Site:[/bold]     {row['site'] or ''}")
    console.print(f"  [bold]Email:[/bold]    {row['email']}")
    console.print(f"  [bold]Password:[/bold] {row['password'] or '[dim](none)[/dim]'}")
    if row["notes"]:
        console.print(f"  [bold]Notes:[/bold]    {row['notes']}")
    if row["job_url"]:
        console.print(f"  [bold]Job URL:[/bold]  {row['job_url']}")
    console.print(f"  [bold]Saved:[/bold]    {(row['created_at'] or '')[:19]}\n")


@creds_app.command("add")
def creds_add(
    domain:   str = typer.Argument(..., help="Domain key (e.g. linkedin.com, myworkdayjobs.com)."),
    email:    str = typer.Option(...,  "--email",    "-e", help="Login email / username."),
    password: str = typer.Option(None, "--password", "-p", help="Password (prompted if omitted)."),
    site:     str = typer.Option(None, "--site",     "-s", help="Human-readable site name."),
    notes:    str = typer.Option(None, "--notes",    "-n", help="Optional notes."),
) -> None:
    """Add or update credentials for a site."""
    _bootstrap()

    from applypilot.database import upsert_account

    if password is None:
        password = typer.prompt(f"Password for {domain}", hide_input=True, confirmation_prompt=True)

    action = upsert_account(domain, email, password, site=site, notes=notes)
    verb = "[green]Created[/green]" if action == "created" else "[yellow]Updated[/yellow]"
    console.print(f"{verb} credentials for [bold]{domain}[/bold] ({email})")


@creds_app.command("set")
def creds_set(
    domain:   str = typer.Argument(..., help="Domain to update."),
    email:    str = typer.Option(None, "--email",    "-e", help="New email / username."),
    password: str = typer.Option(None, "--password", "-p", help="New password (prompted if --email not given either)."),
    notes:    str = typer.Option(None, "--notes",    "-n", help="Update notes."),
) -> None:
    """Update one or more fields for an existing credential entry."""
    _bootstrap()

    from applypilot.database import get_all_accounts, upsert_account

    existing = next((r for r in get_all_accounts() if r["domain"] == domain), None)
    if not existing:
        console.print(f"[red]No credentials found for:[/red] {domain}  (use [bold]add[/bold] to create one)")
        raise typer.Exit(code=1)

    new_email    = email    or existing["email"]
    new_password = password or existing["password"]
    new_notes    = notes    if notes is not None else existing["notes"]

    if not email and not password and notes is None:
        # Nothing specified — prompt for password at minimum
        new_password = typer.prompt(f"New password for {domain}", hide_input=True, confirmation_prompt=True)

    upsert_account(domain, new_email, new_password, site=existing["site"], notes=new_notes)
    console.print(f"[green]Updated[/green] credentials for [bold]{domain}[/bold]")


@creds_app.command("import-logs")
def creds_import_logs(
    log_dir: str = typer.Option(None, "--log-dir", help="Directory of apply logs. Defaults to ~/.applypilot/logs/."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be imported without writing."),
    yes:     bool = typer.Option(False, "--yes", "-y", help="Skip per-entry confirmation for free-form entries."),
) -> None:
    """Scan apply logs for account credentials and import into the DB.

    Handles both structured ACCOUNT_CREATED: lines (current format) and
    older free-form log entries where the agent wrote the password as prose.
    Already-known domains are skipped unless --yes is passed.
    """
    _bootstrap()

    import os
    from applypilot.database import mine_accounts_from_logs, upsert_account, get_all_accounts

    if log_dir is None:
        log_dir = os.path.expanduser("~/.applypilot/logs")

    console.print(f"[dim]Scanning logs in {log_dir}…[/dim]")
    found = mine_accounts_from_logs(log_dir)

    if not found:
        console.print("[dim]No credential hints found in logs.[/dim]")
        return

    existing_domains = {r["domain"] for r in get_all_accounts()}

    imported = skipped = already = 0
    for entry in found:
        domain   = entry["domain"]
        email    = entry["email"]
        password = entry.get("password", "")
        site     = entry.get("site", "")
        source   = entry.get("source", "")
        src_file = entry.get("source_file", "")
        is_new   = domain not in existing_domains

        status_tag = "[green]NEW[/green]" if is_new else "[dim]EXISTS[/dim]"
        src_tag    = "[cyan]structured[/cyan]" if source == "structured" else "[yellow]free-form[/yellow]"
        console.print(
            f"  {status_tag} {src_tag}  {domain}  {email or '[dim](no email)[/dim]'}"
            f"  pw={'***' if password else '[dim]none[/dim]'}  ({src_file})"
        )

        if not is_new:
            already += 1
            continue

        if dry_run:
            imported += 1
            continue

        # Free-form entries ask for confirmation (may be less reliable)
        if source == "free-form" and not yes:
            if not email or not password:
                console.print(f"    [dim]Skipping — missing email or password. Add manually with: "
                              f"applypilot creds add {domain}[/dim]")
                skipped += 1
                continue
            confirmed = typer.confirm(f"    Import {domain} ({email} / {password[:4]}***)?")
            if not confirmed:
                skipped += 1
                continue

        if email and password:
            upsert_account(domain, email, password, site=site or None,
                           notes=f"auto-imported from log: {src_file}")
            existing_domains.add(domain)
            imported += 1
        else:
            console.print(f"    [dim]Skipping — missing {'email' if not email else 'password'}. "
                          f"Add manually: applypilot creds add {domain}[/dim]")
            skipped += 1

    suffix = " [dim](dry run — nothing written)[/dim]" if dry_run else ""
    console.print(
        f"\n[green]Imported {imported}[/green]  "
        f"[dim]already-known {already}  skipped {skipped}[/dim]{suffix}"
    )


@creds_app.command("delete")
def creds_delete(
    domain: str = typer.Argument(..., help="Domain whose credentials to delete."),
    yes:    bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt."),
) -> None:
    """Delete all saved credentials for a domain."""
    _bootstrap()

    from applypilot.database import delete_account, get_all_accounts

    existing = [r for r in get_all_accounts() if r["domain"] == domain]
    if not existing:
        console.print(f"[red]No credentials found for:[/red] {domain}")
        raise typer.Exit(code=1)

    row = existing[0]
    if not yes:
        confirmed = typer.confirm(f"Delete credentials for {domain} ({row['email']})?")
        if not confirmed:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit()

    deleted = delete_account(domain)
    console.print(f"[green]Deleted {deleted} credential row(s) for[/green] [bold]{domain}[/bold]")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "add_help_option": False})
def learning(ctx: typer.Context) -> None:
    """Manage automatic learning; defaults to status. Use 'learning --help' for actions."""
    from applypilot.reasoning.learning import main as learning_main
    raise typer.Exit(learning_main(ctx.args or ["status"]))




@app.command('company')
def company():
    """Start independently durable company/source research."""
    import asyncio
    from applypilot.orchestration.client import run_work
    config.load_env()
    console.print_json(data=asyncio.run(run_work('company_discovery')))


@app.command('worker')
def worker(roles: str = typer.Option('workflow', '--roles')):
    """Run native Temporal workers. Ctrl+C never cancels durable Workflows."""
    from applypilot.orchestration.cli import worker as serve
    serve(roles=roles)


@app.command('schedules')
def schedules(action: str = typer.Argument('status'), name: Optional[str] = typer.Option(None, '--name')):
    """Inspect, install, pause, resume or trigger native Schedules."""
    import asyncio
    from applypilot.orchestration import client
    config.load_env()
    async def perform():
        if action == 'status':
            return await client.schedule_status()
        if action == 'install':
            return await client.install({}, start=False)
        return await client.change_schedules(action, (name,) if name else ())
    console.print_json(data=asyncio.run(perform()))


@app.command('cancel')
def cancel(workflow_id: str):
    """Request cancellation of one explicit durable Workflow."""
    import asyncio
    from applypilot.orchestration.client import cancel as perform
    config.load_env()
    console.print_json(data=asyncio.run(perform(workflow_id)))


@app.command("backup")
def backup_command(
    action: str = typer.Argument("status", help="status, sync, check, points, schedules, restore, rollback"),
    request_file: Optional[Path] = typer.Option(None, "--request", help="Private JSON recovery request for offline restore"),
):
    """Native backup controls; restore/rollback are explicitly offline utilities."""
    import asyncio
    import json
    from applypilot import config
    from applypilot.backup import service
    from applypilot.orchestration import client
    config.load_env()
    root = config.APP_DIR
    if action == "status":
        result = service.status(root)
    elif action in {"sync", "check", "points"}:
        kind = {"sync": "backup_full", "check": "backup_check", "points": "backup_points"}[action]
        result = asyncio.run(client.run_work(kind, {"automatic": False}, root=root))
    elif action == "schedules":
        from applypilot.backup.common import config as backup_config
        result = asyncio.run(client.install_backup_schedules(enabled=backup_config(root)["enabled"], root=root))
    elif action == "restore":
        if request_file is None:
            raise typer.BadParameter("Provide a private --request JSON file with the existing recovery confirmation fields")
        result = service.restore(root, json.loads(request_file.read_text(encoding="utf-8")))
    elif action == "rollback":
        result = service.rollback(root)
    else:
        raise typer.BadParameter("Choose status, sync, check, points, schedules, restore, or rollback")
    console.print_json(data=result)


if __name__ == "__main__":
    app()

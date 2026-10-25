"""Native Temporal CLI. Optional SDK imports happen only for native commands."""
from __future__ import annotations
import asyncio
import json
import typer
from .contracts import ROLES

app = typer.Typer(help="Durable Temporal orchestration; independent of the legacy supervisor.", no_args_is_help=True)


def output(value):
    typer.echo(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False))


@app.callback()
def configure():
    from applypilot.config import load_env
    load_env()


@app.command("initialize")
def initialize():
    """Create shared application tables and an absent Temporal namespace."""
    from .client import initialize_data, initialize_namespace
    initialize_data()
    output(asyncio.run(initialize_namespace()))


@app.command("init-data")
def init_data():
    """Deploy business schemas without contacting Temporal or starting work."""
    from .client import initialize_data
    initialize_data()
    output({"business_schemas_initialized": True, "work_started": False})


@app.command("namespace")
def namespace():
    """Create only an absent namespace; existing retention/config is unchanged."""
    from .client import initialize_namespace
    output(asyncio.run(initialize_namespace()))


@app.command("worker")
def worker(roles: str = typer.Option("workflow", "--roles", help="Comma-separated capability roles")):
    selected = tuple(part.strip() for part in roles.split(",") if part.strip())
    if not selected or any(part not in ROLES for part in selected):
        raise typer.BadParameter("Known roles: " + ",".join(ROLES))
    from .worker import serve
    asyncio.run(serve(selected))


@app.command("install")
def install(start: bool = typer.Option(False, "--start"),
            apply: bool = typer.Option(False, "--apply", help="Include authorized application submissions"),
            dry_run: bool = typer.Option(False, "--dry-run"),
            doc_format: str = typer.Option("pdf", "--doc-format")):
    """Create/update all declared Schedules. Mere reapply preserves existing pauses.

    --apply affects job applications; networking delivery has its own existing
    authorization in unattended.json. This does not change that authorization.
    """
    if doc_format not in {"pdf", "docx"}:
        raise typer.BadParameter("Choose pdf or docx")
    from .client import install as perform
    output(asyncio.run(perform({"apply": apply, "dry_run": dry_run, "doc_format": doc_format}, start=start)))


@app.command("pause")
def pause(names: str = typer.Option("", "--names")):
    """Pause future Schedule actions. Does not cancel existing Workflows."""
    from .client import change_schedules
    output(asyncio.run(change_schedules("pause", tuple(filter(None, names.split(","))))))


@app.command("resume")
def resume(names: str = typer.Option("", "--names")):
    """Resume enabled Schedules; does not start local worker processes."""
    from .client import change_schedules
    output(asyncio.run(change_schedules("resume", tuple(filter(None, names.split(","))))))


@app.command("trigger")
def trigger(name: str):
    """Request one Schedule action, respecting its native overlap policy."""
    from .client import change_schedules
    output(asyncio.run(change_schedules("trigger", (name,))))


@app.command("job")
def job(url: str, apply: bool = typer.Option(False, "--apply"),
        dry_run: bool = typer.Option(False, "--dry-run")):
    """Submit an existing job's durable workflow and return its identity."""
    from .client import run_job
    output(asyncio.run(run_job(url, {"apply": apply, "dry_run": dry_run, "doc_format": "pdf"})))


@app.command("cancel")
def cancel(workflow_id: str):
    """Request cooperative cancellation; never terminates or undoes submissions."""
    from .client import cancel as perform
    output(asyncio.run(perform(workflow_id)))


@app.command("application-pacing")
def application_pacing(account_id: str,
    spacing_seconds: float = typer.Option(..., '--spacing-seconds', min=0.001),
    hourly_limit: int = typer.Option(..., '--hourly-limit', min=1),
    daily_limit: int = typer.Option(..., '--daily-limit', min=1),
    restriction_retry_seconds: float | None = typer.Option(None, '--restriction-retry-seconds', min=0.001)):
    """Update a running account's durable pacing without resetting its usage history."""
    from .client import update_application_pacing
    output(asyncio.run(update_application_pacing(account_id, spacing_seconds=spacing_seconds,
        hourly_limit=hourly_limit, daily_limit=daily_limit, restriction_retry_seconds=restriction_retry_seconds)))


@app.command("status")
def status(cached: bool = typer.Option(False, "--cached")):
    """Capture native read-only state for the dashboard and support ZIP."""
    from . import observability
    output(observability.cached() if cached else asyncio.run(observability.collect()))


@app.command("outreach-pacing")
def outreach_pacing(account_id: str,
    spacing_seconds: float = typer.Option(..., '--spacing-seconds', min=0.001),
    daily_limit: int = typer.Option(..., '--daily-limit', min=-1,
        help='0 pauses the account; -1 keeps the existing authorization limit')):
    """Update durable outreach pacing; existing permissions and receipts remain authoritative."""
    from .client import update_outreach_pacing
    output(asyncio.run(update_outreach_pacing(account_id, spacing_seconds=spacing_seconds, daily_limit=daily_limit)))


@app.command("policy")
def policy():
    """Show the effective non-secret declarative execution policy."""
    from .settings import load
    output(load().model_dump(mode="json"))


@app.command("search")
def search(query: str, providers: str = typer.Option("searxng,ddgs", "--providers"),
           page: int = typer.Option(1, "--page"), category: str = typer.Option("general", "--category"),
           observation_id: str | None = typer.Option(None, "--observation-id")):
    """Start independently durable provider searches; no messages are sent."""
    from .client import run_search
    output(asyncio.run(run_search(query, providers=tuple(p.strip() for p in providers.split(",") if p.strip()),
                                 page=page, category=category, observation_id=observation_id)))


@app.command("retire-source-coordination")
def retire_source_coordination():
    """Explicit deployment migration: archive the retired source gates/slots tables.

    Stop old source readers before deploying. This command does not drop history,
    touch DNS, send mail, or submit applications. Ordinary workers never call it.
    """
    from applypilot.integrations.state import SourceState
    state = SourceState()
    state.initialize()
    output({"archived_tables": state.retire_coordination()})


@app.command("search-results")
def search_results(observation_id: str, include_hits: bool = typer.Option(False, "--include-hits")):
    """Read persisted evidence, not inferred worker/Workflow status."""
    from applypilot.database import get_connection, close_connection
    from .search_store import observation
    try:
        conn = get_connection()
        result = observation(conn, observation_id, include_hits=include_hits)
        conn.commit()
    finally:
        close_connection()
    output(result)


@app.command("initialize-search")
def initialize_search():
    """Initialize only the search evidence tables; do not start applications or outreach."""
    from .client import initialize_search_data
    output(initialize_search_data())


@app.command('signal')
def signal(signal_id: str):
    """Start/resume one captured signal; no application or outreach is sent."""
    from .client import run_signal
    output(asyncio.run(run_signal(signal_id)))


@app.command("retire-discovery-cycle-gate")
def retire_discovery_cycle_gate():
    """Offline-only: archive the unused cycle gate, preserving every stored row."""
    from applypilot.config import APP_DIR
    from applypilot.control import require_offline_maintenance
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.schema_migrations import retire_cycle_gate
    require_offline_maintenance(APP_DIR)
    try:
        result = retire_cycle_gate(get_connection())
    finally:
        close_connection()
    output(result)


@app.command("refresh-sources")
def refresh_sources(source_key: list[str] | None = typer.Option(None, "--source-key"),
                    target_id: list[str] | None = typer.Option(None, "--target-id")):
    """Refresh stale or never-completely-observed sources using native dispatch."""
    from .client import run_work
    options = {}
    if source_key:
        options["source_request_keys"] = source_key
    if target_id:
        options["target_ids"] = target_id
    output(asyncio.run(run_work("source_refresh", options)))


@app.command("retire-target-worker-runs")
def retire_target_worker_runs():
    """Offline-only: archive claim/lease history; target cadence remains a migration seam."""
    from applypilot.config import APP_DIR
    from applypilot.control import require_offline_maintenance
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.schema_migrations import retire_target_runs
    require_offline_maintenance(APP_DIR)
    try:
        result = retire_target_runs(get_connection())
    finally:
        close_connection()
    output(result)

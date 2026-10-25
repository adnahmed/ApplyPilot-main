"""Rich live dashboard for the apply pipeline.

Displays real-time worker status, job progress, and recent events
in a terminal dashboard using the Rich library.
"""

import logging
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from applypilot.autopilot_status import (
    AutopilotSnapshot,
)
from applypilot.autopilot_status import (
    snapshot as autopilot_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class WorkerState:
    """Tracks the current state of the apply worker."""

    worker_id: int = 0
    status: str = "starting"
    role: str = "external"   # "external" or "linkedin"
    job_title: str = ""
    company: str = ""
    score: int = 0
    start_time: float = 0.0
    actions: int = 0
    last_action: str = ""
    status_reason: str = "initializing"
    admission_reason: str = ""
    execution_active: bool = False
    state_since: float = 0.0
    progress_at: float = 0.0
    form_progress_at: float = 0.0
    model_activity_at: float = 0.0
    model_wait_since: float = 0.0
    transport_at: float = 0.0
    trace_id: str = ""
    jobs_applied: int = 0
    jobs_failed: int = 0
    jobs_done: int = 0
    selection_checked_at: float = 0.0
    selection_polls: int = 0
    selection_empty_polls: int = 0
    selection_result: str = ""
    selection_policy_id: str = ""
    selection_diagnostic_id: str = ""
    selected_runtime: str = ""
    browser_session_file: str = ""
    total_cost: float = 0.0
    log_file: Path | None = None
    chrome_ok: bool | None = None  # None=unknown, True=connected, False=unreachable


# Module-level state (thread-safe via _lock)
_worker_states: dict[int, WorkerState] = {}
_archived_totals = {"jobs_applied": 0, "jobs_failed": 0, "jobs_done": 0, "total_cost": 0.0}
_events: deque[str] = deque(maxlen=50)
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# State mutation helpers
# ---------------------------------------------------------------------------

def init_worker(worker_id: int = 0, role: str = "external") -> None:
    """Register the worker in the dashboard state."""
    with _lock:
        _worker_states[worker_id] = WorkerState(worker_id=worker_id, role=role)


def archive_worker(worker_id: int) -> dict[str, int | float]:
    """Remove a finished worker while retaining its contribution to run totals."""
    with _lock:
        state = _worker_states.pop(worker_id, None)
        if state is None:
            return {}
        counters = {key: getattr(state, key) for key in _archived_totals}
        for key, value in counters.items():
            _archived_totals[key] += value
        return counters


def update_state(worker_id: int = 0, **kwargs) -> None:
    """Update the worker's state fields.

    Args:
        worker_id: Which worker to update.
        **kwargs: Field names and values to set on WorkerState.
    """
    with _lock:
        state = _worker_states.get(worker_id)
        if state is not None:
            now = time.time()
            state_changed = (("status" in kwargs and kwargs["status"] != state.status) or
                             ("job_title" in kwargs and kwargs["job_title"] != state.job_title))
            checkpoint_change = state_changed or any(
                key in kwargs and kwargs[key] != getattr(state, key, None)
                for key in ("status_reason", "jobs_applied", "jobs_failed", "jobs_done"))
            if not state.state_since or state_changed:
                state.state_since = now
            form_progress = kwargs.get("form_progress_at")
            if state_changed:
                state.progress_at = now
            elif type(form_progress) in {int, float} and form_progress > state.progress_at:
                state.progress_at = form_progress
            # Live reports expose fixed reasons, not raw action/model text.
            action = str(kwargs.get("last_action", ""))
            reasons = {"initializing": "initializing", "waiting for job": "selecting", "queued in Dask": "queued",
                       "browser unavailable": "browser_unavailable", "LinkedIn cooldown": "linkedin_cooldown",
                       "queue empty": "queue_empty", "finished": "finished"}
            if action in reasons:
                state.status_reason = reasons[action]
            elif action.startswith("polling ("):
                state.status_reason = "no_selectable_job"
            elif "status" in kwargs:
                state.status_reason = str(kwargs["status"])
            for key, value in kwargs.items():
                setattr(state, key, value)
            if checkpoint_change and state.status_reason not in {"selecting", "no_selectable_job"}:
                from applypilot.operations.worker_journal import checkpoint
                checkpoint("application.state",worker_id=worker_id,stage="applications",state=state.status,
                           reason=state.status_reason,job_title=state.job_title,company=state.company,
                           selected_runtime=state.selected_runtime,action_count=state.actions,
                           reported_applied=state.jobs_applied,reported_failed=state.jobs_failed)


def get_state(worker_id: int = 0) -> WorkerState | None:
    """Read the worker's current state."""
    with _lock:
        return _worker_states.get(worker_id)


def web_snapshot() -> list[dict]:
    """Copy lifecycle fields only; never publish prompts, raw logs or actions."""
    names = ("worker_id", "status", "role", "job_title", "company", "score",
             "start_time", "actions", "jobs_applied", "jobs_failed", "jobs_done", "chrome_ok", "status_reason", "admission_reason", "execution_active", "state_since", "progress_at",
             "form_progress_at", "model_activity_at", "model_wait_since", "transport_at",
             "selection_checked_at", "selection_polls", "selection_empty_polls",
             "selection_result", "selection_policy_id", "selection_diagnostic_id", "selected_runtime", "trace_id")
    with _lock:
        return [{name: getattr(state, name) for name in names}
                for _, state in sorted(_worker_states.items())]


def add_event(msg: str) -> None:
    """Add a timestamped event to the scrolling event log.

    Args:
        msg: Rich markup string describing the event.
    """
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        _events.append(f"[dim]{ts}[/dim] {msg}")


# ---------------------------------------------------------------------------
# Chrome health checks
# ---------------------------------------------------------------------------

# CDP port formula: 9222 + worker_id  (matches launch_chrome in chrome.py)
_CDP_BASE_PORT = 9222
_HEALTH_CHECK_INTERVAL = 5  # seconds
_HEALTH_CHECK_TIMEOUT = 0.5  # seconds

_health_thread: threading.Thread | None = None
_health_stop = threading.Event()


def _check_chrome_health(worker_id: int) -> bool:
    """Return True if Chrome CDP port for this worker is reachable."""
    port = _CDP_BASE_PORT + worker_id
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version",
            timeout=_HEALTH_CHECK_TIMEOUT,
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


def _health_check_loop() -> None:
    """Background thread: poll Chrome CDP ports and update chrome_ok."""
    while not _health_stop.wait(_HEALTH_CHECK_INTERVAL):
        with _lock:
            worker_ids = list(_worker_states.keys())
        for wid in worker_ids:
            with _lock:
                runtime_state = _worker_states.get(wid)
                runtime = runtime_state.selected_runtime if runtime_state else ""
                session_file = runtime_state.browser_session_file if runtime_state else ""
                if not runtime_state or not runtime_state.execution_active:
                    if runtime_state:
                        runtime_state.chrome_ok = None
                    continue
            if runtime == "browseros":
                from applypilot.apply.browseros_mcp import bridge_health
                ok = bridge_health(Path(session_file)) if session_file else None
            elif runtime == "playwright":
                ok = _check_chrome_health(wid)
            else:
                ok = None
            with _lock:
                state = _worker_states.get(wid)
                if (state is not runtime_state or not state.execution_active
                        or state.selected_runtime != runtime or state.browser_session_file != session_file):
                    continue  # This probe belongs to a finished/replaced invocation.
                prev = state.chrome_ok
                state.chrome_ok = ok
            if prev != ok and ok is not None:
                label = "BrowserOS owning bridge" if runtime == "browseros" else "Chrome CDP"
                logger.log(logging.INFO if ok else logging.WARNING,
                           "[worker-%d] %s %s", wid, label, "connected" if ok else "unreachable",
                           extra={"ops_worker_id": wid,
                                  "ops_reason_code": "browser_bridge_connected" if ok else "browser_bridge_unreachable",
                                  "ops_attempt_id": Path(session_file).stem if session_file else None})


def start_health_checks() -> None:
    """Start the background Chrome health-check thread (idempotent)."""
    global _health_thread
    if _health_thread is not None and _health_thread.is_alive():
        return
    _health_stop.clear()
    _health_thread = threading.Thread(
        target=_health_check_loop, daemon=True, name="chrome-health"
    )
    _health_thread.start()


def stop_health_checks() -> None:
    """Stop the background Chrome health-check thread."""
    _health_stop.set()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# Status -> Rich style mapping
_STATUS_STYLES: dict[str, str] = {
    "starting": "dim",
    "idle": "dim",
    "applying": "yellow",
    "applied": "bold green",
    "failed": "red",
    "expired": "dim red",
    "done": "bold",
    "waiting_answer": "bold cyan",
    "verification_required": "bold yellow",
    "verification_quarantined": "bold magenta",
}


def _count_actionable_jobs(min_score: int) -> int | None:
    """Display the worker census, not another expensive query on each redraw."""
    from applypilot.apply.capacity import snapshot
    value = snapshot()
    return value.get("selectable_jobs") if value.get("selection_available") is True else None


def _operational_counts() -> dict:
    try:
        from applypilot.database import get_connection

        conn = get_connection()
        reconciling = conn.execute(
            "SELECT COUNT(*) FROM application_attempts WHERE status IN "
            "('verification_required','verification_quarantined')"
        ).fetchone()[0]
        runtime_row = conn.execute(
            "SELECT browser_runtime FROM agent_run_metrics ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return {
            "reconciling": reconciling,
            "runtime": runtime_row[0] if runtime_row else "not selected",
        }
    except Exception:
        return {"reconciling": "unknown", "runtime": "unknown"}


def _stage_label(name: str, count: int, last_active: int) -> str:
    """Format a stage label: show active count or 'idle (was N)' if it was recently active."""
    if count > 0:
        return f"{count} active"
    if last_active > 0:
        return f"idle (was {last_active})"
    return "idle"


def _render_autopilot_panel(
    state: AutopilotSnapshot,
) -> Panel:
    """Render preparation/discovery state above apply workers."""

    discovery = (
        f"{state.discovery_done} / {state.discovery_total}"
        if state.discovery_total
        else str(state.discovery_done)
    )

    actionable = _count_actionable_jobs(state.min_score)
    actionable = "unknown" if actionable is None else actionable
    operational = _operational_counts()

    stats = Table.grid(expand=True)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)
    stats.add_column(ratio=1)

    stats.add_row(
        Text.assemble(
            ("Cycle: ", "bold"),
            str(state.cycle or 1),
        ),
        Text.assemble(
            ("State: ", "bold"),
            state.pipeline_state,
        ),
        Text.assemble(
            ("Discovery: ", "bold"),
            discovery,
        ),
    )
    stats.add_row(
        Text.assemble(("Runtime: ", "bold"), operational["runtime"]),
        Text.assemble(("Reconciling: ", "bold"), str(operational["reconciling"])),
    )

    last_search = Text.assemble(
        ("Last search: ", "bold"),
        state.last_search or "none yet",
    )

    stats.add_row(
        Text.assemble(
            ("New jobs: ", "bold"),
            str(state.new_jobs),
        ),
        Text.assemble(
            ("Duplicates: ", "bold"),
            str(state.duplicates),
        ),
        Text.assemble(
            ("Actionable: ", "bold"),
            str(actionable),
            style="bold green" if isinstance(actionable, int) and actionable > 0 else "",
        ),
    )

    last_active = getattr(state, "last_active_stages", {})
    discover_active = state.stages.get("discover", 0)
    stats.add_row(
        Text.assemble(
            ("Discover: ", "bold"),
            f"active {discovery}" if discover_active else "idle",
        ),
        Text.assemble(
            ("Enrich: ", "bold"),
            _stage_label("enrich", state.stages.get("enrich", 0), last_active.get("enrich", 0)),
        ),
        Text.assemble(("Score queue: ", "bold"), str(state.scoring_queue)),
    )
    stats.add_row(
        Text.assemble(
            ("Score active: ", "bold"),
            f"{state.scoring_active}/{state.scoring_workers or 0}",
        ),
        Text.assemble(
            ("Tailor: ", "bold"),
            _stage_label("tailor", state.stages.get("tailor", 0), last_active.get("tailor", 0)),
        ),
        Text.assemble(
            ("Cover: ", "bold"),
            _stage_label("cover", state.stages.get("cover", 0), last_active.get("cover", 0)),
        ),
    )
    stats.add_row(
        Text.assemble(
            ("PDF: ", "bold"),
            _stage_label("pdf", state.stages.get("pdf", 0), last_active.get("pdf", 0)),
        ),
        Text(),
        Text(),
    )

    if state.last_score is not None and state.last_scored_job:
        last_score = Text.assemble(
            ("Latest score this cycle: ", "bold"),
            f"{state.last_score} — {state.last_scored_job}",
        )
    else:
        last_score = Text.assemble(
            ("Latest score this cycle: ", "bold"),
            "—",
        )

    sources = Text("Sources: ", style="bold")

    if state.sources:
        for index, (name, source) in enumerate(
            state.sources.items()
        ):
            if index:
                sources.append(" | ")

            if source.ok == 0 and source.failed == 0:
                sources.append(f"{name} pending", style="dim")
            elif source.failed == 0:
                suffix = "job" if source.jobs == 1 else "jobs"
                sources.append(
                    f"{name} {source.ok}✓ · {source.jobs} {suffix}",
                    style="green",
                )
            else:
                value = f"{name} {source.ok}✓/{source.failed}✗"
                if source.jobs:
                    suffix = "job" if source.jobs == 1 else "jobs"
                    value += f" · {source.jobs} {suffix}"
                if source.last_error:
                    value += f" · {source.last_error}"
                sources.append(value, style="yellow" if source.ok else "red")
    else:
        sources.append("starting...", style="dim")

    return Panel(
        Group(
            stats,
            last_search,
            last_score,
            sources,
        ),
        title="Preparation Pipeline",
        border_style="cyan",
    )


def render_dashboard() -> Table:
    """Build the Rich table showing all worker statuses.

    Returns:
        A Rich Table object ready for display.
    """
    autopilot = autopilot_snapshot()

    table = Table(
        title=(
            "Apply Workers"
            if autopilot.enabled
            else "ApplyPilot Dashboard"
        ),
        expand=True,
        show_lines=False,
    )
    table.add_column("W", width=3, justify="center")
    table.add_column("Job", min_width=30, max_width=50, no_wrap=True)
    table.add_column("Status", width=12, justify="center")
    table.add_column("Time", width=6, justify="right")
    table.add_column("Acts", width=5, justify="right")
    table.add_column("Last Action", min_width=20, max_width=35, no_wrap=True)
    table.add_column("OK", width=4, justify="right", style="green")
    table.add_column("Fail", width=4, justify="right", style="red")
    table.add_column("Cost", width=8, justify="right")

    with _lock:
        states = sorted(_worker_states.values(), key=lambda s: s.worker_id)
        total_applied = _archived_totals["jobs_applied"]
        total_failed = _archived_totals["jobs_failed"]
        total_cost = _archived_totals["total_cost"]

    for s in states:
        elapsed = ""
        if s.start_time:
            elapsed_s = int(time.time() - s.start_time)
            elapsed = f"{elapsed_s}s" if elapsed_s < 3600 else f"{elapsed_s // 3600}h"

        style = _STATUS_STYLES.get(s.status, "")
        status_text = Text("RECONCILING" if s.status in {"verification_required", "verification_quarantined"}
                           else s.status.upper(), style=style)

        job_text = f"{s.job_title[:28]} @ {s.company[:16]}" if s.job_title else ""

        if s.chrome_ok is True:
            w_style = "bold blue"
        elif s.chrome_ok is False:
            w_style = "bold red"
        else:
            w_style = "bold"

        if s.role == "linkedin":
            w_label = Text("LI", style=w_style)
            if not job_text and s.status == "idle":
                job_text = "LinkedIn (polling)"
        else:
            w_label = Text(str(s.worker_id), style=w_style)

        table.add_row(
            w_label,
            job_text,
            status_text,
            elapsed,
            str(s.actions) if s.actions else "",
            s.last_action[:35] if s.last_action else "",
            str(s.jobs_applied),
            str(s.jobs_failed),
            f"${s.total_cost:.3f}" if s.total_cost else "",
        )
        total_applied += s.jobs_applied
        total_failed += s.jobs_failed
        total_cost += s.total_cost

    # Totals row
    table.add_section()
    table.add_row(
        "", "", "", "", "", "TOTAL",
        str(total_applied), str(total_failed), f"${total_cost:.3f}",
        style="bold",
    )

    return table


def render_full() -> Table | Group:
    """Render apply workers plus optional autopilot preparation state."""
    table = render_dashboard()
    autopilot = autopilot_snapshot()

    with _lock:
        event_lines = list(reversed(_events))

    parts = []

    if autopilot.enabled:
        parts.append(
            Align.center(
                Text(
                    "ApplyPilot AUTOPILOT",
                    style="bold green",
                )
            )
        )
        parts.append(
            _render_autopilot_panel(autopilot)
        )

    parts.append(table)

    if event_lines:
        event_text = Text.from_markup(
            "\n".join(event_lines)
        )

        events_panel = Panel(
            event_text,
            title=f"Recent Events ({len(event_lines)})",
            border_style="dim",
        )

        parts.append(events_panel)

    if len(parts) == 1:
        return table

    return Group(*parts)


def get_totals() -> dict[str, int | float]:
    """Compute aggregate totals across all workers.

    Returns:
        Dict with keys: applied, failed, done, cost, including retired workers.
    """
    with _lock:
        applied = _archived_totals["jobs_applied"] + sum(s.jobs_applied for s in _worker_states.values())
        failed = _archived_totals["jobs_failed"] + sum(s.jobs_failed for s in _worker_states.values())
        done = _archived_totals["jobs_done"] + sum(s.jobs_done for s in _worker_states.values())
        cost = _archived_totals["total_cost"] + sum(s.total_cost for s in _worker_states.values())
    return {"applied": applied, "failed": failed, "done": done, "cost": cost}

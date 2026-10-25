"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import signal
import socketserver
from applypilot import postgres
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


from applypilot import config
from applypilot.database import (
    close_connection,
    get_connection,
    categorize_apply_result,
    commit_with_retry,
    transition_state,
)
from applypilot.apply.chrome import (  # noqa: F401  (re-exports)
    launch_chrome, cleanup_worker, kill_all_chrome,
    detect_ats, save_ats_session, clear_ats_session,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT, WORKER_LISTEN_BASE_PORT, bring_to_foreground,
    probe_existing_chrome, _AdoptedChromeProcess,
    _chrome_procs, _chrome_lock,
)
from applypilot.apply.dashboard import (  # noqa: F401  (re-exports)
    init_worker, update_state, add_event, get_state,
    render_full, get_totals, start_health_checks, stop_health_checks,
)
from applypilot.apply.queue import list_actionable_jobs
from applypilot.apply.result_handlers import (  # noqa: F401  (re-exports)
    _parse_account_created,
    _parse_qa_lines,
    _submission_report_from_output,
    _record_job_history,
)

logger = logging.getLogger(__name__)

# Document format for resume/cover letter uploads ("pdf" or "docx").
# Set once by the CLI before workers start; read by run_job.
_doc_format: str = "pdf"

# Browser automation is one well-defined model task.  When Claude Code is
# pointed at the ApplyPilot/LiteLLM gateway it should always request the
# browser-optimized public route; with Anthropic directly it uses Sonnet.
# Model choice is application policy, not an env/CLI override.
_BROWSER_AGENT_ROUTE = "browser"
_DIRECT_CLAUDE_MODEL = "sonnet"




def agent_model_name() -> str:
    """Model name Claude Code should use for browser automation."""
    from applypilot.gateway_policy import required
    return (_BROWSER_AGENT_ROUTE if os.environ.get("APPLYPILOT_AGENT_BASE_URL")
            or (required() and os.environ.get("LLM_URL")) else _DIRECT_CLAUDE_MODEL)


def set_doc_format(fmt: str) -> None:
    """Set the document format for apply uploads."""
    global _doc_format
    _doc_format = fmt


# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty: now lives in
# apply/orchestrator.py (mutated by main(); read by _worker_loop_body).
# Re-exported via the bottom-of-file import so `launcher.POLL_INTERVAL`
# keeps working for any external caller.

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Register cleanup on exit
atexit.register(cleanup_on_exit)


def _kill_all_children() -> None:
    """Stop owned SDK sessions and user mini-task sessions."""
    from applypilot.apply.agent_sdk import stop_sessions
    stop_sessions()
    for task in list(_mini_tasks.values()):
        task.stop()
    _mini_tasks.clear()


atexit.register(_kill_all_children)

# Temporal worker bootstrap owns process signals; adapters only clean up their resources.

# Always-on per-worker HTTP servers (one per worker, started once in worker_loop)
_worker_servers: dict[int, HTTPServer] = {}
_worker_server_lock = threading.Lock()


def worker_server_port(worker_id: int) -> int:
    """Advertise the bound listener, including the collision fallback port."""
    with _worker_server_lock:
        server = _worker_servers.get(worker_id)
        return server.server_address[1] if server else WORKER_LISTEN_BASE_PORT + worker_id

# Per-worker mutable state, closed over by each worker's HTTP handler
# Keys: job, status, reason, instructions,
#       handback_instructions, mini_task
_worker_state: dict[int, dict] = {}
_worker_state_lock = threading.Lock()

# Per-worker takeover/handback events
_takeover_events: dict[int, threading.Event] = {}
_handback_events: dict[int, threading.Event] = {}

# Per-worker user tasks retain their SDK session until teardown completes.
_mini_tasks: dict[int, object] = {}


def _run_mini_task(worker_id: int, cdp_port: int, instructions: str):
    """Start an explicitly requested user task against the worker's owned browser."""
    from claude_agent_sdk import ClaudeAgentOptions
    from applypilot.apply.agent_sdk import MiniTask
    from applypilot.apply.agent_cli import ASSIST_SYSTEM_RULES
    from applypilot.reasoning.application_policy import render, selected
    from applypilot.reasoning.learning_store import start_episode, finish_episode
    from applypilot.reasoning.settings import integer
    from uuid import uuid4

    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_configuration = json.loads(mcp_config_path.read_text(encoding="utf-8"))["mcpServers"]
    # Only Playwright is admitted by this user-task's existing tool allowlist.
    mcp_configuration = {name: server for name, server in mcp_configuration.items() if name == "playwright"}

    env = os.environ.copy()
    from applypilot.execution import current_task
    if current_task():
        env["APPLYPILOT_ACTIVITY_ID"] = current_task()["key"]
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    custom_model = agent_model_name()
    policy_text, policy_revision = selected(model=custom_model, task_kind="application_assist")
    prompt = (
        f"You have browser access via Playwright MCP (CDP port {cdp_port}).\n"
        f"USER TASK\n{instructions}\n\n"
        f"WORKFLOW\n{render(policy_text)}\n\n"
        f"TASK RULES\n{ASSIST_SYSTEM_RULES}"
    )
    from applypilot.gateway_policy import configure_agent_environment
    configure_agent_environment(env, model=custom_model)
    # SDK overlays inherited env; an omitted key would restore the removed credential.
    env.setdefault("ANTHROPIC_API_KEY", "")
    trace_id = uuid4().hex
    env["APPLYPILOT_AGENT_TRACE_ID"] = trace_id

    options = ClaudeAgentOptions(
        model=custom_model, mcp_servers=mcp_configuration, strict_mcp_config=True,
        system_prompt={"type": "preset", "preset": "claude_code", "append": ASSIST_SYSTEM_RULES},
        permission_mode="dontAsk", tools=[],
        allowed_tools=[
            "mcp__playwright__browser_navigate", "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_click", "mcp__playwright__browser_fill_form",
            "mcp__playwright__browser_type", "mcp__playwright__browser_press_key",
            "mcp__playwright__browser_file_upload", "mcp__playwright__browser_tabs",
            "mcp__playwright__browser_wait_for",
        ],
        extra_args={"no-session-persistence": None}, env=env, cwd=config.APP_DIR,
        max_turns=integer("APPLYPILOT_AGENT_MAX_TURNS", 128, 1),
        max_buffer_size=64 * 1024 * 1024,
    )
    with _worker_state_lock:
        job = (_worker_state.get(worker_id) or {}).get("job") or {}
    start_episode(trace_id, group_id=str(job.get("url") or instructions), attempt_id="",
                  prompt=prompt, system_prompt=ASSIST_SYSTEM_RULES, model=custom_model,
                  context_root="", revision=policy_revision, task_kind="application_assist")
    try:
        return MiniTask(options=options, prompt=prompt,
                        deadline_seconds=None,
                        trace_id=trace_id)
    except Exception:
        finish_episode(trace_id, status="startup_failed", infrastructure=True)
        raise


# ---------------------------------------------------------------------------
# Always-on per-worker HTTP listener
# ---------------------------------------------------------------------------

class _ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """HTTPServer that handles each connection in a new daemon thread."""
    daemon_threads = True


def _start_worker_listener(worker_id: int) -> int:
    """Start the always-on HTTP server for a worker.

    This server starts once at the beginning of worker_loop() and lives for
    the entire worker lifetime. It exposes the Take Over, Run Task, Handback,
    endpoints used by the Chrome extension popup.

    Endpoints:
        GET  /api/status         — current worker state (for extension polling)
        GET  /api/workers        — live worker listener registry for this runtime
        POST /api/takeover       — user takes over; stops current SDK session
        POST /api/run-task       — spawn mini Claude for a user instruction
        GET  /api/task-stream    — SSE stream of mini Claude output
        POST /api/handback       — resume main agent (optionally with context)

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Port the server is listening on.
    """
    port = WORKER_LISTEN_BASE_PORT + worker_id
    cdp_port = BASE_CDP_PORT + worker_id

    # Per-worker mutable state (closed over by handler)
    state: dict = {
        "job": None,
        "status": "idle",
        "reason": None,
        "instructions": None,
        "handback_instructions": None,
        "mini_task": None,
        "saved_instruction": None,
        "chrome_pid": None,
        "last_focused": 0,
        "history": [],  # list of completed job summaries for the homepage log
    }

    takeover_event = threading.Event()
    handback_event = threading.Event()

    with _worker_state_lock:
        _worker_state[worker_id] = state
    _takeover_events[worker_id] = takeover_event
    _handback_events[worker_id] = handback_event

    class _Handler(BaseHTTPRequestHandler):
        def handle_one_request(self):
            try:
                super().handle_one_request()
            finally:
                close_connection()

        def do_OPTIONS(self):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self):
            if self.path in ("/", ""):
                self._handle_homepage()
            elif self.path == "/api/status":
                self._handle_status()
            elif self.path == "/api/workers":
                self._handle_workers()
            elif self.path == "/api/log":
                self._handle_log()
            elif self.path == "/api/task-stream":
                self._handle_task_stream()
            elif self.path == "/api/focus":
                self._handle_focus()
            elif self.path.startswith("/api/jobs"):
                self._handle_jobs_list()
            elif self.path == "/api/integrations":
                self._handle_integrations()
            elif self.path.startswith("/api/qa"):
                self._handle_qa_list()
            elif self.path.startswith("/api/prefs/"):
                self._handle_prefs_get()
            elif self.path == "/api/accounts":
                self._handle_accounts_list()
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        def do_POST(self):
            # Always close after POST response.  If we leave the connection
            # open (HTTP/1.1 keep-alive) and the handler doesn't consume the
            # request body, Python's HTTP server will try to parse the leftover
            # bytes as the next request, causing a deadlock where the server
            # waits for a valid request line and the client waits for a response.
            self.close_connection = True
            if self.path == "/api/takeover":
                self._handle_takeover()
            elif self.path == "/api/run-task":
                self._handle_run_task()
            elif self.path == "/api/handback":
                self._handle_handback()
            elif self.path == "/api/add-job":
                self._handle_add_job()
            elif self.path == "/api/jobs/mark":
                self._handle_jobs_mark()
            elif self.path == "/api/integrations/gmail/reauth":
                self._handle_gmail_reauth()
            elif self.path == "/api/qa":
                self._handle_qa_create()
            elif self.path.startswith("/api/qa/"):
                self._handle_qa_mutate()
            elif self.path.startswith("/api/prefs/"):
                self._handle_prefs_save()
            elif self.path.startswith("/api/accounts/"):
                self._handle_account_mutate()
            elif self.path.startswith("/api/sessions/"):
                self._handle_session_mutate()
            else:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except Exception:
                return {}

        def _json_ok(self, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text_ok(self, text: bytes = b"ok") -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(text)))
            self.end_headers()
            self.wfile.write(text)

        def _handle_workers(self):
            with _worker_server_lock:
                workers = [{"workerId": wid, "serverPort": server.server_address[1]}
                           for wid, server in sorted(_worker_servers.items())]
            self._json_ok({"runtime_id": os.getenv("APPLYPILOT_RUNTIME_ID", ""),
                           "workers": workers})

        def _handle_homepage(self):
            import html as _html
            job = state.get("job") or {}
            status = state.get("status", "idle")
            title = job.get("title", "") or "No active job"
            company = job.get("company", "")
            site = job.get("site", "")
            score = job.get("fit_score", 0)
            status_color = {
                "applying": "#22c55e",
                "waiting_human": "#a855f7",
                "idle": "#6b7280",
            }.get(status, "#eab308")
            meta_parts = [p for p in [company, site] if p]
            if score:
                meta_parts.append(f"Score {score}/10")
            meta_line = " · ".join(meta_parts)
            instructions = state.get("instructions", "")
            instructions_block = ""
            if instructions:
                instructions_block = (
                    f'<div class="instructions">'
                    f'<strong>Instructions:</strong><br>'
                    f'{_html.escape(instructions).replace(chr(10), "<br>")}'
                    f'</div>'
                )

            # Build activity log rows
            history = list(reversed(state.get("history", [])))
            outcome_colors = {
                "applied":       ("#22c55e", "✓ Applied"),
                "already_applied": ("#6366f1", "↩ Already applied"),
                "expired":       ("#6b7280", "⌛ Expired"),
                "failed":        ("#ef4444", "✗ Failed"),
            }
            log_rows = ""
            for h in history:
                oc = h.get("outcome", "failed")
                color, label = outcome_colors.get(oc, ("#6b7280", oc))
                ts_str = datetime.fromtimestamp(h["ts"]).strftime("%H:%M:%S") if h.get("ts") else "–"
                job_title = _html.escape(h.get("title", "–")[:60])
                job_co = _html.escape(h.get("company", "")[:30])
                sc = h.get("score", 0)
                dur = h.get("duration_s", 0)
                url = _html.escape(h.get("url", "#"))
                log_rows += (
                    f'<tr>'
                    f'<td style="color:#64748b;font-size:11px">{ts_str}</td>'
                    f'<td><span style="color:{color};font-weight:600;font-size:11px">{label}</span></td>'
                    f'<td style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
                    f'<a href="{url}" target="_blank" style="color:#e2e8f0;text-decoration:none">{job_title}</a>'
                    f'<br><span style="font-size:10px;color:#64748b">{job_co}</span></td>'
                    f'<td style="color:#60a5fa;font-size:11px;text-align:center">{sc}/10</td>'
                    f'<td style="color:#64748b;font-size:11px;text-align:right">{dur}s</td>'
                    f'</tr>'
                )
            log_section = ""
            if log_rows:
                log_section = f"""
<div class="log-panel">
  <div class="log-title">Session Activity</div>
  <table class="log-table">
    <thead><tr>
      <th>Time</th><th>Result</th><th>Job</th><th>Score</th><th>Time</th>
    </tr></thead>
    <tbody>{log_rows}</tbody>
  </table>
</div>"""

            body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>ApplyPilot W{worker_id}</title>
<style>
  * {{box-sizing:border-box;margin:0;padding:0}}
  body {{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;
        min-height:100vh;padding:24px;display:flex;flex-direction:column;
        align-items:center;gap:16px}}
  .badge {{background:#1e293b;border:1px solid #334155;border-radius:12px;
           padding:20px 28px;width:100%;max-width:600px;text-align:center}}
  .wid {{font-size:40px;font-weight:700;color:#eab308}}
  .status {{display:inline-block;padding:4px 12px;border-radius:9999px;
            font-size:12px;font-weight:600;margin:8px 0;
            background:{status_color}22;color:{status_color};
            border:1px solid {status_color}44}}
  .title {{font-size:18px;font-weight:600;margin:6px 0}}
  .meta {{font-size:12px;color:#94a3b8}}
  .instructions {{margin-top:12px;padding:10px 12px;background:#0f172a;
                  border-left:3px solid #a855f7;border-radius:4px;
                  font-size:12px;text-align:left;line-height:1.5}}
  .log-panel {{width:100%;max-width:600px;background:#1e293b;
               border:1px solid #334155;border-radius:12px;overflow:hidden}}
  .log-title {{padding:12px 16px;font-size:12px;font-weight:700;
               text-transform:uppercase;letter-spacing:.5px;color:#64748b;
               border-bottom:1px solid #334155}}
  .log-table {{width:100%;border-collapse:collapse;font-size:12px}}
  .log-table th {{padding:6px 10px;text-align:left;font-size:10px;
                  text-transform:uppercase;color:#475569;
                  border-bottom:1px solid #1e293b}}
  .log-table td {{padding:7px 10px;border-bottom:1px solid #0f172a;vertical-align:middle}}
  .log-table tr:last-child td {{border-bottom:none}}
  .hint {{font-size:10px;color:#334155;margin-top:4px}}
</style>
</head>
<body>
<div class="badge">
  <div class="wid">W{worker_id}</div>
  <div class="status">{status.upper().replace("_", " ")}</div>
  <div class="title">{_html.escape(title)}</div>
  {'<div class="meta">' + meta_line + '</div>' if meta_line else ''}
  {instructions_block}
</div>
{log_section}
<div class="hint">ApplyPilot Worker {worker_id} &nbsp;·&nbsp; <span id="ts"></span></div>
<script>
  document.getElementById('ts').textContent = new Date().toLocaleTimeString();
  setTimeout(() => location.reload(), 5000);
</script>
</body>
</html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_status(self):
            job = state.get("job") or {}
            site = job.get("site", "")
            reason = state.get("reason", "")
            # Use cached saved_instruction — never hit DB on status polls.
            # (DB access from per-request daemon threads leaks SQLite fds over time.)
            self._json_ok({
                "workerId": worker_id,
                "status": state.get("status", "idle"),
                "jobTitle": job.get("title", ""),
                "jobSite": site,
                "jobCompany": job.get("company", ""),
                "score": job.get("fit_score", 0),
                "reason": reason,
                "instructions": state.get("instructions"),
                "savedInstruction": state.get("saved_instruction"),
                "chromePid": state.get("chrome_pid"),
                "lastFocused": state.get("last_focused", 0),
            })

        def _handle_log(self):
            self._json_ok({"history": list(reversed(state.get("history", [])))})

        def _handle_focus(self):
            state["last_focused"] = time.time()
            try:
                from applypilot.apply.chrome import bring_to_foreground_cdp, bring_to_foreground_pid
                # CDP bringToFront focuses the tab within Chrome.
                # bring_to_foreground_pid raises the OS window (X11/Wayland).
                # Both are needed: CDP alone doesn't always raise the window.
                bring_to_foreground_cdp(cdp_port)
                bring_to_foreground_pid(state.get("chrome_pid"))
            except Exception:
                pass
            self._text_ok()

        def _handle_takeover(self):
            takeover_event.set()
            from applypilot.apply.agent_sdk import stop_sessions
            if not stop_sessions(worker_id):
                state["status"] = "stopping"
                self.send_error(503, "Application session is still stopping")
                return
            state["status"] = "paused_by_user"
            self._text_ok()

        def _handle_run_task(self):
            body = self._read_body()
            instructions = body.get("instructions", "").strip()
            if not instructions:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"instructions required")
                return
            takeover_event.set()
            from applypilot.apply.agent_sdk import stop_sessions
            if not stop_sessions(worker_id):
                self.send_error(503, "Application session is still stopping")
                return
            state["status"] = "paused_by_user"
            old = state.get("mini_task")
            if old:
                old.stop()
            task = _run_mini_task(worker_id, cdp_port, instructions)
            state["mini_task"] = task
            _mini_tasks[worker_id] = task
            task_id = str(int(time.time()))
            self._json_ok({"taskId": task_id})

        def _handle_task_stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            task = state.get("mini_task")
            if not task:
                try:
                    self.wfile.write(b"data: No task running\n\n")
                    self.wfile.flush()
                except Exception:
                    pass
                return
            try:
                for text in task.stream():
                    safe = text.replace("\n", "\\n")
                    self.wfile.write(f"data: {safe}\n\n".encode())
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _handle_handback(self):
            from applypilot.apply.agent_sdk import stop_sessions
            if not stop_sessions(worker_id):
                self.send_error(503, "Application session is still stopping")
                return
            task = state.get("mini_task")
            if task:
                task.stop()
            body = self._read_body()
            instructions = body.get("instructions", "").strip()
            save = body.get("save", False)
            if save and instructions:
                job = state.get("job") or {}
                site = job.get("site", "unknown")
                reason = state.get("reason") or "takeover"
                try:
                    from applypilot.database import store_qa
                    store_qa(
                        question=f"WORKER:{site}:{reason}",
                        answer=instructions,
                        source="human",
                        field_type="worker_instruction",
                    )
                    state["saved_instruction"] = instructions
                except Exception as e:
                    logger.debug("Failed to save worker instruction to Q&A KB: %s", e)
            state["handback_instructions"] = instructions or None
            state["status"] = "applying"
            # Clear takeover so next job doesn't see it
            takeover_event.clear()
            # Unblock worker_loop
            handback_event.set()
            self._text_ok()


        def _handle_jobs_list(self):
            """Return actionable jobs for the extension Jobs tab."""
            from urllib.parse import parse_qs, urlparse as _up
            qs = parse_qs(_up(self.path).query)
            limit = min(int(qs.get("limit", ["50"])[0]), 200)
            try:
                from applypilot.database import get_connection
                conn = get_connection()
                try:
                    rows = list_actionable_jobs(conn, limit=limit, doc_format=_doc_format)
                    fields = ("url", "title", "company", "site", "fit_score", "apply_status",
                              "apply_category", "apply_error", "tailored_resume_path", "cover_letter_path")
                    self._json_ok({"jobs": [{key: row.get(key) for key in fields} for row in rows]})
                finally:
                    conn.close()
            except Exception as e:
                logger.debug("jobs_list error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_add_job(self):
            """Add a job URL to the discovery queue from the extension."""
            from applypilot.database import get_connection
            body = self._read_body()
            url = (body.get("url") or "").strip()
            title = (body.get("title") or "").strip()
            if not url or not url.startswith("http"):
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"url required")
                return
            try:
                from urllib.parse import urlparse
                from applypilot.apply.routes import infer_and_persist_route
                site = urlparse(url).netloc.replace("www.", "")
                conn = get_connection()
                existing = conn.execute(
                    "SELECT url, apply_status FROM jobs WHERE url=?", (url,)
                ).fetchone()
                if existing:
                    self._json_ok({"status": "exists",
                                   "applyStatus": existing["apply_status"]})
                    return
                conn.execute(
                    "INSERT INTO jobs (url, title, site, discovered_at) "
                    "VALUES (?, ?, ?, ap_datetime('now'))",
                    (url, title or "Unknown Position", site),
                )
                infer_and_persist_route(
                    conn, job_url=url, application_url=url,
                    source="extension",
                )
                commit_with_retry(conn)
                logger.info("[W%d] Added job via extension: %s", worker_id, url[:80])
                self._json_ok({"status": "queued"})
            except Exception as e:
                logger.debug("add_job error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_jobs_mark(self):
            """Manually mark a job's apply status from the extension Jobs tab."""
            from applypilot.database import get_connection
            from datetime import datetime, timezone as tz
            body = self._read_body()
            url    = (body.get("url") or "").strip()
            action = (body.get("action") or "").strip()
            valid  = ("applied", "skip", "error", "reset")
            if not url or action not in valid:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"url and action required")
                return
            try:
                
                conn = get_connection()
                conn.execute("BEGIN")
                conn.advisory_lock("application-admission")
                from applypilot.apply.queue import ACTIVE_ATTEMPTS
                if conn.execute(
                    "SELECT 1 FROM application_attempts WHERE job_url=? "
                    f"AND status IN ({ACTIVE_ATTEMPTS})", (url,),
                ).fetchone():
                    raise RuntimeError("guarded application requires token-aware reconciliation")
                now = datetime.now(tz.utc).isoformat()
                if action == "applied":
                    conn.execute("""UPDATE jobs SET apply_status='applied', applied_at=?,
                        apply_category='applied', apply_attempts=COALESCE(apply_attempts,0)+1
                        WHERE url=?""", (now, url))
                    transition_state(conn, url, "applied",
                        reason="HTTP handler mark", force=True)
                elif action == "skip":
                    conn.execute("""UPDATE jobs SET apply_status='failed',
                        apply_category='archived_ineligible',
                        apply_error='manually skipped', apply_attempts=99, apply_recovery_credit=0 WHERE url=?""", (url,))
                    transition_state(conn, url, "manual_only",
                        reason="HTTP handler skip", force=True)
                elif action == "error":
                    conn.execute("""UPDATE jobs SET apply_status='failed',
                        apply_category='archived_platform',
                        apply_error='manually marked error', apply_attempts=99, apply_recovery_credit=0 WHERE url=?""", (url,))
                    transition_state(conn, url, "apply_failed",
                        reason="HTTP handler error", force=True)
                elif action == "reset":
                    conn.execute("""UPDATE jobs SET apply_status=NULL, apply_category='pending',
                        apply_error=NULL, apply_attempts=0, apply_recovery_credit=0, agent_id=NULL WHERE url=?""", (url,))
                    transition_state(conn, url, "ready_to_apply",
                        reason="HTTP handler reset", force=True)
                commit_with_retry(conn)
                logger.info("[W%d] Manual mark '%s': %s", worker_id, action, url[:70])
                self._json_ok({"status": "ok", "action": action})
            except Exception as e:
                logger.error("HTTP _handle_jobs_mark failed for %s: %s", url, e, exc_info=True)
                if "conn" in locals():
                    conn.rollback()
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_accounts_list(self):
            """List all rows in the accounts table for the credentials editor.

            Returns full rows including passwords — the page is on
            chrome-extension://{id} which is already a trusted origin for
            this user. The UI hides passwords by default with a
            click-to-reveal toggle.
            """
            try:
                from applypilot.database import get_connection
                conn = get_connection()
                rows = conn.execute(
                    "SELECT id, site, domain, email, password, "
                    "       created_at, job_url, notes "
                    "FROM accounts ORDER BY created_at DESC"
                ).fetchall()
                self._json_ok({"rows": [dict(r) for r in rows]})
            except Exception as e:
                logger.debug("accounts_list error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_account_mutate(self):
            """Update or delete an account row by ID."""
            try:
                row_id = int(self.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            body = self._read_body()
            action = (body.get("action") or "").strip()
            try:
                from applypilot.database import get_connection
                conn = get_connection()
                if action == "delete":
                    conn.execute("DELETE FROM accounts WHERE id = ?", (row_id,))
                    commit_with_retry(conn)
                    self._json_ok({"status": "deleted", "id": row_id})
                    return
                if action == "update":
                    fields = []
                    params: list = []
                    for col in ("site", "domain", "email", "password", "notes"):
                        if col in body:
                            fields.append(f"{col} = ?")
                            params.append(body[col] or None)
                    if not fields:
                        self.send_response(400)
                        self.end_headers()
                        return
                    params.append(row_id)
                    conn.execute(
                        f"UPDATE accounts SET {', '.join(fields)} WHERE id = ?",
                        params,
                    )
                    commit_with_retry(conn)
                    self._json_ok({"status": "updated", "id": row_id})
                    return
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"action must be 'update' or 'delete'")
            except Exception as e:
                logger.debug("account_mutate error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_session_mutate(self):
            """Clear a saved ATS session: /api/sessions/{slug}.

            Body: {action: "clear"}. Same backing helper as the
            `applypilot apply --clear-session` CLI flag.
            """
            slug = self.path.rsplit("/", 1)[-1]
            body = self._read_body()
            action = (body.get("action") or "").strip()
            if action != "clear":
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"action must be 'clear'")
                return
            try:
                from applypilot.apply.chrome import clear_ats_session
                ok = clear_ats_session(slug)
                self._json_ok({"status": "cleared" if ok else "not_found",
                               "slug": slug})
            except Exception as e:
                logger.debug("session_mutate error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        # --- Preferences (file editor) ---
        # Maps URL key → (path, format). Format determines how we validate
        # before saving — broken YAML/JSON would brick the next pipeline run.
        _PREFS_FILES = {
            "profile":         (config.PROFILE_PATH,                                 "json"),
            "searches":        (config.SEARCH_CONFIG_PATH,                           "yaml"),
            "company-limits":  (config.APP_DIR / config.COMPANY_LIMITS_PATH_NAME,    "yaml"),
        }

        def _handle_prefs_get(self):
            """Read a known prefs file and return raw text + format."""
            key = self.path.rsplit("/", 1)[-1]
            entry = self._PREFS_FILES.get(key)
            if not entry:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            path, fmt = entry
            try:
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                self._json_ok({
                    "key": key,
                    "path": str(path),
                    "format": fmt,
                    "text": text,
                    "exists": path.exists(),
                })
            except Exception as e:
                logger.debug("prefs_get error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_prefs_save(self):
            """Validate + save a prefs file. Backs up the previous version."""
            key = self.path.rsplit("/", 1)[-1]
            entry = self._PREFS_FILES.get(key)
            if not entry:
                self.send_response(404)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            path, fmt = entry
            body = self._read_body()
            text = body.get("text")
            if not isinstance(text, str):
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"text required")
                return

            # Validate before writing — bad JSON/YAML would crash the pipeline.
            try:
                if fmt == "json":
                    json.loads(text)
                elif fmt == "yaml":
                    import yaml
                    yaml.safe_load(text)
            except Exception as e:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(f"parse error: {e}".encode())
                return

            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                # Snapshot the current file before overwriting (single-slot
                # backup; previous backup gets overwritten).
                if path.exists():
                    backup = path.with_suffix(path.suffix + ".bak")
                    backup.write_bytes(path.read_bytes())
                path.write_text(text, encoding="utf-8")

                # Bust the company-limits cache so the change applies on
                # the next acquire_job() without a pipeline restart.
                if key == "company-limits":
                    try:
                        config._company_limits_cache = None
                    except Exception:
                        pass
                self._json_ok({"status": "saved", "path": str(path),
                               "bytes": len(text.encode())})
            except Exception as e:
                logger.warning("prefs_save error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_qa_list(self):
            """List/search qa_knowledge rows for the options-page editor.

            Query params: q (free text), ats, source, outcome, limit, offset.
            """
            from urllib.parse import parse_qs, urlparse as _up
            qs = parse_qs(_up(self.path).query)
            q       = (qs.get("q",       [""])[0] or "").strip()
            ats     = (qs.get("ats",     [""])[0] or "").strip()
            source  = (qs.get("source",  [""])[0] or "").strip()
            outcome = (qs.get("outcome", [""])[0] or "").strip()
            limit   = max(1, min(int(qs.get("limit",  ["200"])[0]), 500))
            offset  = max(0, int(qs.get("offset", ["0"])[0]))
            try:
                from applypilot.database import get_connection
                conn = get_connection()
                where = ["1=1"]
                params: list = []
                if q:
                    where.append("(LOWER(question_text) LIKE ? OR LOWER(answer_text) LIKE ?)")
                    needle = f"%{q.lower()}%"
                    params += [needle, needle]
                if ats:
                    where.append("ats_slug = ?")
                    params.append(ats)
                if source:
                    where.append("answer_source = ?")
                    params.append(source)
                if outcome:
                    where.append("outcome = ?")
                    params.append(outcome)
                where_sql = " AND ".join(where)
                total = conn.execute(
                    f"SELECT COUNT(*) FROM qa_knowledge WHERE {where_sql}",
                    params,
                ).fetchone()[0]
                rows = conn.execute(
                    f"SELECT id, question_text, answer_text, answer_source, "
                    f"field_type, ats_slug, options_json, outcome, "
                    f"created_at, updated_at "
                    f"FROM qa_knowledge WHERE {where_sql} "
                    f"ORDER BY updated_at DESC NULLS LAST, created_at DESC "
                    f"LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
                self._json_ok({"rows": [dict(r) for r in rows], "total": total})
            except Exception as e:
                logger.debug("qa_list error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_qa_create(self):
            """Insert a new qa_knowledge row from the options-page editor."""
            body = self._read_body()
            question = (body.get("question") or "").strip()
            answer   = (body.get("answer")   or "").strip()
            if not question or not answer:
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"question and answer required")
                return
            source     = (body.get("source")     or "human").strip() or "human"
            field_type = (body.get("field_type") or "").strip() or None
            ats_slug   = (body.get("ats_slug")   or "").strip() or None
            try:
                from applypilot.database import store_qa
                row_id = store_qa(question, answer, source=source,
                                  field_type=field_type, ats_slug=ats_slug)
                self._json_ok({"status": "created", "id": row_id})
            except Exception as e:
                logger.debug("qa_create error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_qa_mutate(self):
            """Update or delete a qa_knowledge row by ID.

            Path: /api/qa/{id}. Body action: "update" | "delete".
            Update fields: question_text, answer_text, answer_source,
            field_type, options_json, ats_slug, outcome.
            """
            from datetime import datetime as _dt, timezone as _tz
            from applypilot.database import get_connection, question_key
            try:
                row_id = int(self.path.rsplit("/", 1)[-1])
            except ValueError:
                self.send_response(400)
                self.end_headers()
                return
            body = self._read_body()
            action = (body.get("action") or "").strip()
            try:
                conn = get_connection()
                if action == "delete":
                    conn.execute("DELETE FROM qa_knowledge WHERE id = ?", (row_id,))
                    commit_with_retry(conn)
                    self._json_ok({"status": "deleted", "id": row_id})
                    return
                if action == "update":
                    fields = []
                    params: list = []
                    for col in (
                        "question_text", "answer_text", "answer_source",
                        "field_type", "options_json", "ats_slug", "outcome",
                    ):
                        if col in body:
                            fields.append(f"{col} = ?")
                            params.append(body[col] or None)
                            # Recompute question_key whenever question_text changes
                            if col == "question_text" and body[col]:
                                fields.append("question_key = ?")
                                params.append(question_key(body[col]))
                    if not fields:
                        self.send_response(400)
                        self.end_headers()
                        return
                    fields.append("updated_at = ?")
                    params.append(_dt.now(_tz.utc).isoformat())
                    params.append(row_id)
                    conn.execute(
                        f"UPDATE qa_knowledge SET {', '.join(fields)} WHERE id = ?",
                        params,
                    )
                    commit_with_retry(conn)
                    self._json_ok({"status": "updated", "id": row_id})
                    return
                self.send_response(400)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(b"action must be 'update' or 'delete'")
            except Exception as e:
                logger.debug("qa_mutate error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_integrations(self):
            """Health check for backend integrations (Gmail, ATS sessions).

            Powers the Integrations section of the options page.
            """
            try:
                from applypilot.apply.chrome import list_ats_sessions
                payload = {
                    "gmail": _gmail_status(),
                    "ats_sessions": list_ats_sessions(),
                }
                self._json_ok(payload)
            except Exception as e:
                logger.debug("integrations error: %s", e)
                self.send_response(500)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(str(e).encode())

        def _handle_gmail_reauth(self):
            """Direct OAuth to the shared controlled dashboard setup flow."""
            self._json_ok(_trigger_gmail_reauth())


        def log_message(self, format, *args):
            pass  # Suppress HTTP logging

    # Retry binding to the preferred port for up to 5s (old process may be dying).
    # Fall back to a random port only as a last resort, with a warning.
    preferred_port = port
    server = None
    for _attempt in range(6):
        try:
            server = _ThreadedHTTPServer(("127.0.0.1", port), _Handler)
            break
        except OSError:
            if _attempt < 5:
                time.sleep(1)
            else:
                server = _ThreadedHTTPServer(("127.0.0.1", 0), _Handler)
                port = server.server_address[1]
                logger.warning(
                    "Worker %d: preferred port %d was busy; using random port %d "
                    "(extension will not connect — restart the pipeline to fix)",
                    worker_id, preferred_port, port,
                )

    with _worker_server_lock:
        _worker_servers[worker_id] = server

    thread = threading.Thread(target=server.serve_forever, daemon=True,
                              name=f"worker-http-w{worker_id}")
    thread.start()
    logger.debug("Worker listener for worker %d on port %d", worker_id, port)
    return port


def _stop_worker_listener(worker_id: int) -> None:
    """Shut down a worker's always-on HTTP server."""
    with _worker_server_lock:
        server = _worker_servers.pop(worker_id, None)
    if server:
        server.shutdown()
    with _worker_state_lock:
        _worker_state.pop(worker_id, None)
    _takeover_events.pop(worker_id, None)
    _handback_events.pop(worker_id, None)
    task = _mini_tasks.pop(worker_id, None)
    if task:
        task.stop()


# ---------------------------------------------------------------------------
# Gmail token refresh
# ---------------------------------------------------------------------------

def _gmail_status() -> dict:
    """Report the SAME credential selection used by onboarding and every worker.

    An expired access token is refreshable, not proof of revoked OAuth. This is
    a local configuration report; only a live read probe can verify connectivity.
    """
    from applypilot import config
    from applypilot.onboarding.gmail_state import status
    from applypilot.onboarding.resolution import application_values
    from applypilot.onboarding.diagnostics import explain
    try:
        result = status(config.APP_DIR, application_values(config.APP_DIR)[0])
        return {**result, "status": "reconnect_required" if result["reconnect_required"] else
                "configured" if result["configured"] else "missing"}
    except Exception as exc:
        return {"configured": False, "status": "error", **explain(exc, component="gmail")}


def _trigger_gmail_reauth() -> dict:
    """Do not launch a second OAuth implementation against a hard-coded folder."""
    return {"status": "setup_required", "sent": 0, "message":
            "Open ApplyPilot dashboard → Setup → Gmail. Stop the engine, use Connect "
            "for the saved account, then Verify. Existing client files are retained; "
            "application workers and referrals use this same connection."}


def _refresh_gmail_token() -> bool:
    """Compatibility prerequisite check; refresh is owned by the shared transport."""
    return bool(_gmail_status().get("configured"))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int, worker_id: int = 0) -> dict:
    """Build MCP config dict for a specific CDP port.

    Passes the real Chrome user-agent to Playwright MCP so it doesn't
    override our Chrome --user-agent flag with its default
    "HeadlessChrome" UA when connecting via CDP.

    The viewport is synced with the Chrome window size chosen for this
    worker (see chrome._pick_viewport / get_worker_viewport).
    """
    from applypilot.apply.chrome import _get_real_user_agent, get_worker_viewport
    from applypilot.tracking.gmail_read_mcp import mcp_config as gmail_mcp_config

    vp = get_worker_viewport(worker_id)
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    # Pinned (was @latest — every apply run re-resolved the tag,
                    # so a compromised release would be picked up within hours).
                    # Bump deliberately after a release has soaked ~2 weeks;
                    # check `npm view @playwright/mcp@<v> dist.attestations.url`.
                    "@playwright/mcp@0.0.75",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={vp[0]}x{vp[1]}",
                    f"--user-agent={_get_real_user_agent()}",
                ],
            },
            "gmail": gmail_mcp_config(),
        }
    }


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _db_retry_execute(conn: "postgres.Connection", sql: str,
                      params: tuple = (), timeout: float = 300.0) -> "postgres.Cursor":
    """Compatibility name: execute once; only the transaction owner may retry.

    PostgreSQL statement/lock deadlines bound this call. A failed transaction
    must be rolled back before an explicitly safe whole-unit retry.
    """
    return conn.execute(sql, params)


def _db_retry_commit(conn: "postgres.Connection", timeout: float = 300.0) -> None:
    """Never replay an ambiguous commit."""
    commit_with_retry(conn)






def mark_result(url: str, status: str, error: str | None = None,
                duration_ms: int | None = None,
                task_id: str | None = None, error_detail: str = "") -> None:
    """Record one adapter outcome. Temporal, not this row, decides any later work."""
    from applypilot.apply.guard import (GuardRejected, _load_attempt, browser_activity_uncertain,
        require_verification, attempt_status, cancel_before_submit)
    conn = get_connection()
    initial = attempt_status(task_id, conn=conn)
    if initial["job_url"] != url:
        raise GuardRejected("result does not match its application")
    if initial["status"] == "prepared":
        cancel_before_submit(task_id, reason=error or status, detail=error_detail, conn=conn)
    conn.execute("BEGIN")
    try:
        attempt = _load_attempt(conn, task_id)
        if attempt["job_url"] != url:
            raise GuardRejected("result does not match its application")
        if attempt["result_recorded_at"] or attempt["status"] in {"submitted", "verification_required", "verification_quarantined"}:
            conn.rollback()
            return
        if status == "applied":
            raise GuardRejected("only verified external evidence can mark an application submitted")
        if attempt["status"] == "side_effect_intent" or browser_activity_uncertain(dict(attempt)):
            conn.rollback()
            require_verification(task_id, reason="unconfirmed_submission", detail=error_detail or error or "", quarantine=True, conn=conn)
            return
        if attempt["status"] not in {"prepared", "cancelled_before_submit"}:
            raise GuardRejected("attempt no longer accepts a result")
        from applypilot.operations.runtime import sanitize
        now = datetime.now(timezone.utc).isoformat()
        detail = sanitize(error_detail or error or status, 4000)
        conn.execute("UPDATE application_attempts SET status='cancelled_before_submit', updated_at=?,result_recorded_at=?, "
                     "verification_reason=? WHERE attempt_id=?", (now, now, error or status, task_id))
        state = "apply_failed"
        category = categorize_apply_result("failed", error)
        conn.execute("UPDATE jobs SET apply_status=?,apply_error=?,apply_attempts=COALESCE(apply_attempts,0)+1,"
                     "apply_duration_ms=?,apply_category=? WHERE url=? AND application_attempt_id=?",
                     ("failed", detail, duration_ms, category, url, task_id))
        transition_state(conn, url, state, reason=detail[:200],
                         metadata={"attempt_id": task_id, "category": category, "reason_code": error}, force=True)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        close_connection()


def end_attempt(url: str, task_id: str) -> None:
    """Release only this reservation; never reopen uncertain/submitted work."""
    from applypilot.apply.guard import (
        GuardRejected, attempt_status, cancel_before_submit, require_verification,
    )
    try:
        attempt = attempt_status(task_id)
        if attempt["job_url"] != url:
            raise GuardRejected("release job does not match reservation")
        if attempt["status"] == "prepared":
            cancel_before_submit(task_id, reason="worker released before submit")
        elif attempt["status"] == "side_effect_intent":
            require_verification(task_id, reason="worker_stopped_after_submit_intent",
                                 detail="worker stopped after submit intent", quarantine=True)
    except GuardRejected:
        logger.info("Release refused for stale reservation %s", task_id)




# ---------------------------------------------------------------------------
# Utility modes (--mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    from applypilot.apply.queue import ACTIVE_ATTEMPTS
    conn.execute("BEGIN")
    conn.advisory_lock("application-admission")
    if conn.execute(
        "SELECT 1 FROM application_attempts WHERE job_url=? "
        f"AND status IN ({ACTIVE_ATTEMPTS})", (url,),
    ).fetchone():
        conn.rollback()
        conn.close()
        raise RuntimeError("guarded application requires token-aware reconciliation")
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_category = 'applied'
            WHERE url = ?
        """, (now, url))
    else:
        error = reason or "manual"
        category = categorize_apply_result("failed", error)
        _db_retry_execute(conn, """
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, apply_recovery_credit = 0, agent_id = NULL,
                           apply_category = ?
            WHERE url = ?
        """, (error, category, url))
    
    state_map = {
        "applied":      "applied",
        "failed":       "apply_failed",
        "manual":       "manual_only",
    }
    target = state_map.get(status)
    if target:
        transition_state(conn, url, target,
            reason=f"manually marked {status} via CLI",
            force=True)
    else:
        logger.warning("mark_job: unknown status %r, no state transition emitted", status)
    _db_retry_commit(conn)


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    from applypilot.apply.queue import ACTIVE_ATTEMPTS
    conn.execute("BEGIN")
    try:
        conn.advisory_lock("application-admission")
        urls = [row[0] for row in conn.execute(
            "SELECT url FROM jobs j WHERE j.apply_status='failed' AND j.agent_id IS NULL "
            "AND COALESCE(j.state,'') NOT IN ('applied','applying','verification_required',"
            "'verification_quarantined') "
            "AND NOT EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url "
            f"AND aa.status IN ({ACTIVE_ATTEMPTS}))",
        )]
        for url in urls:
            conn.execute(
                "UPDATE jobs SET apply_status=NULL, apply_error=NULL, apply_attempts=0, apply_recovery_credit=0, "
                "apply_category=NULL WHERE url=?", (url,),
            )
            transition_state(conn, url, "ready_to_apply", reason="failed job reset", force=True)
        commit_with_retry(conn)
        return len(urls)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _reset_browser_tabs(port: int, navigate_url: str | None = None) -> None:
    """Close all existing tabs and open a fresh tab via CDP.

    If *navigate_url* is provided, open that URL directly — the agent can
    start with a snapshot instead of browser_navigate.  Otherwise opens
    about:blank (Playwright MCP path).

    Prevents leftover tabs from a previous job confusing the next agent.
    Skips chrome:// internal pages (omnibox overlays etc.) to avoid side effects.
    """
    import urllib.request
    try:
        data = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=3).read()
        tabs = json.loads(data)
        # Only close http/https/about: pages — never touch chrome:// internal targets
        closeable = [
            t for t in tabs
            if t.get("type") == "page"
            and not t.get("url", "").startswith("chrome://")
        ]
        if not closeable:
            return
        # Open a fresh tab — pre-navigated to the job URL when available,
        # otherwise about:blank (Playwright MCP will navigate it).
        fresh_url = navigate_url or "about:blank"
        req = urllib.request.Request(
            f"http://localhost:{port}/json/new?{fresh_url}", method="PUT"
        )
        urllib.request.urlopen(req, timeout=5)
        # Close all old closeable tabs
        for tab in closeable:
            try:
                urllib.request.urlopen(
                    f"http://localhost:{port}/json/close/{tab['id']}", timeout=2
                )
            except Exception:
                pass
    except Exception:
        pass  # Chrome not ready yet or CDP unavailable — agent will navigate anyway


def _activate_agent_tab(port: int, timeout: float = 300.0) -> None:
    """Background thread: activate the first real (non-blank) page the agent navigates to.

    Playwright MCP creates a new tab for the agent rather than reusing the existing
    about:blank tab. Without this, the user's visible Chrome tab stays on about:blank
    while the agent works in a background tab.
    """
    import urllib.request
    deadline = time.time() + timeout
    activated_url = None
    while time.time() < deadline:
        try:
            data = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=2).read()
            tabs = json.loads(data)
            for tab in tabs:
                url = tab.get("url", "")
                if (tab.get("type") == "page"
                        and url
                        and not url.startswith("about:")
                        and not url.startswith("chrome://")
                        and not url.startswith("http://localhost:738")):
                    tab_id = tab.get("id")
                    if tab_id and url != activated_url:
                        urllib.request.urlopen(
                            f"http://localhost:{port}/json/activate/{tab_id}", timeout=2
                        )
                        activated_url = url
                        # Playwright MCP commonly creates a second page and
                        # leaves Chrome's bootstrap blank tab visible. Once a
                        # real application page exists, close only blank tabs.
                        for other in tabs:
                            if other.get("type") == "page" and other.get("url", "").startswith("about:blank"):
                                try:
                                    urllib.request.urlopen(
                                        f"http://localhost:{port}/json/close/{other['id']}", timeout=2
                                    )
                                except Exception:
                                    pass
                        # Keep watching in case the agent opens a new tab mid-job
                    break
        except Exception:
            pass
        time.sleep(0.75)


def _cleanup_extra_tabs(port: int) -> None:
    """Post-job consolidation: leave exactly one closable page tab open.

    Sites frequently open the application form in a second tab, leaving the
    landing tab behind. Runs only AFTER the agent process exits, so it can
    never interrupt a live session. Playwright-owned worker Chrome only.
    """
    import urllib.request
    try:
        data = urllib.request.urlopen(f"http://localhost:{port}/json", timeout=3).read()
        tabs = [
            t for t in json.loads(data)
            if t.get("type") == "page"
            and not t.get("url", "").startswith("chrome://")
        ]
        if len(tabs) <= 1:
            return
        # Prefer keeping a real content tab over about:blank.
        tabs.sort(key=lambda t: t.get("url", "").startswith("about:"))
        for extra in tabs[1:]:
            try:
                urllib.request.urlopen(
                    f"http://localhost:{port}/json/close/{extra['id']}", timeout=2
                )
            except Exception:
                pass
    except Exception:
        pass  # Chrome already closed or CDP gone — nothing to consolidate


def run_job(job: dict, port: int, worker_id: int = 0,
            dry_run: bool = False,
            skip_tab_reset: bool = False,
            extra_context: str | None = None,
            runtime_selection=None,
            attempt_id: str | None = None) -> tuple[str, int, list[dict]]:
    """Run one governed application in the Claude Agent SDK.

    Args:
        job: Job dict from the database.
        port: CDP port for the worker's Chrome.
        worker_id: Numeric worker identifier.
        dry_run: If True, don't click Submit.
        skip_tab_reset: If True, don't close leftover tabs (used when continuing the same form).
        extra_context: Optional instructions from a previous human takeover, prepended
            to the agent prompt so it knows what was done.

    Returns:
        Tuple of (status_string, duration_ms, screening_questions).
        screening_questions is a list of dicts with keys: question, field_type, options.
    """
    from applypilot.apply.runtime import (
        allowed_tools,
        build_browser_mcp_config,
        record_browser_service_failure,
        select_runtime,
    )

    from applypilot.operations.worker_journal import emit as activity, job_id as trace_job_id
    attempt_id = attempt_id or job.get("_attempt_id")
    from applypilot.apply.guard import (
        attempt_status, defer_application, terminal_result,
    )
    saved_attempt = attempt_status(attempt_id) if attempt_id else None

    def guarded_outcome(attempt: dict, duration_ms: int) -> tuple[str, int, list[dict]] | None:
        terminal = terminal_result(attempt)
        if terminal is None:
            return None
        if terminal["committed"]:
            return "guarded_applied", duration_ms, []
        if terminal["guard_status"] == "cancelled_before_submit":
            recorded = get_connection().execute("SELECT apply_error FROM jobs WHERE url=? AND application_attempt_id=?",
                                                (job["url"], attempt_id)).fetchone()
            if recorded and recorded[0]:
                job["_failure_detail"] = recorded[0]
            return "failed:" + terminal["reason"], duration_ms, []
        return "guarded_verification", duration_ms, []

    def finish_attempt(reason: str, duration_ms: int, *, detail: str = "",
                       quarantine: bool = False) -> tuple[str, int, list[dict]]:
        """Release pre-submit failures and retain uncertain or confirmed submissions."""
        job["_failure_detail"] = detail or reason
        if not attempt_id:
            return f"failed:{reason}", duration_ms, []
        attempt = attempt_status(attempt_id)
        if outcome := guarded_outcome(attempt, duration_ms):
            return outcome
        attempt = defer_application(attempt_id, reason, detail=detail)
        if attempt["status"] == "cancelled_before_submit":
            # Keep provider admission details for the orchestrator's retry policy.
            return f"failed:{reason}", duration_ms, []
        return guarded_outcome(attempt, duration_ms) or ("failed:guard_terminal_state", duration_ms, [])

    if saved_attempt and (terminal_outcome := guarded_outcome(saved_attempt, 0)):
        return terminal_outcome
    if not dry_run and not attempt_id:
        raise ValueError("A prepared Temporal application attempt is required")
    if job.get("application_route") == "email" or (saved_attempt and saved_attempt["channel"] == "email"):
        from applypilot.apply.email_application import run_email_job
        return run_email_job(job, worker_id, attempt_id, dry_run=dry_run)
    selection = runtime_selection or select_runtime()
    from applypilot.apply.guard import GuardRejected, bind_uploaded_artifacts, attempt_snapshot, validate_attempt
    if not dry_run:
        job["_attempt_id"] = attempt_id
        attempt = validate_attempt(attempt_id)
        if (attempt["job_url"] != job["url"] or attempt["browser_runtime"] != selection.selected
                or attempt_snapshot(job) != attempt["snapshot_json"]):
            raise GuardRejected("job, approved artifacts or browser runtime changed")

    close_connection()
    if selection.selected == "browseros":
        from applypilot.apply.browseros_mcp import read_state
        from applypilot.apply.runtime import probe_browser_bridge
        from applypilot.operations.runtime import sanitize
        session_file = config.APP_DIR / "browseros-sessions" / f"{attempt_id}.json"
        previous_bridge = read_state(session_file)
        if previous_bridge and not (previous_bridge.get("session") and previous_bridge.get("resumable") is not False and previous_bridge.get("cleanup") != "complete"):
            attempt = attempt_status(attempt_id) if attempt_id else {}
            if attempt.get("status") in {"side_effect_intent", "verification_required", "verification_quarantined"}:
                return finish_attempt("browser_bridge_unavailable", 0, quarantine=True,
                                      detail="BrowserOS connection ended; same-form resume unavailable")
            from applypilot.apply.guard import browser_activity_uncertain
            if browser_activity_uncertain(attempt):
                return finish_attempt("unconfirmed_submission", 0, quarantine=True,
                                      detail="Browser connection ended after an unconfirmed submit")
            skip_tab_reset = False  # A clean pre-submit restart gets a fresh page.
        try:
            selection = probe_browser_bridge(selection)
            if dry_run and not {"tabs", "snapshot", "read"} <= set(selection.advertised_tools):
                raise RuntimeError("browser_bridge_unavailable: read-only dry run requires granular inspection tools")
        except Exception as exc:
            detail = sanitize(exc, 700)
            from applypilot.operations import capture
            capture(exc, component_name="applications", operation="browser_stdio_preflight", stage="applications")
            logger.warning("BrowserOS stdio preflight: %s", detail)
            config.LOG_DIR.mkdir(parents=True, exist_ok=True)
            with (config.LOG_DIR / f"worker-{worker_id}.log").open("a", encoding="utf-8") as failure_log:
                failure_log.write("BrowserOS stdio startup failure: " + detail + "\n")
            return finish_attempt("browser_bridge_unavailable", 0, detail=detail)
        update_state(worker_id, browser_session_file=str(session_file), selected_runtime="browseros")

    # BrowserOS pages belong to the reservation's MCP session, not a worker
    # CDP port. Its agent opens a fresh owned tab through the pinned session.
    if not skip_tab_reset and selection.selected == "playwright":
        _reset_browser_tabs(port)

    # Hard dry-run gate: install a CDP-side script that blocks form
    # submits and clicks on submit-style buttons. Belt to the prompt's
    # suspenders — even if the model ignores the prompt instruction
    # (Haiku has done this), the click never reaches a handler.
    if dry_run and selection.selected == "playwright":
        try:
            from applypilot.apply.chrome import inject_dry_run_gate
            inject_dry_run_gate(port)
        except Exception:
            logger.debug("dry_run gate injection failed; falling back to prompt-only",
                         exc_info=True)

    # Wipe stale resume / cover-letter / MCP-config copies from the previous
    # job BEFORE build_prompt writes the fresh files. Earlier this wipe sat
    # AFTER build_prompt and silently deleted the resume the agent was
    # about to upload — agent then burned ~5 tool calls hunting for files
    # in the legacy `current/` directory before copying them into worker-0.
    attempt_dir = config.APPLY_WORKER_DIR / "attempts" / attempt_id if attempt_id else None
    if attempt_dir is None:
        reset_worker_dir(worker_id)
    else:
        attempt_dir.mkdir(parents=True, exist_ok=True)

    from applypilot.apply.agent_prompt import (
        APPLICATION_RESULT_SCHEMA,
        build_compact_prompt,
        prepare_context,
    )

    context = prepare_context(job, worker_id, job.get("_doc_format", _doc_format), workspace_dir=attempt_dir)
    worker_dir = attempt_dir or config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    from applypilot.apply.agent_cli import prepare_application_skills
    application_skills = prepare_application_skills(worker_dir)
    browser_skill = None
    if selection.selected == "browseros":
        from applypilot.apply.agent_cli import prepare_browser_skill
        browser_skill = prepare_browser_skill(worker_dir)
    for staged_skill in ([browser_skill] if browser_skill else []) + application_skills:
        activity("agent.skill_staged", worker_id=worker_id, stage="applications",
                 job_id=trace_job_id(job["url"]), origin="launcher", snapshot=staged_skill,
                 skill_name=staged_skill.get("name"), skill_source=staged_skill.get("source"),
                 skill_revision=staged_skill.get("revision"), skill_sha256=staged_skill.get("sha256"))
    if not dry_run:
        bind_uploaded_artifacts(attempt_id, context["documents"])
    close_connection()
    with _worker_state_lock:
        context["unattended"] = True
    context["session"] = {
        "browser_runtime": selection.selected,
        "resuming_after_takeover": bool(skip_tab_reset),
        "human_instructions": extra_context or "",
    }
    from applypilot.reasoning.source_files import SourceFiles
    from uuid import uuid4
    context_key = attempt_id or ("dry-run-" + uuid4().hex)
    context_store = SourceFiles(config.APP_DIR / "application-contexts" / context_key)
    context_store.seed(context, "application")
    for source_file in (worker_dir / ".claude" / "skills").rglob("*.md"):
        context_store.put("skill " + source_file.relative_to(worker_dir).as_posix(),
                          source_file.read_text(encoding="utf-8"))
    agent_model = agent_model_name()
    from applypilot.reasoning.application_policy import selected as selected_application_policy
    policy_task = "application_inspect" if dry_run else "application_agent"
    policy_text, policy_revision = selected_application_policy(model=agent_model, task_kind=policy_task)
    agent_prompt = build_compact_prompt(
        context=context,
        behavior_instructions=policy_text,
        selection=selection,
        attempt_id=attempt_id or "dry-run",
        dry_run=dry_run,
    )

    context_store.put("application task " + str(time.time_ns()), agent_prompt)

    # Gmail connects lazily through the shared read-only bridge. The transport
    # refreshes its selected credential files; workers never rewrite OAuth here.

    mcp_configuration = build_browser_mcp_config(
        selection, cdp_port=port, worker_id=worker_id, attempt_id=attempt_id,
        context_root=str(context_store.root),
    )
    # User takeover mini-tasks consume the same owned MCP configuration.
    mcp_config_path = (worker_dir / '.mcp-apply.json' if attempt_id else
                       config.APP_DIR / f".mcp-apply-{worker_id}.json")
    mcp_config_path.write_text(json.dumps(mcp_configuration), encoding="utf-8")

    from applypilot.agent_skills import (
        APPLICATION_EXECUTION_TOOLS, APPLICATION_READ_TOOLS, application_skill_instructions,
    )
    from applypilot.apply.agent_cli import APPLICATION_SYSTEM_RULES
    skill_rules = (
        "Inspect the assigned application with the selected browser's read-only tools. "
        "Retain the owned page ID and inspect with snapshot/read/grep. "
        "Do not fill, upload or submit. Follow advertised schemas; never request shell access."
        if dry_run else APPLICATION_SYSTEM_RULES
    )
    skill_rules += "\n" + application_skill_instructions(browseros=bool(browser_skill), dry_run=dry_run)
    entry_skill = browser_skill or (None if dry_run else next(
        skill for skill in application_skills if skill["name"] == "apply"
    ))
    if entry_skill:
        # Native slash-skill expansion loads the entry skill before the task.
        agent_prompt = "/" + entry_skill["name"] + " " + agent_prompt

    env = os.environ.copy()
    from applypilot.execution import current_task
    if current_task():
        env["APPLYPILOT_ACTIVITY_ID"] = current_task()["key"]
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    from applypilot.gateway_policy import configure_agent_environment
    configure_agent_environment(env, model=agent_model)
    env.setdefault("ANTHROPIC_API_KEY", "")
    import hashlib
    application_id = hashlib.sha256(str(attempt_id or f"dry-run-{worker_id}").encode()).hexdigest()[:32]
    env["APPLYPILOT_APPLICATION_ID"] = application_id
    from uuid import uuid4
    from applypilot.apply.agent_trace import AgentTrace
    trace_id = uuid4().hex
    trace = AgentTrace(trace_id)
    trace.emit("launch", application_id=application_id, worker_id=worker_id, model=agent_model,
               browser=selection.selected)
    job["_trace_id"] = trace_id
    env["APPLYPILOT_AGENT_TRACE_ID"] = trace_id
    from applypilot.reasoning.learning_store import start_episode, finish_episode
    start_episode(trace_id, group_id=str(job.get("url") or attempt_id or trace_id),
                  attempt_id=attempt_id or "", prompt=agent_prompt, system_prompt=skill_rules,
                  model=agent_model, context_root=context_store.root,
                  revision=policy_revision, task_kind=policy_task,
                  skill_files={path.relative_to(worker_dir / ".claude" / "skills").as_posix():
                               path.read_text(encoding="utf-8-sig")
                               for path in (worker_dir / ".claude" / "skills").rglob("*.md")
                               if not path.is_symlink()})

    custom_headers = [line for line in env.get("ANTHROPIC_CUSTOM_HEADERS", "").splitlines()
                      if not line.lower().startswith("x-applypilot-application-id:")]
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join([*custom_headers, f"X-ApplyPilot-Application-ID: {application_id}"])

    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("company") or job.get("site", ""), score=job.get("fit_score", 0),
                 start_time=time.time(), actions=0, last_action="starting",
                 form_progress_at=0.0, model_activity_at=0.0, model_wait_since=time.time(), transport_at=0.0,
                 trace_id=trace_id)
    add_event(f"[W{worker_id}] Starting: {(job.get('title') or '')[:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / (f"application-{attempt_id}.log" if attempt_id else f"worker-{worker_id}.log")
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {job.get('fit_score', 'N/A')}/10\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    infrastructure_failure = None
    guard_terminal_result = None
    timeout_reason = None

    def check_activity(session_ready):
        nonlocal infrastructure_failure, guard_terminal_result
        from applypilot.execution import current_task
        stop = (current_task() or {}).get("stop_event")
        if stop is not None and stop.is_set():
            infrastructure_failure = "activity_cancelled"
            return infrastructure_failure
        takeover = _takeover_events.get(worker_id)
        if takeover and takeover.is_set():
            return "takeover"
        if attempt_id:
            try:
                persisted = terminal_result(attempt_status(attempt_id))
            except Exception:
                persisted = None
            finally:
                close_connection()
            if persisted is not None:
                guard_terminal_result = persisted
                activity("application.guard_terminal", worker_id=worker_id, stage="applications",
                         job_id=trace_job_id(job["url"]), outcome=persisted["status"],
                         reason=persisted["reason"], origin="guard")
                return "guard_terminal"
        if session_ready and selection.selected == "browseros":
            from applypilot.apply.browseros_mcp import read_state
            bridge = read_state(session_file)
            progress = {}
            for source, field in (("last_form_progress_at", "form_progress_at"),
                                  ("last_transport_at", "transport_at")):
                try:
                    stamp = datetime.fromisoformat(bridge[source].replace("Z", "+00:00")).timestamp()
                    if stamp > 0:
                        progress[field] = stamp
                except (KeyError, TypeError, ValueError, AttributeError):
                    pass
            if progress:
                update_state(worker_id, **progress)
            terminal = bridge.get("terminal_reason")
            if terminal or bridge.get("state") == "failed":
                infrastructure_failure = terminal or "browser_bridge_unavailable"
                activity("application.browser_session_lost", worker_id=worker_id, stage="applications",
                         code=infrastructure_failure, job_id=trace_job_id(job["url"]))
                return infrastructure_failure
        return None

    try:
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher
        from applypilot.apply.agent_sdk import run_application_session
        from applypilot.reasoning.observation import BrowserObservationProcessor
        processor = BrowserObservationProcessor(store=context_store, task_context=context,
                                               application_id=application_id, trace_id=trace_id)
        observation_started = {}

        async def observation_request(input_data, tool_use_id, hook_context):
            observation_started[tool_use_id] = time.time()
            return {}

        async def observation_result(input_data, tool_use_id, hook_context):
            return await processor(
                {**input_data, "_applypilot_request_started": observation_started.pop(tool_use_id, time.time())},
                tool_use_id, hook_context,
            )

        options = ClaudeAgentOptions(
            model=agent_model,
            max_turns=None,  # Semantic completion/cancellation, not an application turn budget.
            mcp_servers=mcp_configuration["mcpServers"],
            strict_mcp_config=True,
            permission_mode="dontAsk",
            allowed_tools=allowed_tools(selection, dry_run=dry_run),
            skills="all",
            tools=["Skill", *(APPLICATION_READ_TOOLS if dry_run else APPLICATION_EXECUTION_TOOLS)],
            system_prompt={"type": "preset", "preset": "claude_code", "append": skill_rules},
            setting_sources=["project"],
            output_format={"type": "json_schema", "schema": APPLICATION_RESULT_SCHEMA},
            cwd=worker_dir,
            env=env,
            # Raw MCP observations reach the hook before they are reduced for Claude.
            max_buffer_size=64 * 1024 * 1024,
            include_partial_messages=True,
            extra_args={"no-session-persistence": None, "debug": "mcp"},
            stderr=lambda line: trace.emit("sdk_mcp_diagnostic", text=line.rstrip()),
            hooks={
                "PreToolUse": [HookMatcher(matcher=r"^mcp__browseros_neo__.*", hooks=[observation_request])],
                "PostToolUse": [HookMatcher(matcher=r"^mcp__browseros_neo__.*", hooks=[observation_result],
                                           timeout=3660)],
            },
        )
        if entry_skill:
            activity("agent.skill_invoked", worker_id=worker_id, stage="applications",
                     job_id=trace_job_id(job["url"]), origin="agent",
                     snapshot={**entry_skill, "application_id": application_id},
                     skill_name=entry_skill.get("name"), skill_source=entry_skill.get("source"),
                     skill_revision=entry_skill.get("revision"), skill_sha256=entry_skill.get("sha256"))
        if selection.selected == "playwright":
            threading.Thread(target=_activate_agent_tab, args=(port,), daemon=True,
                             name=f"tab-activator-w{worker_id}").start()
        with worker_log.open("a", encoding="utf-8", buffering=1) as log_file:
            log_file.write(log_header)
            log_file.write("  Application correlation: " + application_id + "\n")
            log_file.write(f"  Tool permissions: {options.permission_mode}; built-ins: {', '.join(options.tools)}\n")
            log_file.write("  MCP servers: " + ", ".join(sorted(options.mcp_servers)) + "\n")
            log_file.write("  Native application skills: " + json.dumps(application_skills) + "\n")
            session = run_application_session(
                options=options, prompt=agent_prompt, worker_id=worker_id, job_url=job["url"],
                application_id=application_id, trace=trace, log_file=log_file,
                check_owner=check_activity,
            )
        text_parts = session.text
        structured_result = session.structured_output
        stats = session.stats
        tool_calls = session.tool_calls
        admission_failure = session.admission_failure
        infrastructure_failure = infrastructure_failure or session.infrastructure_failure
        if session.stop_reason == "activity_cancelled":
            infrastructure_failure = infrastructure_failure or "activity_cancelled"
        if session.stop_reason in {"agent_job_timeout", "agent_progress_timeout", "agent_idle_timeout"}:
            timeout_reason = session.stop_reason
        if selection.selected == "browseros":
            from applypilot.apply.browseros_mcp import read_state
            terminal_bridge = read_state(session_file)
            if terminal_bridge.get("terminal_reason") and guard_terminal_result is None:
                infrastructure_failure = terminal_bridge["terminal_reason"]
            elif (terminal_bridge.get("state") in {"starting", "failed"} or not terminal_bridge) and guard_terminal_result is None:
                infrastructure_failure = infrastructure_failure or "browser_bridge_unavailable"

        # SDK teardown completes before the browser is handed back to the user.
        tev = _takeover_events.get(worker_id)
        if tev and tev.is_set():
            return "takeover", int((time.time() - start) * 1000), []

        output = "\n".join(text_parts)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if not dry_run:
            _parse_account_created(output, job.get("url"))
            _parse_qa_lines(output, job_url=job.get("url"),
                            ats_slug=detect_ats(job.get("application_url") or job.get("url") or ""))

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        if attempt_id and selection.selected == "browseros" and not dry_run:
            # A success report can outlive a toast, browser connection, or guard
            # helper. Persist it before terminal/error handling hides the result.
            report = structured_result
            if report is None:
                final_text = next((part for part in reversed(text_parts) if part.strip()), "")
                report = _submission_report_from_output(final_text)
            if report and report.get("status") in {"submitted", "already-submitted"}:
                from applypilot.apply.guard import record_submission_report
                try:
                    record_submission_report(attempt_id, report, duration_ms=duration_ms)
                except GuardRejected:
                    logger.warning("Could not record submission report for %s", attempt_id, exc_info=True)

        if attempt_id:
            try:
                if outcome := guarded_outcome(attempt_status(attempt_id), duration_ms):
                    return outcome
            except Exception:
                logger.debug("Could not read terminal application guard", exc_info=True)

        if timeout_reason:
            return finish_attempt(timeout_reason, duration_ms)

        if structured_result is not None:
            status_name = str(structured_result.get("status") or "incomplete")
            reason = str(structured_result.get("reason") or status_name)
            # Persist runtime/tool telemetry without putting page/form content
            # into the metrics table.
            try:
                metrics_conn = get_connection()
                metrics_conn.execute(
                    """
                    INSERT INTO agent_run_metrics
                        (job_url, worker_id, browser_runtime, started_at, finished_at,
                         input_tokens, output_tokens, cache_read, tool_calls,
                         snapshots, screenshots, duration_ms, outcome)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.get("url"), worker_id, selection.selected,
                        datetime.fromtimestamp(start, timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                        stats.get("input_tokens", 0), stats.get("output_tokens", 0),
                        stats.get("cache_read", 0), len(tool_calls),
                        sum(1 for call in tool_calls if "snapshot" in call.get("tool", "")),
                        sum(1 for call in tool_calls if "screenshot" in call.get("tool", "")),
                        duration_ms, status_name,
                    ),
                )
                commit_with_retry(metrics_conn)
            except Exception:
                logger.debug("Could not persist application-agent metrics", exc_info=True)

        if dry_run or (structured_result is not None and status_name == "dry-run"):
            return "skipped", duration_ms, []

        if structured_result is not None and status_name in {"submitted", "already-submitted"}:
            reason = ("unconfirmed_submission" if status_name == "submitted"
                      else "preexisting_submission_unconfirmed")
            return finish_attempt(reason, duration_ms, quarantine=True,
                                  detail="agent reported submission without committed proof")

        if admission_failure:
            reason = str(admission_failure.get("message") or admission_failure.get("reason") or session.error_detail)
        else:
            reason = infrastructure_failure or (reason if structured_result is not None else "no_structured_result")
        return finish_attempt(reason, duration_ms, detail=session.error_detail)

    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        reason = "agent_cli_terminal_error"
        infrastructure_failure = infrastructure_failure or reason
        action = f"ERROR: {str(exc)[:40]}"
        add_event(f"[W{worker_id}] {action}")
        update_state(worker_id, status="failed", last_action=action)
        try:
            finish_attempt(reason, duration_ms, detail=str(exc)[:700])
        except Exception:
            logger.exception("Could not finalize guarded agent failure: %s", reason)
        raise
    finally:
        record_browser_service_failure(selection, infrastructure_failure)
        trace.emit("session_ended", infrastructure_failure=infrastructure_failure)
        try:
            learning_attempt = attempt_status(attempt_id) if attempt_id else {}
            finish_episode(trace_id, status=str((learning_attempt or {}).get("status", "unknown")),
                           proof_kind=str((learning_attempt or {}).get("proof_kind", "")),
                           infrastructure=bool(infrastructure_failure))
        except Exception as learning_error:
            trace.emit("learning_finalize_failed", error_type=type(learning_error).__name__)
        trace.close()
        from applypilot.reasoning.rlm_runtime import cleanup_application_sandboxes
        cleanup_application_sandboxes(application_id)

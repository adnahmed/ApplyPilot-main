"""Best-effort worker snapshots, separate from job admission and submission state.

The hot path changes small in-memory records only. The existing journal writer
publishes them. A heartbeat means reporting, not progress; only actual boundary
and phase changes update progress_at. No prompts, messages or URLs are accepted.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import socket
import threading
import time
import weakref

# Actual executing units. A stage coordinator waiting on a batch is NOT an
# additional job worker. Nested units restore their parent on completion.
UNITS = {
    ("enrich", "job"), ("score", "job"), ("tailor", "job"),
    ("cover", "job"), ("brief", "job"), ("pdf", "document"),
    ("company_research", "job"), ("discovery", "fetch_page"),
    ("tracking", "run"), ("unattended", "cycle"),
    ("backup", "sync"), ("referrals", "prepare"), ("outreach", "deliver"),
    ("network_research", "case"), ("network_research", "connections"),
}
PHASES = {"working", "waiting_provider", "waiting_capacity", "navigating", "reading_page", "saving", "checking_inbox"}
MAX_INACTIVE_WORKERS = 256


@dataclass
class Handle:
    key: str
    span_id: str
    previous: dict | None = None


ISSUE_LABELS = {
    "model_output_incomplete": "Model returned no usable visible output",
    "model_invalid_json": "Model response was not valid JSON",
    "model_schema_error": "Model response fields failed validation",
    "model_bridge_timeout": "Local model-call bridge timed out",
    "provider_concurrency_limit": "Provider rejected concurrent requests",
    "extraction_empty": "Reader returned no usable job details",
    "extraction_empty_page": "Page had no usable text for extraction",
    "inbox_backlog": "Email writes paused: saved inbox messages await processing",
    "document_renderer_fallback": "LaTeX rendering failed; Chromium fallback attempted",
    "document_latex_missing_field": "LaTeX template is missing a required field",
    "document_validation": "Generated document failed validation",
    "source_http_404": "Job page returned HTTP 404 (not found)",
    "source_http_410": "Job page returned HTTP 410 (gone)",
    "source_dns": "Job hostname could not be resolved",
    "gmail_transport_access": "Gmail subprocess reported a file/program access failure",
    "postgres_query_cancelled": "Dashboard database read was interrupted",
    "provider": "Model provider request failed", "schema": "Response format failed",
    "network": "Network request failed", "auth": "Authentication or authorization required",
    "timeout": "Operation timed out", "storage": "Database/storage operation failed",
    "policy": "A configured guard deferred or blocked work", "send_unknown": "Delivery outcome is uncertain",
    "dependency": "Required component is unavailable", "configuration": "Configuration needs attention",
    "rate_limit": "Provider rate limit reached", "security": "Security check blocked the operation",
    "cancelled": "Operation was cancelled", "none": "Operation reported a warning",
    "unknown": "Warning or failure has no recorded diagnosis",
}


def issue_code(error=None, message="", kind="unknown", component="", operation="", details=None):
    """Select a code from structured evidence; display messages are never parsed."""
    details = details or {}
    if component == "pdf" and isinstance(error, KeyError):
        return "document_latex_missing_field"
    if details.get("sqlstate", getattr(error, "sqlstate", None)) == "57014":
        return "postgres_query_cancelled"
    aliases = {"incomplete_output": "model_output_incomplete", "model_empty_response": "model_output_incomplete",
               "failed_validation": "document_validation", "extraction_provider_failure": "provider"}
    for code in (details.get("reason_code"), getattr(error, "reason_code", None), getattr(error, "code", None)):
        if not isinstance(code, str):
            continue
        code = aliases.get(code, code)
        if code in ISSUE_LABELS:
            return code
    status = details.get("http_status")
    if status is None:
        status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if type(status) is int and status in {404, 410}:
        return f"source_http_{status}"
    if isinstance(error, socket.gaierror):
        return "source_dns"
    return kind if kind in ISSUE_LABELS else "unknown"


def issue_evidence(error=None, message="", kind="unknown", component="", operation="", details=None, context=None):
    """Keep a concrete origin/correlation reference without copying model payloads."""
    from .runtime import label, sanitize
    details, context = details or {}, context or {}
    code = issue_code(error, message, kind, component, operation, details)
    summary = ISSUE_LABELS[code]
    if code == "document_latex_missing_field" and error.args:
        field = str(error.args[0])
        # Field names, not KeyError's arbitrary value/payload.
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,60}", field):
            summary += ": " + field
        if details.get("reason_code") == "document_renderer_fallback":
            summary += "; Chromium fallback attempted"
    if code == "unknown":
        summary = f"{component or 'Operation'} · {operation or 'event'}"
        summary += f" raised {type(error).__name__}" if error is not None else " reported a warning/failure"
    result = {"code": code, "summary": summary, "component": label(component), "operation": label(operation)}
    actual = str(error) if error is not None else message
    if actual:
        result["message"] = sanitize(actual, 1800)
    if code == "document_latex_missing_field" and error is not None and error.args:
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,60}", str(error.args[0])):
            result["missing_field"] = str(error.args[0])
    if error is not None:
        result["exception_type"] = type(error).__name__
    origin = details.get("origin")
    if origin:
        result["origin"] = sanitize(origin, 200)
    if context.get("span_id"):
        result["span_id"] = label(context["span_id"])
    for key in ("llm_call_id", "response_log"):
        if details.get(key):
            result[key] = sanitize(details[key], 100)
    return result


class WorkerRegistry:
    def __init__(self, process_id, run_id, pid, role="worker"):
        from applypilot.runtime_info import runtime_info
        runtime_info()
        self.process_id, self.run_id, self.pid = process_id, run_id, pid
        self.role = role
        self.lock = threading.Lock()
        self.rows = {}
        self.references = {}
        self.dropped = 0

    def _trim_inactive(self):
        """Bound retained history without limiting live task telemetry."""
        inactive = sorted((row.get("progress_at", 0), key) for key, row in self.rows.items()
                          if not row.get("active"))
        for _, key in inactive[:-MAX_INACTIVE_WORKERS]:
            self.rows.pop(key, None)
            self.references.pop(key, None)

    def _trace(self, row, event, **extra):
        from .worker_journal import emit
        emit(event, run_id=self.run_id, process_id=self.process_id,
             worker_key=row["key"], stage=row["stage"], operation=row["operation"],
             span_id=row["span_id"], job_id=row.get("job_id"), job_title=row.get("job_title"),
             company=row.get("company"), phase=row.get("phase"), state=row.get("state"),
             source=row.get("source"),
             **extra)

    def enter(self, component, operation, context):
        if (component, operation) not in UNITS:
            return None
        from .runtime import sanitize
        thread = threading.current_thread()
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
        # Async tasks can share an OS thread without being the same execution
        # slot. Thread objects distinguish thread-ID reuse by later executors.
        owner = task or thread
        identity = f"{id(owner)}:{thread.ident}:{'task' if task else 'thread'}"
        key = self.process_id + ":" + hashlib.sha256(identity.encode()).hexdigest()[:12]
        now = time.time()
        with self.lock:
            previous = self.rows.get(key)
            same_unit = previous and (previous["stage"], previous["operation"]) == (context.get("stage"), operation)
            row = {
                "key": key, "process_id": self.process_id, "pid": self.pid,
                "worker": ("Task" if task else "Thread") + " " + str(thread.ident),
                "thread_name": sanitize(thread.name, 80),
                "kind": "service" if component in {"tracking", "unattended", "backup", "referrals", "outreach"} else "preparation",
                "stage": context.get("stage") or component, "operation": operation,
                "span_id": context["span_id"], "job_id": context.get("job_id", ""),
                "job_title": sanitize(context.get("job_title"), 160),
                "company": sanitize(context.get("job_company"), 120),
                "source": sanitize(context.get("job_source"), 80),
                "active": True, "state": "running", "phase": "working",
                "started_at": now, "phase_at": now, "progress_at": now,
                "completed_operations": previous.get("completed_operations", 0) if same_unit else 0,
                "failed_operations": previous.get("failed_operations", 0) if same_unit else 0,
                "partial_operations": previous.get("partial_operations", 0) if same_unit else 0,
                "warning_events": previous.get("warning_events", 0) if same_unit else 0,
                "last_outcome": previous.get("last_outcome") if same_unit else None,
                "last_issue": None,
            }
            self.rows[key] = row
            self.references[key] = (weakref.ref(owner), bool(task))
            self._trace(row, "task.started")
            return Handle(key, context["span_id"], dict(previous) if previous and previous.get("active") else None)

    def finish(self, handle, outcome):
        now = time.time()
        with self.lock:
            row = self.rows.get(handle.key)
            if not row or row["span_id"] != handle.span_id:
                return
            issue = row.get("last_issue") or {}
            self._trace(row, "task.finished", outcome=outcome, task_elapsed_seconds=round(now-row["started_at"], 3),
                        reason=issue.get("message"), exception_type=issue.get("exception_type"),
                        severity="error" if outcome in {"failed", "partial"} else "info")
            if handle.previous:
                restored = handle.previous
                restored["progress_at"] = now
                if row.get("last_issue"):
                    restored["last_issue"] = row["last_issue"]
                self.rows[handle.key] = restored
                return
            row["active"] = False
            row["state"] = "idle"
            row["phase"] = "between_jobs"
            row["phase_at"] = row["progress_at"] = now
            row["last_outcome"] = outcome
            issue = row.get("last_issue") or {}
            if outcome == "success" and issue.get("code") in {"document_renderer_fallback", "document_latex_missing_field"}:
                row["last_issue"] = {**issue, "document_completed_after_warning": True}
            row["completed_operations"] += int(outcome == "success")
            row["failed_operations"] += int(outcome in {"failed", "partial"})
            row["partial_operations"] += int(outcome == "partial")
            row["last_job_title"] = row.pop("job_title", "")
            row["last_company"] = row.pop("company", "")
            row["job_title"] = row["company"] = ""
            self._trim_inactive()

    def issue(self, handle, code, *, evidence=None, severity="error"):
        if not handle:
            return
        with self.lock:
            row = self.rows.get(handle.key)
            if row and row["span_id"] == handle.span_id:
                if (code == "unknown" and (row.get("last_issue") or {}).get("code", "unknown") != "unknown"
                        and not (row.get('last_issue') or {}).get('resolved')):
                    return  # A generic outer summary must not erase the actual cause.
                if (not evidence and (row.get("last_issue") or {}).get("code") == code
                        and not (row.get('last_issue') or {}).get('resolved')):
                    return  # A phase-unwind summary must not erase the original origin.
                row["last_issue"] = {**(evidence or {}), "code": code, "severity": severity, "at": time.time()}
                row["warning_events"] = row.get("warning_events", 0) + int(severity == "warning")
                row["progress_at"] = time.time()
                self._trace(row, "task.issue", code=code, severity=severity,
                            reason=(evidence or {}).get("message"),
                            exception_type=(evidence or {}).get("exception_type"), origin=(evidence or {}).get("origin"))

    def recovered(self, handle, component, message):
        if not handle:
            return
        from .runtime import sanitize
        with self.lock:
            row = self.rows.get(handle.key)
            if not row or row['span_id'] != handle.span_id:
                return
            issue = row.get('last_issue') or {}
            if issue.get('component') != component:
                return
            row['last_issue'] = {**issue, 'resolved': True, 'severity': 'info',
                                 'message': sanitize(message, 500), 'summary': sanitize(message, 500)}
            row['progress_at'] = time.time()

    def model_call(self, handle, call_id, state, started_at=None):
        if not handle:
            return
        with self.lock:
            row = self.rows.get(handle.key)
            if row and row["span_id"] == handle.span_id:
                if started_at is not None:
                    row.update(model_call_id=call_id, model_call_started_at=started_at)
                if row.get("model_call_id") == call_id:
                    row.update(model_call_state=state, progress_at=time.time())
                    self._trace(row, "model." + state, call_id=call_id, call_state=state)

    @contextmanager
    def phase(self, handle, phase, task=""):
        previous = None
        if handle and phase in PHASES:
            with self.lock:
                row = self.rows.get(handle.key)
                if row and row["span_id"] == handle.span_id:
                    previous = (row["state"], row["phase"], row["phase_at"], row.get("model_task"))
                    row.update(state=phase if phase in {"waiting_provider", "waiting_capacity"} else "running",
                               phase=phase, phase_at=time.time(), progress_at=time.time())
                    row["model_task"] = task if task in {"general", "fast", "reasoning", "long_context", "vision", "code", "coding"} else ""
                    self._trace(row, "phase.started", model_task=row["model_task"])
        try:
            yield
        finally:
            if previous:
                with self.lock:
                    row = self.rows.get(handle.key)
                    if row and row["span_id"] == handle.span_id:
                        # A returned call is an observed checkpoint, not proof
                        # of success. The boundary reports the actual outcome.
                        row.update(state=previous[0], phase=previous[1], phase_at=time.time(),
                                   model_task=previous[3], progress_at=time.time())
                        self._trace(row, "phase.returned", model_task=previous[3])

    def snapshot(self, running=True):
        from applypilot.runtime_info import process_info
        with self.lock:
            rows = []
            for key, row in self.rows.items():
                ref, is_task = self.references[key]
                owner = ref()
                alive = owner is not None and (not owner.done() if is_task else owner.is_alive())
                row["execution_alive"] = bool(alive and running)
                if not alive or not running:
                    row["state"] = "stopped"
                    row["active"] = False
            self._trim_inactive()
            rows = [dict(row) for row in self.rows.values()]
            return {"format_version": 1, "process_id": self.process_id, "run_id": self.run_id,
                    "pid": self.pid, "at": time.time(), "running": running,
                    "process": process_info(self.role),
                    "dropped_workers": self.dropped, "rows": rows}

    def publish(self, root=None, running=True):
        from .store import root_path
        from applypilot.web.runtime import atomic_json
        folder = root_path(root) / "worker-snapshots"
        # Called by the journal writer, never by a job/model/browser thread.
        atomic_json(folder / (self.process_id + ".json"), self.snapshot(running))
        # Retain stopped reports briefly for inspection, not indefinitely.
        cutoff = time.time() - 86400
        for path in folder.glob("*.json"):
            if not path.is_symlink() and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

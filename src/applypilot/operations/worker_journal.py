"""Local, bounded worker decision history, independent of both PostgreSQL databases.

Only code-owned metadata is accepted. No prompts, browser payloads, document
contents or submission ownership tokens. A lost log never changes execution.
"""
from __future__ import annotations
import atexit
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid

DIRECTORY = "logs/worker-activity"
_journal = None
_lock = threading.Lock()
_last_warning = 0.0
_FIELDS = {"worker_id", "worker_key", "run_id", "process_id", "trace_id", "stage", "operation", "phase", "state",
    "span_id", "parent_span_id", "cycle_id", "job_id", "job_title", "company", "source", "outcome",
    "reason", "elapsed_seconds", "task_elapsed_seconds", "call_id", "call_state", "model_task",
    "requested_runtime", "selected_runtime", "dry_run", "selection_id", "policy_id", "polls",
    "empty_polls", "last_checked_at", "next_poll_seconds", "diagnostic_id", "snapshot",
    "code", "exception_type", "origin", "severity", "completed_operations", "failed_operations", "partial_operations",
    "warning_events", "reported_applied", "reported_failed", "action_count", "result_code",
    "http_status", "page_text_chars", "description_chars", "has_application_url", "valid_json",
    "finish_reason", "agent_pid", "tool_name", "tool_call_id", "reported_error", "available", "truncated", "selection_trace", "pending", "upstream", "upstream_done", "document_completed_after_warning",
    "provenance", "skill_name", "skill_source", "skill_revision", "skill_sha256"}
_PRIVATE = re.compile(r"(?:password|token|secret|cookie|credential|authorization|prompt|messages|resume|full_description|content|html|target_url|^url$)", re.I)


def enabled():
    return os.environ.get("APPLYPILOT_WORKER_ACTIVITY_LOG", "on").lower().strip() not in {"off", "0", "false", "no"}


def job_id(url):
    return hashlib.sha256(str(url or "").encode()).hexdigest()[:24] if url else ""


def _safe(value, depth=0):
    from .runtime import sanitize
    if depth > 8:
        return "[nesting limit]"
    if isinstance(value, dict):
        return {str(k)[:80]: _safe(v, depth+1) for k, v in list(value.items())[:100]
                if not _PRIVATE.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_safe(v, depth+1) for v in value[:100]]
    if type(value) is float:
        return value if math.isfinite(value) else None
    if value is None or type(value) in {bool, int}:
        return value
    return sanitize(str(value), 400)


def _warn():
    global _last_warning
    now = time.monotonic()
    if now - _last_warning >= 60:
        _last_warning = now
        logging.getLogger(__name__).warning("Worker activity diagnostics lost a record; application execution is unchanged")


class WorkerJournal:
    """One append-only stream per process. No shared rotating-file rename race."""
    def __init__(self, root, *, segment_bytes=4 * 1024**2, total_bytes=64 * 1024**2, retention_days=3, queue_size=4096):
        self.root = Path(root)
        self.folder = self.root / DIRECTORY
        self.pid = os.getpid()
        self.name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-p{self.pid}-" + uuid.uuid4().hex[:12]
        self.segment_bytes, self.total_bytes = segment_bytes, total_bytes
        self.retention_seconds = retention_days * 86400
        self.segment = 0
        self.queue = queue.Queue(maxsize=queue_size)
        self.stopping = threading.Event()
        self.dropped = self.write_errors = 0
        self.last_prune = 0.0
        self.thread = threading.Thread(target=self._run, name="worker-activity-journal", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def put(self, event):
        try:
            if self.stopping.is_set():
                raise queue.Full
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1
            _warn()

    def _write(self, event):
        # Reject redirected log directories. This is not a Windows ACL manager;
        # Windows files inherit the owner's workspace ACL.
        for parent in (self.root, self.root / "logs", self.folder):
            if parent.is_symlink():
                raise OSError("symlink in diagnostics path")
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.last_prune or time.monotonic() - self.last_prune > 60:
            self.prune()
            if shutil.disk_usage(self.folder).free < 64 * 1024**2:
                raise OSError("diagnostics free-space reserve")
            self.last_prune = time.monotonic()
        event = {**event, "journal_dropped": self.dropped, "journal_write_errors": self.write_errors}
        line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        path = self.folder / f"worker-{self.name}-{self.segment:04d}.jsonl"
        rotated = path.exists() and path.stat().st_size + len(line) > self.segment_bytes
        if rotated:
            self.segment += 1
            path = self.folder / f"worker-{self.name}-{self.segment:04d}.jsonl"
        if path.is_symlink():
            raise OSError("symlink log file")
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "ab") as stream:
            stream.write(line)
        if rotated or path.stat().st_size >= self.segment_bytes:
            self.prune(keep=path)

    def prune(self, keep=None):
        entries = []
        for path in self.folder.glob("worker-*.jsonl"):
            if path.is_symlink():
                continue
            try:
                stat = path.stat()
                entries.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                pass
        total = sum(size for _, size, _ in entries)
        cutoff = time.time() - self.retention_seconds
        for stamp, size, path in sorted(entries):
            if path == keep or (stamp >= cutoff and total <= self.total_bytes):
                continue
            try:
                path.unlink(missing_ok=True)
                total -= size
            except OSError:
                pass  # Another process may have the file open on Windows.

    def _run(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                event = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write(event)
            except Exception:
                self.write_errors += 1
                _warn()
            finally:
                self.queue.task_done()

    def flush(self, timeout=3):
        end = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.005)
        return self.queue.unfinished_tasks == 0

    def close(self):
        self.stopping.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)



def emit(event, **fields):
    """Best-effort asynchronous append; unknown payload fields are discarded."""
    global _journal
    if not enabled():
        return
    try:
        from applypilot import config
        value = {k: _safe(v) for k, v in fields.items() if k in _FIELDS}
        if fields.get("reason"):
            from .runtime import sanitize
            value["reason"] = sanitize(fields["reason"], 1800)
        value.update(format="applypilot_worker_activity", format_version=1, event=str(event)[:80],
                     at=datetime.now(timezone.utc).isoformat(), pid=os.getpid())
        # Resolve identity without creating the PostgreSQL recorder from its writer.
        from . import runtime
        rec = runtime._recorder
        if rec is not None and rec.pid == os.getpid():
            value.setdefault("run_id", rec.run_id)
            value.setdefault("process_id", rec.process_id)
        else:
            value.setdefault("run_id", os.environ.get("APPLYPILOT_OPS_RUN_ID", ""))
        with _lock:
            root = Path(config.APP_DIR)
            if _journal is None or _journal.pid != os.getpid() or _journal.root != root:
                _journal = WorkerJournal(root)
            journal = _journal
        journal.put(value)
    except Exception:
        _warn()


def current(event, **fields):
    """Attach the current job/worker scope to a metadata-only checkpoint."""
    try:
        from .runtime import _context
        ctx = _context.get()
        handle = ctx.get("_worker_handle")
        emit(event, **{**{k: v for k, v in ctx.items() if k in _FIELDS},
                       **({"worker_key": handle.key} if handle else {}), **fields})
    except Exception:
        _warn()


_checkpoints = {}
_checkpoint_lock = threading.Lock()


def checkpoint(event, *, interval=60, **fields):
    """Repeat unchanged polls at most once/minute; changed results immediately."""
    try:
        signature = json.dumps({k: v for k,v in fields.items() if k not in
            {"elapsed_seconds", "polls", "empty_polls", "last_checked_at", "selection_id"}},sort_keys=True,default=str)
        key=(os.environ.get("APPLYPILOT_OPS_RUN_ID", ""), event, fields.get("worker_id"),fields.get("stage"))
        now=time.monotonic()
        with _checkpoint_lock:
            old=_checkpoints.get(key)
            if old and old[0]==signature and now-old[1]<interval:return
            _checkpoints[key]=(signature,now)
            if len(_checkpoints)>1024:_checkpoints.pop(next(iter(_checkpoints)))
        emit(event,**fields)
    except Exception:
        _warn()


def agent_tool(event, *, worker_id, job_url, tool_name, tool_call_id, reported_error=None):
    """Tool metadata only: never accept arguments, JS, DOM, or result bodies."""
    name = str(tool_name or "")
    name = name if re.fullmatch(r"[A-Za-z0-9_:. -]{1,180}", name) else "unrecognized_tool_name"
    ident = str(tool_call_id or "")
    ident = ident if re.fullmatch(r"[A-Za-z0-9_-]{1,180}", ident) else ""
    emit(event, worker_id=worker_id, stage="applications", job_id=job_id(job_url),
         tool_name=name, tool_call_id=ident, reported_error=reported_error,
         provenance="agent", origin="agent")

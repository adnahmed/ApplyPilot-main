"""Non-blocking, per-process recorder with cross-process run correlation.

No provider calls, DB writes, thread joins or network traffic on the hot emit path.
The bounded queue and separate WAL writer deliberately fail open. Only telemetry
can be lost; exceptions, retries, eligibility and application results are unchanged.
"""
from __future__ import annotations

import atexit
import asyncio
from concurrent.futures import CancelledError
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import hashlib
import errno
import inspect
import json
import logging
import math
import os
from pathlib import Path
import queue
import re
import socket
import subprocess
from urllib.error import URLError

import httpx
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError
from pydantic import ValidationError as PydanticValidationError
from applypilot import postgres
import threading
import time
import uuid

from . import store

_context = ContextVar("applypilot_operations", default={})
_lock = threading.Lock()
_recorder = None
_COMPONENTS = set("autopilot learning resources engine admission pipeline discovery enrich score tailor cover pdf brief company_research company_discovery career_ops "
    "research search crawl mcp sdk gmail tracking referrals outreach unattended backup database browser "
    "applications feedback setup integrations operations unknown jobspy greenhouse lever ashby workday "
    "smartextract browseros browseros_linkedin twitter linkedin_posts web_search freehire remoteok "
    "remotive hackernews amazon seek reed costco irishjobs ziprecruiter linkedin twikit ddgs "
    "searxng crawl4ai linkedin_scraper linkedin_rs hiring_cafe".split())
_STAGES = set("autopilot learning resources engine admission pipeline discovery enrich score tailor cover pdf brief company_research company_discovery career_ops "
              "tracking referrals outreach unattended backup setup applications operations integrations unknown".split())
_OUTCOMES = {"success", "empty", "skipped", "deferred", "partial", "failed", "cancelled", "started"}
_ERROR_KINDS = {"none", "unknown", "auth", "configuration", "timeout", "network", "rate_limit", "schema",
                "storage", "security", "send_unknown", "dependency", "provider", "cancelled", "policy", "log", "unavailable"}


def component(value):
    value = str(value or "unknown").replace("-", "_").lower().split(":")[0]
    return value if value in _COMPONENTS else "integrations"


def label(value):
    return re.sub(r"[^a-zA-Z0-9_:.-]", "_", str(value or "unknown"))[:80]


def sanitize(value, limit=1800, *, secrets=None):
    """Defense in depth, not an authorization boundary. Never pass payloads here."""
    text = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else str(value or "")
    secret_values = {secret for source in (os.environ, secrets or {}) for key, secret in source.items()
                     if isinstance(secret, str) and len(secret) >= 4
                     and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|MASTER_KEY|COOKIE|CREDENTIAL", key, re.I)}
    for secret in sorted(secret_values, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    text = text[:65536]
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"(?is)-----BEGIN .*?PRIVATE KEY-----.*?(?:-----END .*?PRIVATE KEY-----|$)", "[redacted key]", text)
    text = re.sub(r"(?im)^(\s*(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*).*$", r"\1[redacted]", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+\S+", "[redacted authorization]", text)
    text = re.sub(r'''(?i)(["']?(?:\w*token|api[_-]?key|\w*secret|\w*password|authorization|cookie|set-cookie|li_at|ct0|session_key)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r"\1[redacted]", text)
    text = re.sub(r'''(?i)(--[\w-]*(?:token|secret|password|api-key|cookie)\s+)(?:"[^"]*"|'[^']*'|\S+)''', r"\1[redacted]", text)
    text = re.sub(r"\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{10,}|github_pat_[\w_]{10,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", "[redacted token]", text)
    text = re.sub(r'(?is)(?:"(?:messages|prompt|resume|full_description|content)"\s*:)\s*.*', "[request/document payload omitted]", text)
    text = re.sub(r"(?:https?|postgres(?:ql)?|redis|amqps?|mongodb(?:\+srv)?|ftp)://[^\s<>\"']+", "[url omitted]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email omitted]", text)
    text = re.sub(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|mnt|tmp|var)/)[^\s\"']+", "[local path omitted]", text)
    return text[:limit]


def diagnostic_details(value, *, secrets=None):
    """Keep bounded failure metadata, never arbitrary payloads, argv or environments."""
    if not isinstance(value, dict):
        return {}
    strings = {"error": 1800, "message": 1800, "code": 100, "error_code": 100,
               "error_kind": 50, "component": 100, "phase": 100, "command": 200,
               "subcommand": 100, "stderr": 6000, "hint": 1600, "reason": 600,
               "auth_source": 100, "exception_type": 100, "query_id": 64, "sqlstate": 5,
               "constraint": 160, "table": 160, "column": 160, "schema": 160,
               "request_id": 160, "provider_request_id": 160, "route": 200,
               "channel": 50, "operation": 100, "reason_code": 100,
               "error_type": 100, "next_retry_at": 100}
    numbers = {"return_code", "http_status", "status_code", "errno", "winerror", "stderr_bytes",
               "elapsed_seconds", "duration_seconds", "timeout_seconds", "statement_position", "retry_after"}
    flags = {"stderr_truncated", "stderr_captured", "stdout_omitted", "timed_out", "retryable", "details_redacted"}
    result = {}
    for key, limit in strings.items():
        if isinstance(value.get(key), (str, bytes)):
            result[key] = sanitize(value[key], limit, secrets=secrets)
    for key in numbers:
        number = value.get(key)
        if type(number) in (int, float) and math.isfinite(number):
            result[key] = number
    for key in flags:
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if isinstance(value.get('stderr'), (str, bytes)) and len(value['stderr']) > 6000:
        result['stderr_truncated'] = True
    if isinstance(value.get('failures'), (list, tuple)):
        allowed = {'channel', 'operation', 'reason', 'reason_code', 'error_type',
                   'retryable', 'retry_after', 'next_retry_at', 'http_status'}
        result['failures'] = [diagnostic_details({key: item[key] for key in allowed if key in item}, secrets=secrets)
                              for item in value['failures'][:12] if isinstance(item, dict)]
    return result


def stderr_details(source, *, secrets=None):
    """Read a bounded failure tail from a captured stream or subprocess exception."""
    if isinstance(source, (str, bytes)):
        data = source.encode('utf-8', errors='replace') if isinstance(source, str) else source
        size = len(data)
        data = data[-32768:]
    else:
        source.seek(0, os.SEEK_END)
        size = source.tell()
        source.seek(max(0, size - 32768))
        data = source.read(32768)
    truncated = size > 32768
    if truncated:
        data = data.partition(b'\n')[2]  # Do not retain a cut credential suffix.
    text = sanitize(data, 65536, secrets=secrets)
    tail = text[-6000:]
    return {'stderr': tail, 'stderr_bytes': size, 'stderr_captured': bool(size),
            'stderr_truncated': truncated or len(text) > 6000}


def error_kind(exc=None, hint=""):
    """Use explicit kinds, typed exceptions and protocol fields; never prose.

    ``hint`` is retained for callers supplying a canonical machine category.
    Messages, exception class names and HTTP-looking text are not evidence.
    """
    def canonical(value):
        value = getattr(value, "value", value)
        if value == "access_denied":
            return "policy"
        return value if isinstance(value, str) and value in _ERROR_KINDS else None

    explicit = canonical(hint)
    if explicit is not None:
        return explicit
    seen = set()
    while isinstance(exc, BaseException) and id(exc) not in seen:
        seen.add(id(exc))
        for value in (getattr(exc, "error_kind", None), getattr(exc, "kind", None),
                      getattr(getattr(exc, "failure", None), "kind", None)):
            explicit = canonical(value)
            if explicit is not None:
                return explicit
        if isinstance(exc, postgres.Error):
            return "storage"
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError, CancelledError)):
            return "cancelled"
        if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired, httpx.TimeoutException)):
            return "timeout"
        if isinstance(exc, (ImportError, FileNotFoundError)):
            return "dependency"
        if isinstance(exc, (json.JSONDecodeError, JSONSchemaValidationError, PydanticValidationError)):
            return "schema"
        status = next((value for value in (
            getattr(exc, "status_code", None),
            getattr(getattr(exc, "response", None), "status_code", None),
            getattr(exc, "http_status", None), getattr(exc, "status", None),
            getattr(exc, "code", None),
        ) if type(value) is int and 400 <= value <= 599), None)
        if status in {401, 403}:
            return "auth"
        if status == 429:
            return "rate_limit"
        if status in {408, 504}:
            return "timeout"
        if status is not None and status >= 500:
            return "provider"
        if isinstance(exc, (ConnectionError, socket.gaierror, socket.herror, httpx.NetworkError,
                            httpx.RemoteProtocolError, httpx.ProxyError)):
            return "network"
        if isinstance(exc, OSError):
            if exc.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS}:
                return "storage"
            if exc.errno in {errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH}:
                return "network"
        cause = exc.__cause__
        if cause is None and isinstance(exc, URLError) and isinstance(exc.reason, BaseException):
            cause = exc.reason
        exc = cause
    return "unknown"


def exception_details(exc, *, secrets=None):
    chain, seen, pending = [], set(), [(exc, "root")]
    while pending and len(chain) < 12:
        current, relation = pending.pop(0)
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        frames, tb = [], current.__traceback__
        while tb is not None:
            path = tb.tb_frame.f_code.co_filename.replace("\\", "/")
            safe_path = "applypilot/" + path.split("/applypilot/", 1)[1] if "/applypilot/" in path else Path(path).name
            frames.append({"file": safe_path, "line": tb.tb_lineno, "function": tb.tb_frame.f_code.co_name})
            tb = tb.tb_next
        process_error = isinstance(current, (subprocess.CalledProcessError, subprocess.TimeoutExpired))
        message = current
        if isinstance(current, subprocess.TimeoutExpired):
            message = "Subprocess exceeded its time budget"
        elif process_error:
            message = "Subprocess failed with exit code " + str(current.returncode)
        elif isinstance(current, postgres.Error):
            message = getattr(getattr(current, 'diag', None), 'message_primary', None) or "PostgreSQL operation failed"
        safe_message = sanitize(message, 1801, secrets=secrets)
        item = {"type": type(current).__name__, "message": safe_message[:1800], "frames": frames[-16:], "relation": relation}
        if len(safe_message) > 1800:
            item['message_truncated'] = True
        if len(frames) > 16:
            item['frames_truncated'] = True
        diagnostics = diagnostic_details(getattr(current, 'details', None), secrets=secrets)
        return_code = getattr(current, 'returncode', None)
        if type(return_code) is int:
            diagnostics['return_code'] = return_code
        if process_error:
            command = getattr(current, 'cmd', None)
            if isinstance(command, (list, tuple)) and command and isinstance(command[0], (str, os.PathLike)):
                diagnostics['command'] = sanitize(str(command[0]).replace('\\', '/').rsplit('/', 1)[-1], 120, secrets=secrets)
            stderr = getattr(current, 'stderr', None)
            diagnostics.setdefault('stderr_captured', stderr is not None)
            if isinstance(stderr, (str, bytes)) and 'stderr' not in diagnostics:
                diagnostics.update(stderr_details(stderr, secrets=secrets))
            diagnostics['stdout_omitted'] = getattr(current, 'stdout', None) is not None
            if isinstance(current, subprocess.TimeoutExpired):
                diagnostics.update(diagnostic_details({'timeout_seconds': current.timeout}, secrets=secrets))
                diagnostics['timed_out'] = True
        response = getattr(current, 'response', None)
        status = getattr(response, 'status_code', None) or getattr(current, 'status_code', None) or getattr(current, 'http_status', None)
        if type(status) is int and 100 <= status <= 599:
            diagnostics['http_status'] = status
        diag = getattr(current, 'diag', None)
        if diag is not None:
            diagnostics.update(diagnostic_details({
                'hint': getattr(diag, 'message_hint', None), 'constraint': getattr(diag, 'constraint_name', None),
                'table': getattr(diag, 'table_name', None), 'column': getattr(diag, 'column_name', None),
                'schema': getattr(diag, 'schema_name', None),
            }, secrets=secrets))
            position = getattr(diag, 'statement_position', None)
            if isinstance(position, str) and position.isdigit():
                diagnostics['statement_position'] = int(position)
        if diagnostics:
            item['diagnostics'] = diagnostics
        for key in ("errno", "winerror"):
            value = getattr(current, key, None)
            if type(value) is int:
                item[key] = value
        sqlstate = getattr(current,"sqlstate",None)
        if isinstance(sqlstate,str) and re.fullmatch(r'[A-Z0-9]{5}',sqlstate):
            item['sqlstate']=sqlstate
        phase = getattr(current, "transaction_phase", None)
        if phase in {"begin", "body", "commit"}:
            item["transaction_phase"] = phase
        waited = getattr(current, "wait_seconds", None)
        if type(waited) in {int, float} and math.isfinite(waited) and waited >= 0:
            item["wait_seconds"] = round(waited, 3)
        chain.append(item)
        if isinstance(current, BaseExceptionGroup):
            pending[0:0] = [(child, f"{relation}.exceptions[{index}]") for index, child in enumerate(current.exceptions[:12])]
        linked = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
        if linked is not None:
            pending.append((linked, relation + (".cause" if current.__cause__ is not None else ".context")))
    result = {"exception_chain": chain}
    if pending:
        result["exception_chain_truncated"] = True
    return result


class Recorder:
    def __init__(self, role="worker"):
        self.pid = os.getpid()
        inherited = os.environ.get("APPLYPILOT_OPS_RUN_ID", "")
        self.run_id = inherited if re.fullmatch(r"[a-f0-9]{32}", inherited) else uuid.uuid4().hex
        # Internal propagation, never an operator requirement or credential.
        os.environ["APPLYPILOT_OPS_RUN_ID"] = self.run_id
        self.process_id = uuid.uuid4().hex
        self.role = label(role)
        from .worker_status import WorkerRegistry
        self.workers = WorkerRegistry(self.process_id, self.run_id, self.pid, self.role)
        self.queue = queue.Queue(maxsize=20000)
        self.stop = threading.Event()
        self.dropped = 0
        self.write_errors = 0
        self.root = None  # Resolve config after ordinary bootstrapping has loaded it.
        self.thread = threading.Thread(target=self._writer, name="operations-journal", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def put(self, event):
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            self.dropped += 1

    def _writer_failure(self, phase, error, *, discarded=0, retry=0):
        self.write_errors += 1
        self.dropped += discarded
        # Independent of the journal and the logging handler that feeds it.
        try:
            from applypilot.atomic_io import atomic_record
            atomic_record(store.root_path(self.root) / ("writer-" + self.process_id + ".json"), {
                "at": time.time(), "process_id": self.process_id, "run_id": self.run_id,
                "phase": phase, "error_type": type(error).__name__,
                "transaction_phase": getattr(error, "transaction_phase", None),
                "message": sanitize(error, 300), "retry": retry,
                "discarded_events": discarded, "dropped": self.dropped,
                "write_errors": self.write_errors, "queue_depth": self.queue.qsize(),
            })
        except Exception:
            pass

    def _writer(self):
        conn = None
        last_heartbeat = last_prune = last_worker_snapshot = 0.0
        batch, batch_id, retries, batch_started = [], None, 0, 0.0
        while not self.stop.is_set() or batch or not self.queue.empty():
            # File snapshots describe live execution even while the operations
            # database is busy. Append retries must not skip their heartbeat.
            now = time.time()
            if now - last_worker_snapshot >= 5:
                try:
                    self.workers.publish(self.root)
                except Exception:
                    pass
                last_worker_snapshot = now
            if not batch:
                try:
                    batch.append(self.queue.get(timeout=0.25))
                except queue.Empty:
                    pass
                while len(batch) < 200:
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    batch_id, retries, batch_started = uuid.uuid4().hex, 0, time.monotonic()
            phase = "connect"
            try:
                if conn is None:
                    conn = store.connect_writer(self.root)
                if batch:
                    phase = "append"
                    store.append(conn, batch, batch_id=batch_id)
            except Exception as error:
                retries += 1
                busy = postgres.is_retryable(error)
                retry = bool(batch and busy and retries < 5 and time.monotonic() - batch_started < 30)
                committed = False
                if batch and not retry:
                    # A ledger entry resolves even an ambiguous commit response.
                    try:
                        with store.reader(self.root) as check:
                            committed = check.execute("SELECT 1 FROM journal_batches WHERE id=?", (batch_id,)).fetchone() is not None
                    except Exception as lookup_error:
                        self._writer_failure("commit_lookup", lookup_error)
                discarded = len(batch) if batch and not retry and not committed else 0
                self._writer_failure(phase, error, discarded=discarded, retry=retries)
                if not retry:
                    for _ in batch:
                        self.queue.task_done()
                    batch = []
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                    conn = None
                time.sleep(min(2, .25 * max(1, retries)))
                continue
            # A committed append is acknowledged before unrelated maintenance.
            for _ in batch:
                self.queue.task_done()
            batch = []
            now = time.time()
            if now - last_heartbeat >= 5:
                try:
                    with conn:
                        conn.execute('INSERT INTO processes VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT (id) DO UPDATE SET run_id=excluded.run_id, pid=excluded.pid, role=excluded.role, heartbeat=excluded.heartbeat, dropped=excluded.dropped, write_errors=excluded.write_errors, queue_depth=excluded.queue_depth, running=excluded.running', (
                            self.process_id, self.run_id, self.pid, self.role, now,
                            self.dropped, self.write_errors, self.queue.qsize()))
                    last_heartbeat = now
                except Exception as error:
                    self._writer_failure("heartbeat", error)
                    last_heartbeat = now
            if now - last_prune >= 60:
                try:
                    store.prune(conn, self.root)
                    last_prune = now
                except Exception as error:
                    self._writer_failure("prune", error)
                    last_prune = now
        if conn is not None:
            try:
                with conn:
                    conn.execute("UPDATE processes SET running=0,heartbeat=?,dropped=?,write_errors=? WHERE id=?", (
                        time.time(), self.dropped, self.write_errors, self.process_id))
                    conn.execute("DELETE FROM active WHERE process_id=?", (self.process_id,))
            except Exception as error:
                self._writer_failure("shutdown", error)
            finally:
                conn.close()
            try:
                self.workers.publish(self.root, running=False)
            except Exception:
                pass

    def close(self):
        self.stop.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)


def diagnostics_health():
    recorder = _recorder
    if recorder is None or recorder.pid != os.getpid():
        return {}
    return {"dropped_events": recorder.dropped, "write_errors": recorder.write_errors,
            "pending_events": recorder.queue.qsize(), "writer_alive": recorder.thread.is_alive()}


def initialize(role="worker"):
    global _recorder
    if _recorder is None or _recorder.pid != os.getpid():
        with _lock:
            if _recorder is None or _recorder.pid != os.getpid():
                _recorder = Recorder(role)
                handler = OperationsHandler()
                logging.getLogger("applypilot").addHandler(handler)
    return _recorder


def record(*, component_name="unknown", operation="event", stage=None, kind="signal", outcome="failed",
           severity="error", message="", error=None, error_type=None, duration=0.0, details=None, **context):
    """Best effort; pass only safe identifiers/counts, never complete result objects."""
    try:
        recorder = initialize()
        ctx = {**_context.get(), **context}
        admission = getattr(error, "_applypilot_admission", None) if error is not None else None
        if isinstance(admission, dict):
            details = {**{key: admission[key] for key in ("reason_code", "reason", "origin", "retryable", "retry_trigger",
                        "scope_id", "next_eligible_at", "evidence_id", "logical_task_id") if key in admission}, **(details or {})}
        comp = component(component_name)
        stage = stage or ctx.get("stage") or comp
        stage = stage if stage in _STAGES else "integrations"
        ek = error_kind(hint=error_type) if error_type is not None else (
            error_kind(error) if severity in {"warning", "error", "critical"} else "none")
        if severity in {"warning", "error", "critical"}:
            try:
                from .worker_status import issue_evidence
                evidence = issue_evidence(error, message, ek, comp, label(operation), details, ctx)
                recorder.workers.issue(ctx.get("_worker_handle"), evidence["code"], evidence=evidence, severity=severity)
            except Exception:
                pass
        safe = exception_details(error) if error is not None else {}
        diagnostics = diagnostic_details((details or {}).get('diagnostics'))
        if diagnostics:
            safe['diagnostics'] = diagnostics
        if ctx.get("integration"):
            safe["integration"] = sanitize(ctx["integration"], 100)
        for key in ("job_title", "job_company", "job_source"):
            if ctx.get(key):
                safe[key] = sanitize(ctx[key], 200)
        diagnostics = (details or {}).get("validation_errors")
        if isinstance(diagnostics, (list, tuple)):
            safe["validation_errors"] = [sanitize(item, 700) for item in diagnostics[:20] if isinstance(item, str)]
        for key in ("provider_request_id", "reason_code", "scope_id", "next_eligible_at", "evidence_id", "logical_task_id", "case_id", "attempt_id", "error_origin", "validation_report", "validation_mode", "factual_review", "retryable", "retry_trigger"):
            if (details or {}).get(key) is not None:
                safe[key] = details[key] if type(details[key]) in {bool, int, float} else sanitize(details[key], 200)
        # details is strictly a counts/coverage channel, never an arbitrary payload.
        for key, value in (details or {}).items():
            if key in {"processed", "ok", "partial", "error", "planned", "drafted", "held", "expired", "description_chars",
                       "research_enqueued", "with_candidates", "profiles_checked", "promoted", "queries",
                       "facts", "leads_found", "profile_failures", "eligible", "attempted",
                       "blocked", "retired", "refreshed", "new_cases", "verified_relevant_contacts"} and type(value) in {int, float}:
                if math.isfinite(value) and value >= 0:
                    safe[key] = value
            if key == "has_application_url" and type(value) is bool:
                safe[key] = value
            if key in {"found", "new", "existing", "pages", "errors", "selected", "reports", "scored", "sent", "return_code", "coverage", "reason", "pending", "origin", "integration", "worker_id", "elapsed_seconds", "idle_seconds", "route", "sqlstate", "backlog", "backlog_due", "backlog_deferred", "backlog_next_retry_at", "sync_complete", "llm_call_id", "model", "response_log", "http_status", "response_received"}:
                safe[key] = value if type(value) in {bool, int, float} else sanitize(value, 300)
        failure_message = (safe.get('exception_chain') or [{}])[0].get('message')
        message = sanitize(message or failure_message or outcome)
        frame = (safe.get("exception_chain") or [{}])[0].get("frames", [])
        location = frame[-1] if frame else {}
        fp = hashlib.sha256(json.dumps([stage, comp, label(operation), ek, location, sanitize(message, 250)
            if not location else ""], sort_keys=True).encode()).hexdigest()[:32]
        recorder.put({"at": time.time(), "run_id": ctx.get("run_id", recorder.run_id),
            "process_id": recorder.process_id, "role": recorder.role,
            **{k: str(ctx.get(k, ""))[:160] for k in ("span_id", "parent_span_id", "cycle_id", "task_id", "job_id")},
            "stage": stage, "component": comp, "operation": label(operation), "kind": kind,
            "severity": severity if severity in {"info", "warning", "error", "critical"} else "error",
            "outcome": outcome if outcome in _OUTCOMES else "failed", "error_kind": ek,
            "message": message, "details": json.dumps(safe, ensure_ascii=True), "duration": max(0.0, float(duration)),
            "fingerprint": fp})
    except Exception:
        # Observability must not change the application's exception/result path.
        pass


def capture(exc, *, component_name="unknown", operation="exception", stage=None, severity="error", **context):
    deferred = getattr(exc, "operation_outcome", None) == "deferred" or getattr(exc, "kind", None) == "budget"
    record(error=exc, component_name=component_name, operation=operation, stage=stage,
           severity="info" if deferred else severity, outcome="deferred" if deferred else "failed", **context)


def correlation():
    """Safe identifiers for carrying a work attempt across a persistence boundary."""
    result = {key: str(value)[:160] for key, value in _context.get().items()
              if key in {"run_id", "span_id", "parent_span_id", "cycle_id", "task_id", "job_id"}}
    if _recorder is not None:
        result.setdefault("run_id", _recorder.run_id)
    return result


class OperationsHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)

    def emit(self, entry):
        if not entry.name.startswith("applypilot") or entry.name.startswith(("applypilot.operations", "applypilot.llm_diagnostics")):
            return
        # Journal writes can log their own database latency/failures. Keep those
        # in ordinary logs and _writer_failure's independent record, not the queue
        # whose slow append produced them.
        if _recorder is not None and entry.thread == _recorder.thread.ident:
            return
        try:
            exc = entry.exc_info[1] if entry.exc_info else getattr(entry, 'ops_exception', None)
            if not isinstance(exc, BaseException):
                exc = None
            if exc is None and isinstance(entry.args, tuple):
                exc = next((arg for arg in reversed(entry.args) if isinstance(arg, BaseException)), None)
            # Logging arguments may contain applicant profiles/emails/prompts. Do not persist them.
            message = str(entry.msg)
            if entry.args:
                message = re.split(r"%(?:\([^)]+\))?[-+0-9.#]*[sdrf]", message, maxsplit=1)[0].rstrip(': ,;=-')
                message = re.sub(r"\s+(?:for|from|to|at|in|after|with)$", "", message).strip()
                message = message or 'Worker operation reported a problem'
            parts = entry.name.split(".")
            comp = parts[1] if len(parts) > 1 else "unknown"
            comp = {"enrichment": "enrich", "apply": "applications"}.get(comp, comp)
            if comp == "scoring":
                comp = {"scorer": "score", "cover_letter": "cover"}.get(parts[-1], parts[-1])
            severity = "critical" if entry.levelno >= logging.CRITICAL else "error" if entry.levelno >= logging.ERROR else "warning"
            details = {"origin": entry.name + ":" + str(entry.lineno)}
            diagnostics = diagnostic_details(getattr(entry, 'ops_diagnostics', None))
            if diagnostics:
                details['diagnostics'] = diagnostics
            if exc is not None and entry.args:
                message += ': ' + exception_details(exc)['exception_chain'][0]['message']
            # An explicit numeric identifier is safe. Keep arbitrary logging
            # arguments redacted; do not format emails, profiles or prompts.
            worker_id = getattr(entry, "ops_worker_id", None)
            if type(worker_id) is int and 0 <= worker_id <= 1_000_000:
                details["worker_id"] = worker_id
            reason_code = getattr(entry, "ops_reason_code", None)
            if isinstance(reason_code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason_code):
                details["reason_code"] = reason_code
            for key in ("elapsed_seconds", "idle_seconds"):
                value = getattr(entry, "ops_" + key, None)
                if type(value) in {int, float} and 0 <= value <= 31_536_000 and math.isfinite(value):
                    details[key] = round(value, 3)
            if "reason_code" in details:
                message = details["reason_code"].replace("_", " ")
                if "worker_id" in details:
                    message = f"Worker {details['worker_id']}: {message}"
                if "elapsed_seconds" in details:
                    message += f"; elapsed {details['elapsed_seconds']:.0f}s"
                if "idle_seconds" in details:
                    message += f"; no output for {details['idle_seconds']:.0f}s"
            attempt_id = getattr(entry, "ops_attempt_id", None)
            context = {}
            if isinstance(attempt_id, str) and re.fullmatch(r"[a-f0-9]{32}", attempt_id):
                details["attempt_id"] = attempt_id
                context["task_id"] = attempt_id
            if getattr(entry, "ops_database_failure", False) is True:
                # Route path only, never a query string or exception/SQL body.
                route = getattr(entry, "ops_route", "")
                if re.fullmatch(r"/api/[a-z][a-z0-9_/-]{0,90}", route):
                    details["route"] = route
                sqlstate=getattr(entry,'ops_sqlstate','')
                if re.fullmatch(r'[A-Z0-9]{5}',str(sqlstate)):
                    details['sqlstate']=sqlstate
                record(component_name="database", operation="dashboard_read", stage="operations", kind="log",
                       severity=severity, message="Dashboard database read failed", error=exc, error_type="storage", details=details)
                return
            outcome = getattr(entry, "ops_outcome", None)
            if outcome not in _OUTCOMES:
                outcome = "partial" if severity == "warning" else "failed"
            record(component_name=comp, operation="log_" + label(entry.funcName), kind="log", severity=severity, outcome=outcome,
                   message=message, error=exc, error_type=getattr(entry, "ops_error_kind", None if exc else "log"),
                   details=details, **context)
        except Exception:
            pass


@contextmanager
def scope(**values):
    token = _context.set({**_context.get(), **{k: v for k, v in values.items() if v is not None}})
    try:
        yield
    finally:
        _context.reset(token)


def thread_target(function):
    context = copy_context()
    return lambda *args, **kwargs: context.run(function, *args, **kwargs)


def submit(executor, function, /, *args, **kwargs):
    """Copy context separately for each future (a Context cannot run concurrently)."""
    return executor.submit(copy_context().run, function, *args, **kwargs)


def result_state(value):
    """Classify returned failures, not just raised exceptions. Never mutate results."""
    failures = getattr(value, "failures", ())
    if failures:
        first = failures[0]
        kind = getattr(getattr(first, "kind", None), "value", "unknown")
        return "partial" if getattr(value, "records", ()) else "failed", str(getattr(first, "message", "Discovery page failure")), error_kind(hint=kind)
    if hasattr(value, "records") and hasattr(value, "complete"):
        if getattr(value, "partial", False) or getattr(value, "capped", False) or not value.complete:
            return "partial", "Page has incomplete coverage", "none"
        if not value.records:
            return "empty", "Page completed with no records", "none"
    if isinstance(value, dict):
        status = str(value.get("status", "")).lower()
        if isinstance(value.get("provider_admission"), dict):
            admission = value["provider_admission"]
            return "deferred", sanitize(admission.get("reason") or value.get("provider_reason") or "Provider eligibility deferred"), "none"
        if value.get("uncertain"):
            return "failed", "Delivery outcome is uncertain; reconcile before retry", "send_unknown"
        stages = value.get("stages")
        if isinstance(stages, list) and any(isinstance(r, dict) and str(r.get("status", "")).lower() not in {"ok", "success", "complete", "skipped"} for r in stages):
            return "partial", "One or more stages did not complete cleanly; inspect child operations", "unknown"
        if type(value.get("error")) in {int, float} and value["error"] > 0:
            return "partial" if value.get("ok") or value.get("partial") else "failed", "Batch contains item errors", "unknown"
        if "score" in value and value["score"] is None:
            # A missing score can be caused by transport/auth/deadline failures,
            # not only invalid JSON. Do not infer an error category from prose.
            return "failed", sanitize(value.get("error") or "No valid score was returned"), error_kind(
                value.get("error") if isinstance(value.get("error"), BaseException) else None,
                hint=value.get("error_kind"))
        if status == "unusable_output":
            reasons = value.get("validation_errors") or (value.get("validator") or {}).get("errors") or []
            reason = "; ".join(str(item) for item in reasons) if isinstance(reasons, list) else str(reasons)
            return "failed", sanitize(reason or "Document output failed source validation"), "schema"
        if value.get("rendering_pending") and (status == "approved" or value.get("approved")):
            return "deferred", "Approved document source saved; rendering is queued", "none"
        if value.get("ready") is False or (status == "approved" and "pdf_path" in value and not value["pdf_path"]):
            return "failed", "Preparation did not produce an uploadable document; inspect the document operation", "dependency"
        if status == "exhausted_retries":
            return "failed", "Preparation exhausted its validation retries", "schema"
        if value.get("error") or value.get("ok") is False or status.startswith(("error", "failed", "unavailable", "broken")):
            err = value.get("error") or status or "Operation returned ok=false"
            return "failed", sanitize(err), error_kind(
                err if isinstance(err, BaseException) else None,
                hint=value.get("error_kind") or value.get("error_type"))
        if value.get("errors") or value.get("failed") or value.get("partial") or status in {"partial", "degraded", "partial_failure"}:
            message = value.get("message") or value.get("reason_code")
            reason = value.get("reason_code")
            issues = value.get("errors")
            issues = [item for item in issues if isinstance(item, dict)] if isinstance(issues, list) else []
            if not message:
                message = "; ".join(sanitize(item.get("error") or item.get("message"), 300)
                                    for item in issues[:3] if item.get("error") or item.get("message"))
            kind = ({"missing_application_url": "none", "missing_description": "none",
                     "application_url_unobserved": "schema"}.get(reason)
                    if isinstance(reason, str) else None)
            return "partial", sanitize(message or "Operation completed with errors or incomplete coverage"), (
                kind or error_kind(hint=value.get("error_kind") or value.get("error_type")
                                   or (issues[0].get("kind") if issues else None)))
        if status in {"skipped", "disabled", "not_due", "busy", "busy_or_not_due"}:
            return "skipped", status, "none"
        if status in {"deferred", "cooldown", "budget_deferred", "backoff"}:
            return "deferred", sanitize(value.get("message") or value.get("reason_code") or status), "none"
        if status == "manual_required":
            return "failed", sanitize(value.get("message") or value.get("reason_code") or "Operation could not complete automatically"), "policy"
        if value.get("no_work"):
            return "empty", "No eligible work in this pass", "none"
    if isinstance(value, list) and value and all(isinstance(v, dict) for v in value) and any(v.get("error") for v in value):
        return "partial", "One or more item results contain errors", "unknown"
    if isinstance(value, (list, tuple)) and not value:
        return "empty", "Completed with no results", "none"
    return "success", "Completed", "none"


def observe(component_name, operation=None, *, stage=None, job_arg=None):
    """Transparent sync/async boundary. No retries, filtering or swallowed exceptions."""
    def decorate(function):
        name = operation or function.__name__
        signature = inspect.signature(function)

        def begin(args, kwargs):
            try:
                initialize(component_name)
            except Exception:
                pass
            ctx = dict(_context.get())
            try:
                bound_args = signature.bind_partial(*args, **kwargs)
                bound_args.apply_defaults()
                bound = bound_args.arguments
                provider = bound.get("provider") or getattr(bound.get("self"), "backend", None)
                if isinstance(provider, str):
                    ctx["integration"] = provider
                context = bound.get("context") or bound.get("worker_context")
                if context is not None and getattr(context, "cycle_id", None):
                    ctx["cycle_id"] = str(context.cycle_id)
                task = bound.get("task")
                if task is not None:
                    ctx["task_id"] = str(getattr(task, "task_id", ""))
                    ctx["job_source"] = str(getattr(task, "source_key", ""))
                job = bound.get(job_arg) if job_arg else None
                if job is not None:
                    url = job.get("url", "") if isinstance(job, dict) else str(job)
                    ctx["job_id"] = hashlib.sha256(url.encode()).hexdigest()[:24] if url else ""
                    if isinstance(job, dict):
                        ctx.update(job_title=job.get("title", ""), job_company=job.get("company", ""), job_source=job.get("site", ""))
            except Exception:
                pass
            ctx["parent_span_id"] = ctx.get("span_id", "")
            ctx["span_id"] = uuid.uuid4().hex
            ctx["stage"] = stage or ctx.get("stage") or component_name
            try:
                handle = initialize().workers.enter(component_name, name, ctx)
                if handle:
                    ctx["_worker_handle"] = handle
            except Exception:
                pass
            return ctx

        def worker_finished(outcome):
            try:
                ctx = _context.get()
                handle = ctx.get("_worker_handle")
                if handle and handle.span_id == ctx.get("span_id"):
                    initialize().workers.finish(handle, outcome)
            except Exception:
                pass

        def _finish(value, started):
            outcome, message, ek = result_state(value)
            severity = "error" if outcome == "failed" else "warning" if outcome == "partial" else "info"
            details = {}
            if isinstance(value, dict):
                details = {key: value[key] for key in ("found", "new", "existing", "pages", "selected", "scored", "sent",
                           "processed", "ok", "partial", "error", "approved", "generated", "saved", "failed", "unusable_output",
                           "planned", "drafted", "held", "expired", "research_enqueued", "with_candidates",
                           "profiles_checked", "promoted", "queries", "facts", "leads_found", "profile_failures",
                           "eligible", "attempted", "blocked", "retired", "refreshed", "new_cases", "verified_relevant_contacts")
                           if type(value.get(key)) in {int, float}}
                if component_name == "enrich" and name == "job":
                    details["description_chars"] = len(value.get("full_description") or "")
                    details["has_application_url"] = bool(value.get("application_url"))
                if value.get("errors"):
                    details["errors"] = len(value["errors"]) if isinstance(value["errors"], (list, dict)) else value["errors"]
                if isinstance(value.get("validation_errors"), list):
                    details["validation_errors"] = value["validation_errors"]
                    if value["validation_errors"] and outcome in {"failed", "partial"}:
                        message = "; ".join(sanitize(e, 500) for e in value["validation_errors"][:3] if isinstance(e, str)) or message
                if value.get("error_origin"):
                    details["error_origin"] = value["error_origin"]
                if value.get("validation_report"):
                    details["validation_report"] = Path(str(value["validation_report"])).name
                for key in ("validation_mode", "factual_review", "reason_code", "status"):
                    if isinstance(value.get(key), str):
                        details[key] = value[key]
                if (outcome == "success" and value.get("validation_mode") == "basic"
                        and value.get("factual_review") == "not_run"):
                    message = "Basic checks passed; factual review was not run."
                if isinstance(value.get("provider_admission"), dict):
                    details.update({key: value["provider_admission"][key] for key in
                                    ("reason_code", "reason", "origin", "retryable", "retry_trigger", "scope_id", "next_eligible_at", "evidence_id", "logical_task_id")
                                    if key in value["provider_admission"]})
            job = value.get("job") if job_arg and isinstance(value, dict) else None
            job_context = ({"job_title": job.get("title", ""), "job_company": job.get("company", "")}
                           if isinstance(job, dict) else {})
            record(component_name=component_name, operation=name, kind="finish", outcome=outcome, severity=severity,
                   message=message, error_type=ek, duration=time.monotonic() - started, details=details, **job_context)
            worker_finished(outcome)

        def finish(value, started):
            try:
                if job_arg and isinstance(value, dict):
                    ids = correlation()
                    value["_operation_span_id"] = ids.get("span_id", "")
                    value["_operation_run_id"] = ids.get("run_id", "")
                _finish(value, started)
            except Exception:
                pass

        def failed(exc, started):
            deferred = getattr(exc, "operation_outcome", None) == "deferred" or getattr(exc, "kind", None) == "budget"
            cancelled = error_kind(exc) == "cancelled"
            record(component_name=component_name, operation=name, kind="finish", error=exc,
                   severity="info" if deferred else "warning" if cancelled else "error",
                   outcome="deferred" if deferred else "cancelled" if cancelled else "failed",
                   duration=time.monotonic() - started)
            worker_finished("deferred" if deferred else "cancelled" if cancelled else "failed")

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args, **kwargs):
                with scope(**begin(args, kwargs)):
                    started = time.monotonic()
                    record(component_name=component_name, operation=name, kind="start", outcome="started", severity="info")
                    try:
                        value = await function(*args, **kwargs)
                    except BaseException as exc:
                        failed(exc, started)
                        raise
                    finish(value, started)
                    return value
            return async_wrapper

        @wraps(function)
        def wrapper(*args, **kwargs):
            if component_name == "pipeline" and kwargs.get("dry_run") is True:
                return function(*args, **kwargs)
            with scope(**begin(args, kwargs)):
                started = time.monotonic()
                record(component_name=component_name, operation=name, kind="start", outcome="started", severity="info")
                try:
                    value = function(*args, **kwargs)
                except BaseException as exc:
                    failed(exc, started)
                    raise
                finish(value, started)
                return value
        return wrapper
    return decorate


def worker_recovered(component_name, message):
    """Resolve the current worker's matching connector issue after usable output."""
    try:
        if _recorder is not None:
            _recorder.workers.recovered(_context.get().get('_worker_handle'), component_name, message)
    except Exception:
        pass


@contextmanager
def worker_phase(phase, *, task=""):
    """Fixed phase labels only; never pass model prompts or browser content."""
    manager = None
    try:
        handle = _context.get().get("_worker_handle")
        if handle and _recorder is not None:
            manager = _recorder.workers.phase(handle, phase, task)
            manager.__enter__()
    except Exception:
        manager = None
    try:
        yield
    except BaseException as exc:
        try:
            from .worker_status import issue_code
            if _recorder is not None:
                _recorder.workers.issue(_context.get().get("_worker_handle"), issue_code(exc, kind=error_kind(exc)))
        except Exception:
            pass
        raise
    finally:
        if manager:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass

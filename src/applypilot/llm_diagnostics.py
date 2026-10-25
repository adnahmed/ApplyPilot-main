"""Local-only response evidence. Never attach response bodies to Operations.

Capture what reached ApplyPilot, not LiteLLM's internal provider hops. Request
messages/headers are not captured. Returned text can contain personal data;
credential redaction is best effort, not a general personal-data scrubber.
The bounded journal queue has its own writer; disk failures never fail a call.
"""
from __future__ import annotations

import atexit
import asyncio
from datetime import datetime, timezone
import inspect
from functools import wraps
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import threading
import time
import uuid

log = logging.getLogger(__name__)
DIRECTORY = "logs/llm-responses"
_HEADERS = {
    "x-request-id", "request-id", "x-litellm-call-id", "x-litellm-model-id",
    "x-litellm-version", "x-litellm-attempted-retries", "x-litellm-attempted-fallbacks",
    "x-litellm-response-duration-ms", "x-litellm-overhead-duration-ms",
    "x-litellm-response-cost", "retry-after", "content-type",
    "x-applypilot-evidence-id",
}
_SECRET_FIELD = re.compile(r"^(?:authorization|proxy.authorization|cookie|set.cookie|.*(?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|client[_-]?secret|password|credential|private[_-]?key))$", re.I)
_SECRET_ENV = re.compile(r"TOKEN|SECRET|PASSWORD|API_KEY|MASTER_KEY|COOKIE|CREDENTIAL", re.I)
_SECRET_TEXT = re.compile(r'''(?i)(["']?(?:access_token|refresh_token|id_token|api_key|client_secret|password|authorization|cookie|set-cookie)["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}]+)''')
_journal = None
_lock = threading.Lock()
_last_warning = 0.0


def _mode():
    value = os.environ.get("APPLYPILOT_LLM_RESPONSE_LOG", "full").strip().lower()
    return "off" if value in {"off", "0", "false", "no"} else "metadata" if value == "metadata" else "full"


def _limit():
    try:
        return max(1024, min(16 * 1024 * 1024, int(os.environ.get("APPLYPILOT_LLM_RESPONSE_MAX_CHARS", 1024 * 1024))))
    except (TypeError, ValueError):
        return 1024 * 1024


def _warn():
    global _last_warning
    now = time.monotonic()
    if now - _last_warning > 60:
        _last_warning = now
        # No exception text here: it can contain local paths or response data.
        log.warning("Local model-response diagnostics lost a record (queue/storage unavailable); model execution is unchanged")


def _secrets(extra=()):
    return tuple(sorted({v for k, v in os.environ.items() if len(v) >= 8 and _SECRET_ENV.search(k)} |
                        {str(v) for v in extra if v}, key=len, reverse=True))


def redact_text(text, secrets=()):
    for secret in secrets:
        text = text.replace(secret, "[redacted credential]")
    text = re.sub(r"(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----", "[redacted private key]", text)
    text = re.sub(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_./+=-]+", "[redacted authorization]", text)
    text = _SECRET_TEXT.sub(lambda m: m[1] + '"[redacted credential]"', text)
    text = re.sub(r"\b(?:sk-[\w-]{8,}|gh[pousr]_[\w]{10,}|github_pat_[\w_]{10,}|eyJ[\w-]+\.[\w-]+\.[\w-]+)\b", "[redacted token]", text)
    text = re.sub(r"(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
    return text


def _redact(value, secrets, depth=0):
    if depth > 60:
        return "[diagnostic nesting limit]"
    if isinstance(value, dict):
        return {redact_text(str(k), secrets): "[redacted credential]" if _SECRET_FIELD.match(str(k)) else
                _redact(v, secrets, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v, secrets, depth + 1) for v in value]
    return redact_text(value, secrets) if isinstance(value, str) else value


def _reject_constant(value):
    raise ValueError("Non-JSON constant: " + value)


def _body(value):
    """Serialize only a response value, never an SDK client, request or repr."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if callable(getattr(value, "model_dump", None)):
        value = value.model_dump(mode="json")
    elif not isinstance(value, (dict, list, int, float, bool, type(None))):
        return json.dumps({"diagnostic_unavailable": "response has no JSON serializer", "response_type": type(value).__name__})
    return json.dumps(value, ensure_ascii=False)


class ResponseJournal:
    """One append-only stream per process. No shared rotating-file rename race."""
    def __init__(self, root, *, segment_bytes=8 * 1024**2, total_bytes=128 * 1024**2, retention_days=3, queue_size=64):
        self.root = Path(root)
        self.folder = self.root / DIRECTORY
        self.pid = os.getpid()
        self.name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-p{self.pid}-" + uuid.uuid4().hex[:12]
        self.segment_bytes, self.total_bytes = segment_bytes, total_bytes
        self.retention_seconds = retention_days * 86400
        self.segment = 0
        self.queue = queue.Queue(maxsize=queue_size)
        self.pending_bytes = 0
        self.pending_lock = threading.Lock()
        self.stopping = threading.Event()
        self.dropped = self.write_errors = 0
        self.last_prune = 0.0
        self.thread = threading.Thread(target=self._run, name="llm-response-journal", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def put(self, event):
        weight = 4096 + 4 * sum(len(value) for value in event.values() if isinstance(value, str))
        with self.pending_lock:
            try:
                if self.stopping.is_set() or self.pending_bytes + weight > 32 * 1024**2:
                    raise queue.Full
                self.queue.put_nowait((event, weight))
                self.pending_bytes += weight
            except queue.Full:
                self.dropped += 1

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
        event = dict(event)
        secrets = _secrets((event.pop("_secret", ""),))
        raw = event.pop("_body", None)
        event = _redact(event, secrets)
        if raw is not None:
            try:
                response = _redact(json.loads(raw, parse_constant=_reject_constant), secrets)
                raw = json.dumps(response, ensure_ascii=False)
                parsed = True
            except (ValueError, RecursionError):
                raw = redact_text(raw, secrets)
                parsed = False
            captured = raw[:_limit()]
            event["response_body_chars"] = len(raw)
            event["captured_body_chars"] = len(captured)
            event["response_body_truncated"] = len(raw) > len(captured)
            if parsed and not event["response_body_truncated"]:
                event["response"] = response
            else:
                event["response_text"] = captured
        line = (json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        path = self.folder / f"llm-{self.name}-{self.segment:04d}.jsonl"
        rotated = path.exists() and path.stat().st_size + len(line) > self.segment_bytes
        if rotated:
            self.segment += 1
            path = self.folder / f"llm-{self.name}-{self.segment:04d}.jsonl"
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
        for path in self.folder.glob("llm-*.jsonl"):
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
        reported_drops = 0
        while not self.stopping.is_set() or not self.queue.empty():
            if self.dropped > reported_drops:
                _warn()
                reported_drops = self.dropped
            try:
                event, weight = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._write(event)
            except Exception:
                self.write_errors += 1
                _warn()
            finally:
                with self.pending_lock:
                    self.pending_bytes -= weight
                self.queue.task_done()

    def flush(self, timeout=3):
        end = time.monotonic() + timeout
        while self.queue.unfinished_tasks and time.monotonic() < end:
            time.sleep(0.005)
        return self.queue.unfinished_tasks == 0

    def close(self):
        if self.pid != os.getpid():
            return
        self.stopping.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)


def _emit(event, *, body=None, secret=""):
    global _journal
    mode = _mode()
    if mode == "off":
        return
    try:
        from applypilot import config
        root = config.APP_DIR
        journal = _journal
        if journal is None or journal.pid != os.getpid() or journal.root != root:
            with _lock:
                journal = _journal
                if journal is None or journal.pid != os.getpid() or journal.root != root:
                    if journal is not None and journal.pid == os.getpid():
                        journal.stopping.set()
                    journal = _journal = ResponseJournal(root)
        value = {**event, "capture_mode": mode, "_secret": secret}
        if body is not None and mode == "full":
            raw = _body(body)
            value["original_response_body_chars"] = len(raw)
            # The journal redacts before truncation, away from the shared model
            # loop. Keep the immutable response snapshot until it is written.
            value["_body"] = raw
        journal.put(value)
    except Exception:
        _warn()


def _after_fork():
    global _journal, _lock
    _journal = None
    _lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _best_effort(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            try:
                _warn()
            except Exception:
                pass
    return safe


def _context():
    from applypilot.operations import runtime
    context = runtime._context.get()
    recorder = runtime._recorder
    meta = {key: runtime.label(context[key]) for key in ("stage", "span_id", "parent_span_id", "job_id", "task_id", "cycle_id") if context.get(key)}
    if recorder is not None and recorder.pid == os.getpid():
        meta.update(run_id=recorder.run_id, process_id=recorder.process_id)
    handle = context.get("_worker_handle")
    if handle:
        meta["worker_key"] = handle.key
    return meta


class ModelCall:
    def __init__(self, *, task, provider, model, endpoint, kwargs, secret=""):
        self.id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.secret = secret
        self.received = False
        try:
            context = _context()
        except Exception:
            context = {}
        self.meta = {"format": "applypilot_llm_response", "format_version": 1,
                     "call_id": self.id, "pid": os.getpid(), "task": str(task),
                     "provider": provider, "model_requested": model, "endpoint": endpoint,
                     "max_output_tokens": kwargs.get("max_output_tokens", kwargs.get("max_tokens")),
                     "temperature": kwargs.get("temperature"),
                     "response_format_type": (kwargs.get("response_format") or kwargs.get("text", {}).get("format") or {}).get("type"),
                     "message_count": len(kwargs.get("messages", kwargs.get("input", []))), **context}
        policy = (kwargs.get("extra_body") or {}).get("metadata") or {}
        for name in ("logical_task_id", "task_kind", "output_enforcement", "rlm_id", "rlm_call_id", "rlm_trace_id"):
            if name in policy:
                self.meta[name] = policy[name]
        self.event("request")
        self._worker("requesting", started_at=time.time())
        log.info("LLM request call_id=%s model=%s task=%s; local evidence: %s", self.id, model, task, DIRECTORY if _mode() != "off" else "capture disabled")

    def _worker(self, state, *, started_at=None):
        try:
            from applypilot.operations import runtime
            recorder = runtime._recorder
            handle = runtime._context.get().get("_worker_handle")
            if recorder is not None:
                recorder.workers.model_call(handle, self.id, state, started_at)
        except Exception:
            pass

    @_best_effort
    def event(self, event, *, body=None, **fields):
        _emit({**self.meta, "event": event, "at": datetime.now(timezone.utc).isoformat(),
               "elapsed_seconds": round(time.monotonic() - self.started, 4), **fields}, body=body, secret=self.secret)
        if self.meta.get("rlm_trace_id"):
            from applypilot.reasoning.telemetry import emit
            emit("rlm.gateway_" + event, source="rlm", trace_id=self.meta["rlm_trace_id"],
                 **self.meta, **fields, request_seconds=round(time.monotonic() - self.started, 4))

    @_best_effort
    def response(self, body, *, status=None, headers=None, source="http_body"):
        self.received = True
        safe_headers = {k.lower(): str(v)[:300] for k, v in (headers or {}).items() if k.lower() in _HEADERS}
        self.event("response", body=body, http_status=status, headers=safe_headers, response_source=source)
        self._worker("response_received")

    @_best_effort
    def result(self, text, reason=None):
        self.event("extraction", usable_text=bool(text and text.strip()), visible_text_chars=len(text or ""), reason=reason)
        log.info("LLM response call_id=%s model=%s visible_chars=%s result=%s; local evidence: %s", self.id,
                 self.meta["model_requested"], len(text or ""), reason or "text_received", DIRECTORY if _mode() != "off" else "capture disabled")
        if reason:
            from applypilot import operations as ops
            ops.record(component_name="integrations", operation="model_output", severity="error", error_type="provider",
                       message="Model response has no usable visible text: " + reason,
                       details={"llm_call_id": self.id, "model": self.meta["model_requested"], "reason": reason,
                                "response_log": DIRECTORY if _mode() != "off" else "disabled"})

    @_best_effort
    def failure(self, exc):
        cancelled = isinstance(exc, asyncio.CancelledError)
        from applypilot.scoring.provider_backoff import admission_details
        admission = admission_details(exc) or {}
        response = getattr(exc, "response", None)
        if response is not None and not self.received:
            try:
                self.response(response.text, status=getattr(response, "status_code", None), headers=getattr(response, "headers", {}))
            except Exception:
                pass
        if not self.received and getattr(exc, "body", None) is not None:
            self.response(exc.body, status=getattr(exc, "status_code", None), source="sdk_error_body")
        error_message = str(exc)
        if self.secret:
            error_message = error_message.replace(self.secret, "[redacted credential]")
        self.event("deferred" if admission else "cancelled" if cancelled else "error", exception_type=type(exc).__name__,
                   # Redacted in the private writer; never put this value on the dashboard.
                   error_message=error_message, http_status=getattr(exc, "status_code", None),
                    response_received=self.received, admission=admission or None)
        self._worker("deferred" if admission else "cancelled" if cancelled else "failed")
        log.info("LLM request ended call_id=%s exception=%s response_received=%s; local evidence: %s",
                 self.id, type(exc).__name__, self.received, DIRECTORY if _mode() != "off" else "capture disabled")
        from applypilot import operations as ops
        from applypilot.operations.runtime import error_kind
        ops.record(component_name="integrations", operation="model_request", severity="info" if admission else "warning" if cancelled else "error",
                   outcome="deferred" if admission else "cancelled" if cancelled else "failed", error_type="none" if admission else "cancelled" if cancelled else error_kind(exc),
                   message=(admission.get("reason") or "Waiting for compatible provider capacity") if admission else "Model request cancelled locally" if cancelled else "Model request failed: " + type(exc).__name__,
                    details={"llm_call_id": self.id, "model": self.meta["model_requested"],
                             "http_status": getattr(exc, "status_code", None), "response_received": self.received,
                             **admission,
                            "response_log": DIRECTORY if _mode() != "off" else "disabled"})


async def request(resource, kwargs, *, task, provider, endpoint, secret=""):
    """Exactly one SDK request. Capture BEFORE parse/extraction, never retry it.

    with_raw_response is the OpenAI SDK's documented raw-response interface.
    Minimal SDK-compatible adapters without that interface are captured after
    their serializer and explicitly marked as such (not called raw HTTP).
    """
    call = ModelCall(task=task, provider=provider, model=kwargs.get("model"), endpoint=endpoint, kwargs=kwargs, secret=secret)
    from applypilot import operations as ops
    # Async callers, not only the synchronous chat bridge, report this wait.
    with ops.worker_phase("waiting_provider", task=task):
        try:
            raw_create = getattr(getattr(resource, "with_raw_response", None), "create", None)
            if callable(raw_create):
                raw = await raw_create(**kwargs)
                http = getattr(raw, "http_response", None)
                try:
                    body = http.text if http is not None else raw.text
                    if callable(body):
                        body = body()
                    if inspect.isawaitable(body):
                        body = await body
                    call.response(body, status=getattr(raw, "status_code", None), headers=getattr(raw, "headers", {}))
                except Exception:
                    # Capture failure must not replace a valid provider result.
                    call.event("capture_unavailable", reason="raw body could not be read")
                result = raw.parse()
                if inspect.isawaitable(result):
                    result = await result
            else:
                result = await resource.create(**kwargs)
                try:
                    call.response(result, source="sdk_serialized_response")
                except Exception:
                    call.event("capture_unavailable", reason="response serializer failed")
            return result, call
        except BaseException as exc:
            try:
                call.failure(exc)
            except Exception:
                pass
            raise

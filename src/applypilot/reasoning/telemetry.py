"""Reasoning events visible in the same private trace window as browser calls."""
from __future__ import annotations

import atexit
from collections import Counter, OrderedDict
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import logging
import os
import queue
import re
import threading
import time

current_trace = ContextVar("applypilot_reasoning_trace", default="")
_journal = None
_init_lock = threading.Lock()
log = logging.getLogger(__name__)


class _TraceJournal:
    """One bounded queue per process/workspace, never storage work on callers."""

    def __init__(self, root):
        self.root, self.pid = root, os.getpid()
        from applypilot.runtime_policy import load_policy
        self.policy = load_policy(root).telemetry
        self.queue = queue.Queue(maxsize=self.policy.queue_records)
        self.pending_bytes = 0
        self.max_bytes = self.policy.queue_bytes
        self.stopping = threading.Event()
        self.traces = OrderedDict()
        self.losses = Counter()
        self.state_lock = threading.Lock()
        self.reported_losses = Counter()
        self.last_warning = 0.0
        self.thread = threading.Thread(target=self._run, name="reasoning-trace-journal", daemon=True)
        self.thread.start()
        atexit.register(self.close)

    def lost(self, reason, count=1):
        # This short counter lock never contains I/O or redaction.
        with self.state_lock:
            self.losses[reason] += count

    def put(self, record):
        # All parts are immutable strings. Four bytes per character bounds
        # Python's string storage as well as UTF-8 without another large copy.
        weight = 512 + sum(len(part) * 4 for part in record if part is not None)
        with self.state_lock:
            if self.stopping.is_set():
                self.losses["writer_stopping"] += 1
            elif self.pending_bytes + weight > self.max_bytes:
                self.losses["queue_bytes_full"] += 1
            else:
                try:
                    self.queue.put_nowait((record, weight))
                    self.pending_bytes += weight
                except queue.Full:
                    self.losses["queue_full"] += 1

    def _loss_report(self, trace=None, *, force=False):
        with self.state_lock:
            counts = self.losses.copy()
        if counts == self.reported_losses:
            return
        recorded = trace is not None and trace.emit(
            "telemetry.dropped", pid=self.pid, scope="process",
            dropped_events=sum(counts.values()), reasons=dict(counts))
        now = time.monotonic()
        if force or now - self.last_warning >= 60:
            try:
                log.warning("Reasoning diagnostics dropped %s events in process %s: %s; execution continues",
                            sum(counts.values()), self.pid, dict(counts))
            except Exception:
                pass
            self.last_warning = now
        if recorded:
            self.reported_losses = counts

    def _trace(self, key):
        from applypilot.apply.agent_trace import AgentTrace
        trace = self.traces.pop(key, None)
        if trace is None:
            trace = AgentTrace(*key, root=self.root, register_close=False)
        self.traces[key] = trace
        while len(self.traces) > self.policy.open_traces:
            self.traces.popitem(last=False)[1].close()
        return trace

    def _write_group(self, key, records):
        from applypilot.processes import process_guard
        from applypilot.apply.agent_trace import SOURCES
        trace_id, source = key
        if source in SOURCES and re.fullmatch(r"[a-f0-9]{32}", trace_id):
            directory = self.root / "logs" / "agent-traces"
            deadline = time.monotonic() + .1
            while True:
                try:
                    # The lock covers only this trace. Persistent handlers must
                    # release their streams so another Windows process can rotate.
                    with process_guard(directory / f".{trace_id}-{source}.guard"):
                        trace = self._trace(key)
                        try:
                            self._loss_report(trace)
                            for event, fields, payload in records:
                                if not trace.emit(event, _payload=payload, **fields):
                                    self.lost("write_unavailable")
                        finally:
                            if trace.handler is not None:
                                trace.handler.close()  # Append handlers reopen on the next emit.
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        self.lost("trace_busy", len(records))
                        break
                    self.stopping.wait(.005)
                except Exception:
                    self.lost("write_error", len(records))
                    break
        for event, fields, _ in records:
            # Payloads belong only in private traces. Routine logging also stays
            # on the writer, away from the shared model event loop.
            try:
                log.info("reasoning event=%s trace=%s %s", event, trace_id,
                         {key: value for key, value in fields.items() if key not in {"text", "error_message"}}
                         if source == "rlm" else fields)
            except Exception:
                pass

    def _write_batch(self, batch):
        groups = {}
        for (trace_id, source, event, fields, payload), _ in batch:
            groups.setdefault((trace_id, source), []).append((event, json.loads(fields), payload))
        for key, records in groups.items():
            self._write_group(key, records)

    def _run(self):
        try:
            while not self.stopping.is_set() or not self.queue.empty():
                try:
                    batch = [self.queue.get(timeout=.1)]
                except queue.Empty:
                    self._loss_report()
                    continue
                for _ in range(31):
                    try:
                        batch.append(self.queue.get_nowait())
                    except queue.Empty:
                        break
                try:
                    self._write_batch(batch)
                except Exception:
                    self.lost("writer_error", len(batch))
                finally:
                    count, weight = len(batch), sum(weight for _, weight in batch)
                    batch.clear()
                    with self.state_lock:
                        self.pending_bytes -= weight
                    for _ in range(count):
                        self.queue.task_done()
        finally:
            for trace in self.traces.values():
                trace.close()
            self._loss_report(force=True)

    def close(self):
        if self.pid != os.getpid():
            return
        self.stopping.set()
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=2)
        atexit.unregister(self.close)


def diagnostics_health():
    journal = _journal
    if journal is None or journal.pid != os.getpid():
        return {}
    with journal.state_lock:
        return {"dropped_events": sum(journal.losses.values()), "loss_reasons": dict(journal.losses),
                "pending_events": journal.queue.qsize(), "writer_alive": journal.thread.is_alive()}


def _after_fork():
    global _journal, _init_lock
    _journal = None
    _init_lock = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def emit(event: str, *, trace_id: str = "", source: str = "learning", payload=None, **fields) -> None:
    """Enqueue without waiting for another inference's logging or disk work."""
    global _journal
    trace_id = trace_id or current_trace.get() or os.getenv("APPLYPILOT_AGENT_TRACE_ID", "")
    try:
        from applypilot import config
        root = config.APP_DIR
        journal = _journal
        if journal is None or journal.pid != os.getpid() or journal.root != root:
            with _init_lock:
                journal = _journal
                if journal is None or journal.pid != os.getpid() or journal.root != root:
                    if journal is not None and journal.pid == os.getpid():
                        journal.stopping.set()
                    journal = _journal = _TraceJournal(root)
        fields.setdefault("at", datetime.now(timezone.utc).isoformat())
        # Snapshot once before asynchronous writing: prompts/context dictionaries
        # may be mutated by the next interpreter turn while this record queues.
        if payload is not None:
            payload = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else (
                payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, default=str))
        journal.put((str(trace_id), str(source), str(event),
                     json.dumps(fields, ensure_ascii=False, default=str), payload))
    except Exception:
        # Capture must not replace a model result or cancellation.
        if _journal is not None:
            _journal.lost("enqueue_error")


def diagnostic_snapshot():
    """Observe this process's journal; never start a writer or export queued prose."""
    journal = _journal
    if journal is None or journal.pid != os.getpid():
        return {"pid": os.getpid(), "initialized": False}
    with journal.state_lock:
        return {"pid": journal.pid, "initialized": True,
                "writer_alive": journal.thread.is_alive(),
                "pending_records": journal.queue.qsize(), "pending_bytes": journal.pending_bytes,
                "dropped_events": sum(journal.losses.values()), "losses": dict(journal.losses),
                "capacity": journal.policy.model_dump()}

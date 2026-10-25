"""Persistent RLM execution evidence, outside disposable container workspaces."""
from __future__ import annotations

from contextlib import contextmanager
import os
import re
import sys
import threading
import time
import traceback
import uuid

from .telemetry import current_trace, emit


class RLMTrace:
    def __init__(self, task, application_id=None):
        from applypilot import operations as ops

        self.rlm_id = uuid.uuid4().hex
        candidate = current_trace.get() or os.getenv("APPLYPILOT_AGENT_TRACE_ID", "")
        self.trace_id = candidate if re.fullmatch(r"[a-f0-9]{32}", candidate) else self.rlm_id
        self.started = time.monotonic()
        self.context = {**ops.correlation(), "rlm_id": self.rlm_id, "task": task,
                        "application_id": application_id or "", "pid": os.getpid()}

    def event(self, name, *, payload=None, **fields):
        """Queue evidence; the private writer owns redaction, chunking and disk I/O."""
        try:
            common = {**self.context, "elapsed_seconds": round(time.monotonic() - self.started, 3), **fields}
            emit("rlm." + name, trace_id=self.trace_id, source="rlm", payload=payload, **common)
        except Exception:
            pass

    def failure(self, name, error, **fields):
        self.event(name, payload="".join(traceback.format_exception(error)),
                   error_type=type(error).__name__, **fields)

    @contextmanager
    def span(self, name, *, heartbeat=False, **fields):
        started = time.monotonic()
        span_id = uuid.uuid4().hex
        owner = threading.get_ident()
        stopped = threading.Event()
        self.event(name + "_started", diagnostic_span_id=span_id, **fields)

        def watch():
            while not stopped.wait(15):
                try:
                    elapsed = round(time.monotonic() - started, 3)
                    frame = sys._current_frames().get(owner)
                    stack = "".join(traceback.format_stack(frame)) if frame is not None else "Owner thread exited"
                    self.event(name + "_waiting", diagnostic_span_id=span_id,
                               phase_seconds=elapsed, payload=stack, **fields)
                except Exception:
                    pass

        watcher = threading.Thread(target=watch, name="rlm-diagnostic-watch", daemon=True) if heartbeat else None
        if watcher is not None:
            try:
                watcher.start()
            except RuntimeError:
                watcher = None
                self.event(name + "_watch_unavailable", diagnostic_span_id=span_id, **fields)
        try:
            yield span_id
        except BaseException as exc:
            self.failure(name + "_failed", exc, diagnostic_span_id=span_id,
                         phase_seconds=round(time.monotonic() - started, 3), **fields)
            raise
        else:
            self.event(name + "_finished", diagnostic_span_id=span_id,
                       phase_seconds=round(time.monotonic() - started, 3), **fields)
        finally:
            stopped.set()
            if watcher is not None:
                watcher.join(timeout=0.2)

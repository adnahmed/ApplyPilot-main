"""Activity-local cancellation and attribution, never execution ownership.

Temporal workers own polling, concurrency, retries and shutdown.  This module
has no executor, task registry, capacity allocator, lock, or dispatch API.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar

_task: ContextVar[dict | None] = ContextVar("applypilot_activity", default=None)


class ExecutionStopped(RuntimeError):
    """The current operation was cancelled, without stopping unrelated work."""
    error_kind = "cancelled"


def current_task() -> dict:
    return _task.get() or {}


def check_cancelled() -> None:
    stop = current_task().get("stop_event")
    if stop is not None and stop.is_set():
        raise ExecutionStopped("Activity cancelled")


@contextmanager
def activity_scope(*, key: str, stage: str, stop_event, attempt: int = 1):
    token = _task.set({"key": key, "stage": stage, "stop_event": stop_event,
                       "backend": "temporal", "attempt": attempt})
    try:
        from applypilot.work_context import stage_scope
        with stage_scope(stage=stage, stop_event=stop_event):
            check_cancelled()
            yield
    finally:
        _task.reset(token)


@contextmanager
def browser_operation():
    """Check cancellation around one browser operation; acquire no capacity."""
    check_cancelled()
    try:
        yield
    finally:
        # Resource cleanup belongs to the browser adapter's own context manager.
        pass


@asynccontextmanager
async def async_browser_operation():
    check_cancelled()
    yield

"""Activity-local cooperative cancellation for external networking adapters."""
from __future__ import annotations
import asyncio


def pause_requested() -> bool:
    from applypilot.execution import current_task
    stop = current_task().get("stop_event")
    return stop is not None and stop.is_set()


def require_running() -> None:
    if pause_requested():
        raise asyncio.CancelledError("Networking Activity cancelled")

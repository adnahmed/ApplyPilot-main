"""Read-only reporting for the configured application worker pool."""

from __future__ import annotations

import os
import time


def snapshot() -> dict:
    """Freshness is explicit; a failed census is never a zero-sized queue."""
    from applypilot import config
    from applypilot.live_status import read_component
    try:
        value = read_component(config.APP_DIR, "applications", runtime_id=os.getenv("APPLYPILOT_RUNTIME_ID") or None)
        if value is None:
            return {"selection_available": False, "reason": "capacity_snapshot_unavailable"}
        age = time.time() - float(value.get("reported_at", 0))
        if age < -5 or age > 60:
            return {"selection_available": False, "reason": "capacity_snapshot_stale"}
        run_id = os.getenv("APPLYPILOT_RUNTIME_ID", "")
        if run_id and value.get("runtime_id") != run_id:
            return {"selection_available": False, "reason": "capacity_runtime_mismatch"}
        return value
    except (OSError, ValueError, TypeError):
        return {"selection_available": False, "reason": "capacity_snapshot_unavailable"}


def report(**values) -> None:
    from applypilot.live_status import publish
    publish("applications", {**values, "reported_at": time.time(),
            "runtime_id": os.getenv("APPLYPILOT_RUNTIME_ID", ""), "pid": os.getpid()})

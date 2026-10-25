"""Application worker presence is not evidence that selection succeeded."""
from __future__ import annotations
import math


def application_health(runtime: dict, *, active: int, now: float) -> dict:
    if active:
        return {"state": "running", "reason": "Application workers are processing eligible jobs."}
    # Capacity is the actual common-selector sample, not the raw DB backlog.
    cap = runtime.get("application_capacity") or (runtime.get("services") or {}).get("applications") or {}
    if not cap:
        cap = (runtime.get("engine") or runtime.get("pipeline") or {}).get("application_capacity") or {}
    if not isinstance(cap, dict):
        cap = {}
    try:
        age = now - float(cap.get("checked_at"))
        fresh = math.isfinite(age) and -5 <= age < 30
    except (TypeError, ValueError):
        fresh = False
    if fresh and cap.get("selection_available") is False:
        if str(cap.get("reason", "")).startswith("capacity_snapshot_"):
            return {"state": "checking", "reason": "Application selector report is unavailable or stale; queue status is unknown."}
        return {"state": "error", "reason": "Application selector failed; queue availability is unknown. Inspect worker diagnostics."}
    if fresh and cap.get("selection_available") is True:
        count = cap.get("selectable_jobs")
        valid_count = (isinstance(count, (int, float)) and not isinstance(count, bool)
                       and math.isfinite(count) and count >= 0 and int(count) == count)
        if valid_count and count > 0:
            return {"state": "checking", "pending": count,
                    "reason": f"{count:g} selectable jobs reported; waiting for worker acquisition."}
        if valid_count and count == 0:
            return {"state": "waiting", "pending": 0,
                    "reason": "Latest selector sample found no selectable jobs. Check route, attempts, evidence and document blockers."}
    return {"state": "checking", "reason": "Workers are present, but no fresh selector result is available; queue status is unknown."}

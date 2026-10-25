"""Correlate the preparation interpreter with its exact supervisor launch."""
from __future__ import annotations

import math
import time


def read_engine_heartbeat(root, *, instance_id=None, runtime_id=None, not_before=0.0, now=None):
    """Read-only; a changing PID alone is never ownership or heartbeat evidence.

    The random per-launch token is inherited by a Windows venv's interpreter
    even when Popen refers to its redirector. Old launches cannot keep a new
    launch alive. Neither the file nor its PID grants termination authority.
    """
    now = time.time() if now is None else now
    from applypilot.live_status import read_component
    try:
        value = read_component(root, "preparation", runtime_id=runtime_id)
        if not isinstance(value, dict) or value.get("running") is not True:
            return None
        stamp = value.get("at")
        if (type(stamp) not in (int, float) or not math.isfinite(stamp)
                or not max(now - 20, not_before - 1) <= stamp <= now + 5):
            return None
        if type(value.get("pid")) is not int or value["pid"] <= 0:
            return None
        if instance_id is not None and value.get("instance_id") != instance_id:
            return None
        if runtime_id is not None and value.get("runtime_id") != runtime_id:
            return None
        if not isinstance(value.get("stages"), dict):
            return None
        integer_metrics = {"configured_workers", "effective_workers", "active_permits", "active_tasks",
                           "queued_tasks", "completed"}
        numeric_metrics = {"oldest_runnable_age", "completion_rate"}
        def metrics(row):
            result = dict(row)
            for key in integer_metrics:
                item = result.get(key)
                if type(item) is not int or item < 0:
                    result[key] = None
            for key in numeric_metrics:
                item = result.get(key)
                if type(item) not in {int, float} or not math.isfinite(item) or item < 0:
                    result[key] = None
            return result
        value = metrics(value)
        value["stages"] = {str(key): metrics(row) for key, row in value["stages"].items()
                           if isinstance(row, dict)}
        return value
    except (OSError, ValueError, TypeError, OverflowError):
        return None

"""Content-change notifications only. Native Temporal Schedules own backups."""
from __future__ import annotations
from pathlib import Path
import time
from applypilot import operations as ops
from applypilot.onboarding.storage import write_json
from .common import config, folder


def request_checkpoint(root: Path):
    """Record a dirty fact; never launch a process or make a due/retry decision."""
    try:
        if config(root)["enabled"]:
            write_json(folder(root) / "dirty.json", {"at": time.time()})
    except Exception as exc:
        ops.capture(exc, component_name="backup", operation="request_checkpoint", stage="backup")

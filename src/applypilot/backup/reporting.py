"""Saved backup history and bounded local storage accounting; no network on GET."""
from __future__ import annotations

import os
import logging
from pathlib import Path
import time

from applypilot.onboarding.storage import now, read_json, write_json
from applypilot.operations.runtime import sanitize
from .common import folder

_CACHE = {}


def record(root, *, status, snapshot=None, head=None, error=None, maintenance=None):
    try:
        _record(root, status=status, snapshot=snapshot, head=head, error=error, maintenance=maintenance)
    except (OSError, ValueError):
        logging.getLogger(__name__).warning("Could not save backup history; the recovery checkpoint is unchanged", exc_info=True)


def _record(root, *, status, snapshot=None, head=None, error=None, maintenance=None):
    path = folder(root) / "history.json"
    previous = read_json(path, [])
    previous = previous if isinstance(previous, list) else []
    event = {"at": now(), "status": status}
    if head:
        event["head"] = head
    if snapshot:
        event.update({key: snapshot.get(key) for key in ("id", "bytes", "files", "data_added", "data_added_packed")})
        event["snapshot_at"] = snapshot.get("created_at")
    if error:
        event["error"] = sanitize(error, 1800)
    if maintenance:
        event["maintenance"] = maintenance
    write_json(path, [event, *previous][:100])


def storage(root):
    root = Path(root)
    key = str(root.resolve())
    saved = _CACHE.get(key)
    if saved and time.monotonic() - saved[0] < 30:
        return saved[1]
    target = folder(root)
    report = {"local_bytes": 0, "local_files": 0, "complete": True,
              "scope": "Local backup state, cache, staging and recovery copies; excludes remote repository storage."}
    deadline = time.monotonic() + .3
    for directory, dirs, files in os.walk(target, followlinks=False):
        dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()
                   and not (getattr(Path(directory) / name, "is_junction", lambda: False)())]
        if report["local_files"] >= 20000 or time.monotonic() > deadline:
            report["complete"] = False
            break
        for name in files:
            if report["local_files"] >= 20000 or time.monotonic() > deadline:
                report["complete"] = False
                break
            path = Path(directory) / name
            try:
                if not path.is_symlink():
                    report["local_bytes"] += path.stat().st_size
                    report["local_files"] += 1
            except OSError:
                report["complete"] = False
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = (time.monotonic(), report)
    return report


def report(root, cfg, state):
    try:
        history = read_json(folder(root) / "history.json", [])
    except (OSError, ValueError):
        history = []
    snapshot = state.get("snapshot") or {}
    remote = state.get("remote_storage") or {}
    return {"history": history if isinstance(history, list) else [], "storage": {
        **storage(root), "snapshot_bytes": snapshot.get("bytes"), "snapshot_files": snapshot.get("files"),
        "last_added_bytes": snapshot.get("data_added"), "last_added_packed_bytes": snapshot.get("data_added_packed"),
        "remote_total_bytes": remote.get("bytes") if remote.get("complete") else None,
        "remote_storage": remote, "remote_storage_error": state.get("remote_storage_error")},
        "retention": {"automatic": True, "keep_snapshots": cfg["keep_snapshots"],
                      "when": "After each completed full snapshot",
                      "last_maintenance": state.get("maintenance"),
                      "note": "Expired full snapshots and unreferenced encrypted data are pruned. Git checkpoint history and local rollback copies are retained."}}

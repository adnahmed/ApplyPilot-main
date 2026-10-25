"""Atomic dashboard snapshots and the current Temporal status projection."""
from __future__ import annotations

from pathlib import Path
import json
import os
import tempfile

def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        from applypilot.atomic_io import replace_with_retry
        replace_with_retry(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_runtime(app_dir: Path):
    from .temporal_status import snapshot
    native = snapshot(app_dir)
    reporting = any(row['active'] is not None for row in native['queues'])
    return {'execution_owner': 'Temporal', 'control': native.get('control', {'state': 'unavailable'}),
            'running': reporting, 'stale': not native['available'],
            'heartbeat_stale': not reporting, 'at': native['captured_at'],
            'reason': 'Current worker and queue observations',
            'workers': [], 'worker_activity': [], 'pipeline': {}, 'engine': {},
            'services': {}, 'execution': None, 'application_capacity': None,
            'temporal': native, 'last_report': None}

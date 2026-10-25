"""Bounded retained worker outcomes, independent of live heartbeat freshness."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from applypilot.operations.runtime import sanitize
from applypilot.operations.worker_journal import DIRECTORY, _safe
from .presentation import humanize_issue

_CACHE = {}
_TERMINAL = {"task.finished", "application.guard_outcome", "application.launcher_failed"}


def history(root: Path, *, limit=60):
    root = Path(root)
    key = str(root.resolve())
    saved = _CACHE.get(key)
    if saved and time.monotonic() - saved[0] < 5:
        return {**saved[1], "items": saved[1]["items"][:limit]}
    result = {"items": [], "counts": {}, "available": False, "truncated": False,
              "retention_days": 3, "scope": "Retained worker outcomes from the local journal; includes previous runs."}
    folder = root / DIRECTORY
    if any(path.is_symlink() for path in (root, root / "logs", folder)):
        result["error"] = "Worker history directory is redirected."
        return result
    events, consumed = [], 0
    try:
        files = sorted(folder.glob("worker-*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)
        result["available"] = bool(files)
        result["truncated"] = len(files) > 16
        for path in files[:16]:
            if path.is_symlink():
                continue
            cap = min(256 * 1024, 2 * 1024**2 - consumed)
            if cap <= 0:
                result["truncated"] = True
                break
            size = path.stat().st_size
            with path.open("rb") as stream:
                if size > cap:
                    result["truncated"] = True
                    stream.seek(size - cap)
                    stream.readline(cap)
                raw = stream.read(cap)
            consumed += len(raw)
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(record, dict) and record.get("event") in _TERMINAL | {"task.issue"}:
                    events.append(record)
        events.sort(key=lambda record: str(record.get("at", "")))
        issues, outcomes = {}, []
        for record in events:
            identity = (record.get("run_id"), record.get("span_id") or record.get("job_id"))
            if record.get("event") == "task.issue":
                issues[identity] = record
                continue
            issue = issues.get(identity, {})
            item = {field: _safe(record[field]) for field in (
                "at", "event", "run_id", "trace_id", "worker_key", "worker_id", "stage", "operation",
                "span_id", "job_id", "job_title", "company", "source", "outcome", "reason",
                "task_elapsed_seconds", "elapsed_seconds", "result_code", "exception_type") if field in record}
            reason = record.get("reason") or issue.get("reason")
            item["reason"] = sanitize(humanize_issue(reason), 1800) if reason else ''
            item["severity"] = record.get("severity") or issue.get("severity") or (
                "error" if record.get("outcome") in {"failed", "partial"} or record.get("event") == "application.launcher_failed" else "info")
            if issue.get("exception_type"):
                item["exception_type"] = sanitize(issue["exception_type"], 100)
            outcomes.append(item)
        retained = list(reversed(outcomes))[:100]
        result.update(items=retained, counts=dict(Counter(str(row.get("outcome") or "unknown") for row in retained)),
                      captured_at=datetime.now(timezone.utc).isoformat(),
                      truncated=result["truncated"] or len(outcomes) > 100)
    except OSError as exc:
        result["error"] = sanitize(exc)
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = (time.monotonic(), result)
    return {**result, "items": result["items"][:limit]}

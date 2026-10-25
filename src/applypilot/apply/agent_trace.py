"""Private, bounded application diagnostics shared by the launcher and MCP children.

Trace failures never fail an application. Owner-requested support exports include
bounded, redacted previews. Traces are excluded from the backup allowlist and
are not application or submission state.
"""
from __future__ import annotations

import atexit
from datetime import datetime, timezone
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time

DIRECTORY = Path("logs/agent-traces")
SOURCES = {"agent", "browser", "aleph", "learning", "rlm"}
SEGMENT_BYTES = 4 * 1024**2
TOTAL_BYTES = 256 * 1024**2
READ_BYTES = 64 * 1024
_SECRET = re.compile(r"password|secret|token|cookie|authorization|api[_-]?key", re.I)
_maintenance = {}
_maintenance_lock = threading.Lock()


def _preview(value, depth=0, *, diagnostic=False, secrets=None):
    from applypilot.operations.runtime import sanitize
    if diagnostic and secrets is None:
        from applypilot.llm_diagnostics import _secrets
        secrets = _secrets()
    if depth > 8:
        return "[nesting limit]"
    if isinstance(value, dict):
        if value.get("type") in {"thinking", "redacted_thinking", "reasoning", "image", "image_url"}:
            return {"type": value["type"], "omitted": True}
        return {str(k)[:100]: "[redacted]" if _SECRET.search(str(k)) and not (diagnostic and type(v) in {int, float, bool})
                else _preview(v, depth + 1, diagnostic=diagnostic, secrets=secrets)
                for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [_preview(item, depth + 1, diagnostic=diagnostic, secrets=secrets) for item in value[:100]]
    if type(value) is float:
        return value if math.isfinite(value) else None
    if value is None or type(value) in {int, bool}:
        return value
    text = str(value)
    if text.startswith("data:"):
        return "[data payload omitted]"
    if diagnostic:
        from applypilot.llm_diagnostics import redact_text
        return redact_text(text, secrets)[:16000]
    return sanitize(text, 16000)


def _folder(root):
    root = Path(root)
    folder = root / DIRECTORY
    if any(path.is_symlink() for path in (root, root / "logs", folder)):
        raise OSError("Redirected trace directory")
    return folder


def _prune(folder, keep):
    entries = []
    with os.scandir(folder) as directory:
        for entry in directory:
            if not re.fullmatch(r"[a-f0-9]{32}-(agent|browser|aleph|learning|rlm)\.jsonl(?:\.[12])?", entry.name):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                stat = entry.stat(follow_symlinks=False)
                entries.append((stat.st_mtime, stat.st_size, Path(entry.path)))
            except OSError:
                pass
    total = sum(size for _, size, _ in entries)
    cutoff = time.time() - 3 * 86400
    for stamp, size, path in sorted(entries):
        if path == keep or (stamp >= cutoff and total <= TOTAL_BYTES):
            continue
        try:
            path.unlink(missing_ok=True)
            total -= size
        except OSError:
            pass  # An active writer can retain its file on Windows.


def _can_write(folder, keep):
    if not _maintenance_lock.acquire(blocking=False):
        return _maintenance.get(folder, (0, False))[1]
    try:
        now = time.monotonic()
        checked, available = _maintenance.get(folder, (float("-inf"), False))
        if now - checked < 60:
            return available
        # Share retention work across short-lived traces without making other
        # worker threads wait for a directory scan or retry a failed scan.
        _maintenance[folder] = (now, False)
        _prune(folder, keep)
        available = shutil.disk_usage(folder).free >= 64 * 1024**2
        _maintenance[folder] = (time.monotonic(), available)
        return available
    finally:
        _maintenance_lock.release()


class _QuietFileHandler(RotatingFileHandler):
    def handleError(self, record):
        self.failed = True  # Disk/rotation failures must not break the MCP stdio protocol.


class AgentTrace:
    def __init__(self, trace_id, source="agent", *, root=None, register_close=True):
        self.trace_id = str(trace_id)
        self.source = source
        self.handler = None
        if not re.fullmatch(r"[a-f0-9]{32}", self.trace_id) or source not in SOURCES:
            return
        try:
            from applypilot import config
            self.folder = _folder(config.APP_DIR if root is None else root)
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.path = self.folder / f"{self.trace_id}-{source}.jsonl"
            if any(Path(str(self.path) + suffix).is_symlink() for suffix in ("", ".1", ".2")):
                return
            self.handler = _QuietFileHandler(self.path, maxBytes=SEGMENT_BYTES, backupCount=2,
                                             encoding="utf-8", delay=True)
            self.handler.setFormatter(logging.Formatter("%(message)s"))
            if register_close:
                atexit.register(self.close)
        except (OSError, ValueError):
            self.handler = None

    def emit(self, event, *, _payload=None, **fields):
        if self.handler is None:
            return False
        try:
            if not _can_write(self.folder, self.path):
                return False
            diagnostic = self.source == "rlm"
            if diagnostic:
                from applypilot.llm_diagnostics import redact_text, _secrets
                secrets = _secrets()
            else:
                secrets = None
            entry = {"at": datetime.now(timezone.utc).isoformat(), "trace_id": self.trace_id,
                     "source": self.source, "event": str(event)[:100],
                     **_preview(fields, diagnostic=diagnostic, secrets=secrets)}
            records = [entry]
            if diagnostic and _payload is not None:
                text = _payload.decode("utf-8", errors="replace") if isinstance(_payload, bytes) else (
                    _payload if isinstance(_payload, str) else json.dumps(_payload, ensure_ascii=False, default=str))
                text = redact_text(text, secrets)
                length = len(text)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                captured = text[:128000]
                parts = max(1, (len(captured) + 7999) // 8000)
                records = [{**entry, "payload_chars": length, "captured_chars": len(captured),
                            "payload_sha256": digest, "truncated": length > len(captured),
                            "part": part + 1, "parts": parts, "text": captured[part * 8000:(part + 1) * 8000]}
                           for part in range(parts)]
            self.handler.failed = False
            for record in records:
                line = json.dumps(record, ensure_ascii=False, allow_nan=False)
                if len(line.encode("utf-8")) > READ_BYTES // 2:
                    record = {"at": entry["at"], "trace_id": self.trace_id, "source": self.source,
                              "event": str(event)[:100], "preview": line[:6000], "truncated": True}
                    line = json.dumps(record, ensure_ascii=False)
                self.handler.handle(logging.LogRecord(__name__, logging.INFO, "", 0, line, (), None))
            self.path.chmod(0o600)
            return not self.handler.failed
        except Exception:
            return False

    def close(self):
        atexit.unregister(self.close)
        handler, self.handler = self.handler, None
        if handler is not None:
            try:
                handler.close()
            except OSError:
                pass


class _ChildHandler(logging.Handler):
    def __init__(self, trace):
        super().__init__(logging.INFO)
        self.trace = trace

    def emit(self, record):
        try:
            self.trace.emit("log", logger=record.name, level=record.levelname, text=record.getMessage())
        except Exception:
            pass


def child_logging(source):
    """Attach native Python diagnostics without writing anything to stdout."""
    trace_id = os.environ.get("APPLYPILOT_AGENT_TRACE_ID", "")
    if not re.fullmatch(r"[a-f0-9]{32}", trace_id) or source not in SOURCES:
        return None
    trace = AgentTrace(trace_id, source)
    root = logging.getLogger()
    root.addHandler(_ChildHandler(trace))
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return trace


def read_trace(root, query):
    """Read a bounded byte window using only validated IDs and rotated segments."""
    empty = {"available": False, "text": "Trace is not available yet.", "start": 0,
             "end": 0, "size": 0, "next_before": None}
    trace_id, source = str(query.get("id", "")), query.get("source", "agent")
    try:
        segment = int(query.get("segment", 0))
        before = int(query["before"]) if query.get("before") is not None else None
        if (not re.fullmatch(r"[a-f0-9]{32}", trace_id) or source not in SOURCES
                or segment not in {0, 1, 2} or (before is not None and before < 0)):
            return {**empty, "text": "Invalid trace selection."}
        folder = _folder(root)
        path = folder / (f"{trace_id}-{source}.jsonl" + (f".{segment}" if segment else ""))
        if path.is_symlink():
            return empty
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            end = min(size, before) if before is not None else size
            start = max(0, end - READ_BYTES)
            stream.seek(start)
            data = stream.read(end - start)
        # Drop a partial leading record without leaving a gap in pagination.
        if start and b"\n" in data:
            newline = data.index(b"\n") + 1
            start += newline
            data = data[newline:]
        return {"available": True, "text": data.decode("utf-8", errors="replace"),
                "start": start, "end": end, "size": size, "next_before": start or None}
    except (OSError, ValueError, TypeError):
        return empty

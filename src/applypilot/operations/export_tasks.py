"""Authenticated dashboard support exports with bounded, retryable retention.

The HTTP request starts a local task; it never waits for ZIP construction or
buffers the whole ZIP in browser memory. Task IDs are random, not filesystem paths.
"""
from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import secrets
import threading
import time
import zipfile

from .bundle import Redactor, build_bundle
from .runtime import exception_details


@dataclass
class _Task:
    identifier: str
    created: float
    expires: float
    state: str = "building"
    phase: str = "queued"
    path: Path | None = None
    size: int = 0
    error: str = ""
    error_details: dict = field(default_factory=dict)
    run_id: str = ""
    coverage_complete: bool | None = None
    coverage_warnings: int = 0


class SupportExports:
    retention_seconds = 1800

    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()
        self._tasks: dict[str, _Task] = {}
        self._closed = False

    @staticmethod
    def _remove(task):
        if task.path is not None:
            with suppress(OSError):
                task.path.unlink(missing_ok=True)

    def _prune(self):
        for key, task in list(self._tasks.items()):
            if task.state != "building" and task.expires < time.time():
                self._remove(task)
                del self._tasks[key]

    def _view(self, task):
        message = None
        if task.state == "ready":
            message = "Support ZIP is ready."
            if task.coverage_warnings:
                message += " Some records were unavailable, excluded, or truncated; see manifest.json for details."
        elif task.state == "building":
            message = "Collecting diagnostics: " + task.phase.replace("_", " ") + "…"
        return {"id": task.identifier, "state": task.state, "phase": task.phase,
                "export_kind": "support", "run_id": task.run_id,
                "title": "Support ZIP", "download_label": "Download support ZIP",
                "message": message, "coverage_complete": task.coverage_complete,
                "coverage_warnings": task.coverage_warnings,
                "created_at": task.created, "expires_at": task.expires,
                "bytes": task.size, "error": task.error or None,
                "error_details": task.error_details,
                "download_url": f"/api/operations/support-bundle/{task.identifier}/download"
                    if task.state == "ready" else None}

    def start(self, run_id=""):
        if run_id:
            from applypilot.web.work_export import validate_run_id

            run_id = validate_run_id(self.root, run_id)
        with self._lock:
            if self._closed:
                raise ValueError("The dashboard is stopping")
            self._prune()
            for task in self._tasks.values():
                if task.state == "building":
                    if task.run_id != run_id:
                        raise ValueError("A support ZIP for another selection is being built. Let it finish before retrying.")
                    return self._view(task)
            if len(self._tasks) >= 3:
                raise ValueError("Three support exports are retained. Download an existing export or retry after retention expires.")
            now = time.time()
            task = _Task(secrets.token_hex(24), now, now + self.retention_seconds, run_id=run_id)
            self._tasks[task.identifier] = task
            threading.Thread(target=self._build, args=(task,), daemon=True,
                name="support-export").start()
            return self._view(task)

    def _build(self, task):
        def progress(phase):
            with self._lock:
                task.phase = phase
        path = None
        try:
            path = build_bundle(self.root, run_id=task.run_id, progress=progress)
            size = path.stat().st_size
            if size <= 22:
                raise ValueError("Empty support archive")
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            with self._lock:
                if self._closed:
                    path.unlink(missing_ok=True)
                    return
                task.path, task.size = path, size
                task.coverage_complete = manifest["complete_within_declared_scope"]
                task.coverage_warnings = sum(entry["status"] != "complete" for entry in manifest["entries"])
                task.state, task.phase = "ready", "complete"
                task.expires = time.time() + self.retention_seconds
        except Exception as exc:
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            with self._lock:
                task.state, task.phase = "failed", "failed"
                task.expires = time.time() + 300
                # Do not reflect filesystem paths or arbitrary exception text.
                task.error = f"Support export failed ({type(exc).__name__}). No ZIP was published; inspect the local dashboard log."
                task.error_details = Redactor(self.root).value(exception_details(exc))
            import logging
            logging.getLogger(__name__).exception("Support export failed")

    def _lookup(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{48}", identifier):
            raise LookupError("Unknown support export")
        self._prune()
        task = self._tasks.get(identifier)
        if task is None:
            raise LookupError("Support export expired or the dashboard restarted. Create a new export.")
        return task

    def status(self, identifier):
        with self._lock:
            return self._view(self._lookup(identifier))

    def download(self, identifier):
        with self._lock:
            task = self._lookup(identifier)
            if task.state != "ready" or task.path is None:
                raise ValueError("Support export is not ready")
            try:
                info = task.path.stat()
            except OSError:
                raise LookupError("Support file is no longer available. Create a new export.") from None
            if info.st_size != task.size or info.st_size <= 22:
                raise ValueError("Support file validation failed; refusing an empty or changed download")
            # HEAD, range, failed and repeated GETs must not delete the artifact.
            task.expires = time.time() + self.retention_seconds
            filename = "applypilot-support-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(task.created)) + ".zip"
            return task.path, filename, info

    def close(self):
        with self._lock:
            self._closed = True
            for task in self._tasks.values():
                self._remove(task)


class WorkExports(SupportExports):
    """Retained JSON snapshots for one exact operations run."""

    def __init__(self, root: Path, database: Path):
        super().__init__(root)
        self.database = Path(database)

    def _view(self, task):
        value = super()._view(task)
        value.update(export_kind="work", title="Work report", download_label="Download work JSON",
                     message="Work report is ready." if task.state == "ready" else
                     "Preparing work report: " + task.phase.replace("_", " ") + "…"
                     if task.state == "building" else None)
        value["run_id"] = task.run_id
        value["coverage_complete"] = task.coverage_complete
        value["coverage_warnings"] = task.coverage_warnings
        value["download_url"] = (
            f"/api/work/exports/{task.identifier}/download"
            if task.state == "ready" else None
        )
        return value

    def start(self, run_id):
        from applypilot.web.work_export import validate_run_id

        run_id = validate_run_id(self.root, run_id)
        with self._lock:
            if self._closed:
                raise ValueError("The dashboard is stopping")
            self._prune()
            for task in self._tasks.values():
                if task.run_id == run_id and task.state == "building":
                    return self._view(task)
            if len(self._tasks) >= 3:
                raise ValueError("Three work exports are retained. Download an existing export or retry after retention expires.")
            now = time.time()
            task = _Task(secrets.token_hex(24), now, now + self.retention_seconds,
                         run_id=run_id)
            self._tasks[task.identifier] = task
            threading.Thread(target=self._build, args=(task,), daemon=True,
                             name="work-export").start()
            return self._view(task)

    def _build(self, task):
        from applypilot.web.work_export import build_report

        def progress(phase):
            with self._lock:
                task.phase = phase

        path = None
        try:
            path, coverage = build_report(self.root, self.database, task.run_id,
                                          progress=progress)
            size = path.stat().st_size
            if size <= 2:
                raise ValueError("Empty work export")
            with self._lock:
                if self._closed:
                    path.unlink(missing_ok=True)
                    return
                task.path, task.size = path, size
                task.coverage_complete = coverage["complete"]
                task.coverage_warnings = coverage["warnings"]
                task.state, task.phase = "ready", "complete"
                task.expires = time.time() + self.retention_seconds
        except Exception as exc:
            if path is not None:
                with suppress(OSError):
                    path.unlink(missing_ok=True)
            with self._lock:
                task.state, task.phase = "failed", "failed"
                task.expires = time.time() + 300
                task.error = ("Work export failed (" + type(exc).__name__ + "). "
                              "No JSON file was published; inspect the local dashboard log.")
                task.error_details = Redactor(self.root).value(exception_details(exc))
            import logging
            logging.getLogger(__name__).exception("Work export failed")

    def download(self, identifier):
        with self._lock:
            task = self._lookup(identifier)
            if task.state != "ready" or task.path is None:
                raise ValueError("Work export is not ready")
            try:
                info = task.path.stat()
            except OSError:
                raise LookupError("Work export is no longer available. Create it again.") from None
            if info.st_size != task.size or info.st_size <= 2:
                raise ValueError("Work export validation failed; refusing an empty or changed download")
            task.expires = time.time() + self.retention_seconds
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(task.created))
            return task.path, f"applypilot-work-{task.run_id[:16]}-{stamp}.json", info

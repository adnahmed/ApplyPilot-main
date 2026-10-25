"""Producer-owned PostgreSQL status, delivered as one latest-value SSE stream."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress
from pathlib import Path

import psycopg
from sse_starlette import EventSourceResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from applypilot import postgres
from applypilot.live_status import read_status

log = logging.getLogger(__name__)
CONFIG_FILES = frozenset({
    ".env", "profile.json", "resume.txt", "searches.yaml", "searches.target.yaml",
    "discovery_runtime.yaml", "company_limits.yaml", "unattended.json", "autopilot-control.json",
})
FILE_DIRECTORIES = ("setup", "setup/tasks", "setup/jobs", "github_sync")


def _file_change(root, path):
    try:
        relative = Path(path).relative_to(root).as_posix()
    except ValueError:
        return False
    return relative in CONFIG_FILES or any(
        relative == name or relative.startswith(name + "/") for name in FILE_DIRECTORIES
    )


class StatusHub:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.latest = None
        self.version = 0
        self.generation = 0
        self.automation_generation = 0
        self.condition = asyncio.Condition()
        self.ready = asyncio.Event()
        self.automation_ready = asyncio.Event()
        self.changed = asyncio.Event()
        self.status_dirty = self.data_dirty = False
        self.clients = 0
        self.closed = False
        self.tasks = []
        self.listener = None
        self._subscription = None
        self.stopping = threading.Event()
        self.schemas = {postgres.namespace(self.root / name) for name in postgres.STORE_PATHS}

    async def snapshot(self):
        value = await run_in_threadpool(read_status, self.root)
        if not value.available:
            raise RuntimeError("The live status store is unavailable")
        return value.model_dump(mode="json")

    def mark(self, status=False, data=False):
        if not self.closed:
            self.status_dirty |= status
            self.data_dirty |= data
            self.changed.set()

    def notification(self, channel, payload):
        if channel == "applypilot_changed":
            if payload in self.schemas:
                self.mark(data=True)
        elif channel == "applypilot_live":
            try:
                value = json.loads(payload)
            except (TypeError, ValueError):
                return
            if isinstance(value, dict) and value.get("root") == str(self.root):
                self.mark(status=True)

    def _set_subscription(self, subscription):
        if self.closed:
            return
        self._subscription = subscription
        self.ready.clear()
        self.mark(status=subscription is not None, data=subscription is not None)

    def listen(self, loop):
        # The dedicated synchronous connection also works with Windows' default
        # Proactor loop. It never borrows a pooled application transaction.
        while not self.stopping.is_set():
            try:
                with psycopg.connect(postgres.server_uri(), autocommit=True,
                                     connect_timeout=3, application_name="applypilot:status-listener") as conn:
                    conn.execute("LISTEN applypilot_live")
                    conn.execute("LISTEN applypilot_changed")
                    if self.stopping.is_set():
                        return
                    # Subscribe before reloading: commits during reconnect are
                    # represented by either this read or a queued notification.
                    loop.call_soon_threadsafe(self._set_subscription, object())
                    while not self.stopping.is_set():
                        for notice in conn.notifies(timeout=1):
                            if self.stopping.is_set():
                                return
                            loop.call_soon_threadsafe(self.notification, notice.channel, notice.payload)
            except (OSError, ValueError, RuntimeError, psycopg.Error) as exc:
                if self.stopping.is_set():
                    return
                loop.call_soon_threadsafe(self._set_subscription, None)
                log.warning("Status notifications disconnected (%s); reconnecting", type(exc).__name__)
                self.stopping.wait(2)

    async def pump(self):
        while not self.closed:
            await self.changed.wait()
            await asyncio.sleep(.1)
            status, data = self.status_dirty, self.data_dirty
            self.status_dirty = self.data_dirty = False
            self.changed.clear()
            value = self.latest
            subscription = self._subscription
            if status and subscription is not None:
                try:
                    sample = await self.snapshot()
                except (OSError, ValueError, TypeError, RuntimeError, psycopg.Error) as exc:
                    self.ready.clear()
                    log.warning("Live status unavailable (%s)", type(exc).__name__)
                else:
                    # A reconnect must finish its own read before becoming ready.
                    if subscription is self._subscription:
                        value = sample
                        self.ready.set()
            async with self.condition:
                if value is not None and value != self.latest:
                    self.latest = value
                    self.version += 1
                if data:
                    self.generation += 1
                self.condition.notify_all()

    async def watch_files(self):
        from watchfiles import awatch

        try:
            while not self.closed:
                paths = [self.root, *(
                    self.root / name for name in FILE_DIRECTORIES
                    if (self.root / name).is_dir() and not (self.root / name).is_symlink()
                )]
                async for changes in awatch(
                    *paths, recursive=False, force_polling=False, debounce=250,
                    watch_filter=lambda change, path: _file_change(self.root, path),
                ):
                    self.mark(data=True)
                    if any(Path(path) == self.root / name
                           for _, path in changes for name in FILE_DIRECTORIES):
                        break  # Include newly created setup/backup directories.
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as exc:
            log.warning("Configuration file notifications unavailable (%s)", type(exc).__name__)

    async def watch_automation(self):
        from applypilot.orchestration.connection import connect
        from .temporal_status import refresh, unavailable

        client = None
        previous = None
        while not self.closed:
            try:
                async with asyncio.timeout(10):
                    if client is None:
                        client = await connect(self.root)
                    sample = await refresh(self.root, client=client)
            except Exception as exc:
                log.warning("Automation observations unavailable (%s)", type(exc).__name__)
                sample = unavailable(self.root)
            if not sample['available']:
                client = None
            # Sample time alone is not a change; worker heartbeat freshness is.
            observation = {**sample, 'captured_at': None}
            if observation != previous:
                previous = observation
                async with self.condition:
                    self.automation_ready.set()
                    self.automation_generation += 1
                    self.condition.notify_all()
            await asyncio.sleep(2)

    def start(self):
        if self.tasks or self.closed:
            return
        self.tasks = [asyncio.create_task(self.pump(), name="dashboard-status"),
                      asyncio.create_task(self.watch_files(), name="dashboard-config-watch"),
                      asyncio.create_task(self.watch_automation(), name="dashboard-automation-watch")]
        self.listener = threading.Thread(target=self.listen, args=(asyncio.get_running_loop(),),
                                         name="dashboard-postgres-listener", daemon=True)
        self.listener.start()

    async def response(self, request, security):
        if not security.session(request):
            return JSONResponse({"error": "Dashboard session expired"}, status_code=401)
        if self.closed or self.clients >= 32:
            return JSONResponse({"error": "Live connection limit reached"}, status_code=503,
                                headers={"Retry-After": "5"})
        self.start()
        if self._subscription is not None and not self.ready.is_set():
            self.mark(status=True)
        try:
            async with self.condition:
                await asyncio.wait_for(self.condition.wait_for(
                    lambda: self.ready.is_set() or self.automation_ready.is_set() or self.closed), timeout=5)
        except TimeoutError:
            return JSONResponse({"error": "Live status unavailable"}, status_code=503,
                                headers={"Retry-After": "3"})
        if self.closed or self.clients >= 32:
            return JSONResponse({"error": "Live connection limit reached"}, status_code=503,
                                headers={"Retry-After": "5"})
        self.clients += 1

        async def events():
            version = generation = automation_generation = -1
            try:
                while not self.closed:
                    if not security.session(request):
                        yield {"event": "session-expired", "data": "{}"}
                        return
                    if not self.ready.is_set() and not self.automation_ready.is_set():
                        return
                    async with self.condition:
                        if ((not self.ready.is_set() or version == self.version)
                                and generation == self.generation
                                and (not self.automation_ready.is_set()
                                     or automation_generation == self.automation_generation)):
                            with suppress(TimeoutError):
                                await asyncio.wait_for(self.condition.wait(), timeout=10)
                            continue  # Recheck the session even without producer changes.
                        snapshot = self.latest if self.ready.is_set() else None
                        current_version, current_generation = self.version, self.generation
                        current_automation_generation = self.automation_generation
                    if snapshot is not None and current_version != version:
                        version = current_version
                        yield {"event": "status", "data": json.dumps(snapshot, allow_nan=False), "retry": 3000}
                    if current_generation != generation:
                        generation = current_generation
                        yield {"event": "data_changed", "data": json.dumps({"generation": generation})}
                    if self.automation_ready.is_set() and current_automation_generation != automation_generation:
                        automation_generation = current_automation_generation
                        yield {"event": "automation_changed", "data": json.dumps({"generation": automation_generation})}
            finally:
                self.clients -= 1

        return EventSourceResponse(events(), ping=10, send_timeout=15,
                                   headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    async def close(self):
        self.closed = True
        self._subscription = None
        self.ready.clear()
        self.automation_ready.clear()
        self.stopping.set()
        async with self.condition:
            self.condition.notify_all()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.listener:
            await run_in_threadpool(self.listener.join, 4)

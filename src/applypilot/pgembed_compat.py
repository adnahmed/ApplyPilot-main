"""Crash-safe handle bookkeeping for the pinned pgembed 0.2.0 provider."""
from __future__ import annotations

import importlib.util
import logging
import shutil
from pathlib import Path

import psutil
from pgembed.utils import DiskList, PostmasterInfo
from pgembed.postgres_server import PostgresServer

from .atomic_io import atomic_record
from .processes import process_alive

log = logging.getLogger(__name__)

_LOG_ROTATE_BYTES = 10 * 1024 * 1024


def ensure_timezone_files() -> None:
    """Copy the tzdata wheel's compiled zoneinfo into pgembed's timezone dir.

    pgembed 0.2.0 ships no timezone data, so every numeric SET TIME ZONE
    probes a missing directory and spams the server log. Copying the wheel's
    IANA zoneinfo is idempotent and safe on a live server, where the files are
    opened on demand rather than at startup.
    """
    try:
        import pgembed
    except ImportError:
        return
    spec = importlib.util.find_spec('tzdata')
    if spec is None or spec.submodule_search_locations is None:
        log.warning('tzdata is not installed; timezone files will remain missing')
        return
    source = Path(list(spec.submodule_search_locations)[0]) / 'zoneinfo'
    if not source.is_dir():
        log.warning('tzdata wheel has no zoneinfo payload; timezone files remain missing')
        return
    from pgembed._commands import POSTGRES_BIN_PATH
    target = Path(POSTGRES_BIN_PATH).resolve().parent / 'share' / 'postgresql' / 'timezone'
    if target.is_symlink() or target.exists() and not target.is_dir():
        raise RuntimeError('Redirected PostgreSQL timezone directory')
    if (target / 'UTC').is_file() and (target / 'Etc' / 'UTC').is_file():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, dirs_exist_ok=True)
    log.info('Installed timezone data into %s', target)


def rotate_postgres_log(pgdata: Path) -> None:
    """Rotate an oversized postmaster log before a fresh start.

    PostgreSQL holds the log open once running, so rotation runs only while
    the server is stopped, before construction hands off to pg_ctl.
    """
    log_file = pgdata / 'log'
    try:
        if not log_file.is_file() or log_file.stat().st_size <= _LOG_ROTATE_BYTES:
            return
        rotated = pgdata / 'log.old'
        rotated.unlink(missing_ok=True)
        log_file.replace(rotated)
        log.warning('Rotated oversized PostgreSQL log (>%d bytes) to %s', _LOG_ROTATE_BYTES, rotated)
    except OSError as exc:
        log.warning('PostgreSQL log rotation failed: %s', exc)


def ready_postmaster(data):
    """Observe a live cluster without starting it or changing its owner list."""
    try:
        info = PostmasterInfo.read_from_pgdata(data)
        if (info is not None and info.pgdata.resolve() == data
                and info.status == 'ready' and info.is_running()
                and abs(info.process.create_time() - info.start_time.timestamp()) <= 2):
            return info
    except (OSError, ValueError, AssertionError, psutil.Error):
        pass
    return None


class _AttachLock:
    """Use pgembed's own lock protocol, with a bounded live-attachment wait."""
    def __init__(self, lock):
        self.lock = lock

    def __enter__(self):
        if not self.lock.acquire(timeout=5):
            raise TimeoutError('PostgreSQL ownership registration is busy; retry the connection')
        return self

    def __exit__(self, *exc):
        self.lock.release()


class ManagedPostgresServer(PostgresServer):
    def __init__(self, pgdata, *, minimum_pool: int, cleanup_mode='stop', attach_only=False):
        self.minimum_pool = minimum_pool
        self.attach_only = attach_only
        if attach_only:
            self._lock = _AttachLock(self._lock)
        super().__init__(pgdata, cleanup_mode=cleanup_mode)

    def ensure_pgdata_inited(self) -> None:
        if self.attach_only:
            # The owner/readiness check below must succeed before a URI escapes.
            # An attaching helper never initializes or reconfigures this cluster.
            return
        super().ensure_pgdata_inited()
        from .postgres_capacity import configure_embedded_connections
        configure_embedded_connections(self.pgdata, self.minimum_pool)

    def ensure_postgres_running(self) -> None:
        if not self.attach_only:
            return super().ensure_postgres_running()
        info = ready_postmaster(self.pgdata)
        if info is None:
            raise ConnectionError('PostgreSQL is no longer ready; retry the connection')
        self._postmaster_info = info


class HandlePids(DiskList):
    """Keep pgembed's list format and locking, but publish whole JSON records."""

    def get(self) -> list[int]:
        try:
            values = super().get()
            if not isinstance(values, list) or any(type(pid) is not int or pid <= 0 for pid in values):
                raise ValueError('Invalid PostgreSQL handle registry')
        except ValueError:
            # Lost ownership cannot be reconstructed safely. Retain all live
            # PIDs so repairing metadata cannot stop another process's database.
            values = [pid for pid in psutil.pids() if pid > 0]
            self.put(values)
            log.warning('Recovered unreadable PostgreSQL handle registry; automatic database shutdown deferred '
                        'to protect existing users.')
        return [pid for pid in values if process_alive(pid)]

    def put(self, values: list[int]) -> None:
        atomic_record(self.path, values)

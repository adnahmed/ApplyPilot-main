"""Worker demand and bounded capacity for the shared PostgreSQL server."""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import tempfile

from .atomic_io import replace_with_retry

_pending_connection_releases: dict[str, set[str]] = {}
_capacity_snapshots: dict[str, dict] = {}


def pool_size(workers: int | None = None) -> int:
    from .runtime_policy import load_policy
    return load_policy(workers=workers).capacity.database_connections


def embedded_connection_limit(version: str, minimum_pool: int, *, reserved_connections: int) -> int:
    import psutil
    # PostgreSQL startup must not import an application execution engine.
    # Remote deployments use their configured server; this is only the embedded
    # database's sizing input. The deployment owns its actual container limits.
    return _embedded_connection_limit(version, psutil.virtual_memory().total,
                                      os.cpu_count() or 1, minimum_pool, reserved_connections)


@lru_cache(maxsize=8)
def _embedded_connection_limit(version: str, memory: int, cores: int, minimum_pool: int,
                               reserved_connections: int) -> int:
    from pg_configurator import PGConfigurator

    defaults = PGConfigurator.get_arg_parser().parse_args([])
    calculator = PGConfigurator(defaults, ext_params=None)
    demanded = minimum_pool + reserved_connections
    common = dict(
        cpu_cores=cores, ram_value=memory, pg_version=version,
        duty_db='oltp', replication_mode='none',
        platform='WINDOWS' if os.name == 'nt' else 'LINUX',
        reserved_ram_percent=0, reserved_system_ram='0B',
    )
    try:
        # Probe the RAM-derived ceiling before supplying the workload floor:
        # the tuner rejects min_conns above that ceiling.
        calculator.make_conf(**common, min_conns=1, max_conns=defaults.max_conns)
        capacity = int(calculator.last_calculation['connection_capacity'])
        settings = calculator.make_conf(**common, min_conns=min(demanded, capacity), max_conns=capacity)
        result = int(settings['max_connections'])
        if result <= reserved_connections:
            raise ValueError('Native PostgreSQL reserved slots exhaust the calculated connection capacity')
    except ValueError as exc:
        raise RuntimeError(f'Cannot calculate embedded PostgreSQL capacity: {exc}') from exc
    # Only consume the connection recommendation. Preserve PostgreSQL's WAL,
    # durability, memory, replication, and extension settings.
    return result


def server_connection_capacity(dsn: str) -> int:
    """Read the running limit, including settings that require a server restart."""
    return refresh_connection_capacity(dsn)['server_capacity']


def _budget_path(dsn):
    import hashlib
    from .config import APP_DIR

    directory = APP_DIR / 'postgres' / 'connection-budgets'
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (hashlib.sha256(dsn.encode()).hexdigest() + '.json')


def _live_leases(path):
    import json
    import psutil

    if path.is_symlink():
        raise RuntimeError('Redirected PostgreSQL connection budget')
    rows = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    alive = {}
    for row in rows.values():
        owner = (row['pid'], row['started'])
        if owner not in alive:
            try:
                alive[owner] = abs(psutil.Process(owner[0]).create_time() - owner[1]) < .01
            except psutil.NoSuchProcess:
                alive[owner] = False
            except psutil.AccessDenied:
                alive[owner] = True
    return {k: {'pid': v['pid'], 'started': v['started']} for k, v in rows.items()
            if alive[(v['pid'], v['started'])]}


def refresh_connection_capacity(dsn: str) -> dict:
    """Publish observed server capacity; a staged startup setting is never live."""
    import logging
    import time
    import psycopg

    with psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                         application_name='applypilot-capacity') as connection:
        maximum, reserved, privileged = connection.execute(
            "SELECT current_setting('max_connections')::int, "
            "COALESCE(current_setting('reserved_connections', true),'0')::int, "
            "current_setting('superuser_reserved_connections')::int").fetchone()
        pending_restart = connection.execute(
            "SELECT pending_restart FROM pg_settings WHERE name='max_connections'").fetchone()[0]
        staged = None
        config_error = None
        try:
            row = connection.execute(
                "SELECT setting,error FROM pg_file_settings WHERE name='max_connections' ORDER BY seqno DESC LIMIT 1").fetchone()
            if row is not None:
                staged, config_error = int(row[0]), row[1]
        except (psycopg.errors.InsufficientPrivilege, ValueError, TypeError):
            pass
    capacity = maximum - reserved - privileged
    snapshot = {'running_max_connections': maximum, 'configured_max_connections': staged,
                'reserved_connections': reserved + privileged,
                'server_capacity': capacity,
                'restart_required': bool(pending_restart or (staged is not None and staged != maximum)),
                'configuration_error': config_error, 'observed_at': time.time()}
    previous = _capacity_snapshots.get(dsn)
    _capacity_snapshots[dsn] = snapshot
    if snapshot['restart_required'] and (previous is None or
            previous['configured_max_connections'] != staged or previous['running_max_connections'] != maximum):
        logging.getLogger(__name__).warning(
            'PostgreSQL max_connections is %s; configuration requests %s. An orderly server restart is required; '
            'live admission remains bounded by %s connections.', maximum, staged, capacity)
    return connection_capacity_status(dsn)


def connection_capacity_status(dsn: str) -> dict:
    """Return the last observed startup state and current resource admission."""
    snapshot = dict(_capacity_snapshots.get(dsn, {}))
    if snapshot:
        if os.getenv('APPLYPILOT_DATABASE_URL'):
            snapshot['capacity_owner'] = 'postgresql'
            snapshot['leased_connections'] = None
        else:
            snapshot['leased_connections'] = len(_live_leases(_budget_path(dsn)))
    return snapshot


def budget_connection_class(dsn: str, capacity: int):
    """Account for physical pool connections across local ApplyPilot processes.

    Pool demand may exceed capacity, but a physical connection is opened only
    after obtaining a shared lease. Pool waiters retain their existing bounded
    timeout and scheduled retry behavior. Dead-process leases are reclaimed.
    """
    import time
    from uuid import uuid4

    from filelock import FileLock
    import psutil
    import psycopg

    from .atomic_io import atomic_record
    path = _budget_path(dsn)
    lock_path = str(path) + '.lock'
    pending_releases = _pending_connection_releases.setdefault(str(path), set())
    process_owner = {'pid': os.getpid(), 'started': psutil.Process().create_time()}

    def update(token=None, *, acquire=False):
        with FileLock(lock_path, timeout=5):
            rows = _live_leases(path)
            releasing = tuple(pending_releases)
            for released in releasing:
                rows.pop(released, None)
            available = True
            if acquire:
                current = _capacity_snapshots.get(dsn, {}).get('server_capacity', capacity)
                if len(rows) >= current:
                    available = False
                else:
                    rows[token] = dict(process_owner)
            atomic_record(path, rows)
            pending_releases.difference_update(releasing)
            return available

    def release(token):
        pending_releases.add(token)
        try:
            update()
        except Exception:
            import logging
            logging.getLogger(__name__).warning('PostgreSQL connection closed; shared budget receipt release will retry')

    class BudgetConnection(psycopg.Connection):
        _capacity_lease = None

        @classmethod
        def connect(cls, *args, **kwargs):
            token = uuid4().hex
            deadline = time.monotonic() + 5
            try:
                while not update(token, acquire=True):
                    if time.monotonic() >= deadline:
                        raise psycopg.OperationalError('Shared PostgreSQL connection budget is busy; queued demand will retry')
                    time.sleep(.1)
                connection = super().connect(*args, **kwargs)
                connection._capacity_lease = token
                return connection
            except BaseException:
                release(token)
                raise

        def close(self):
            try:
                super().close()
            finally:
                if self.closed:
                    token, self._capacity_lease = self._capacity_lease, None
                    if token:
                        release(token)

    return BudgetConnection


def _write_config(path: Path, content: str, *, owner: Path) -> None:
    if path.is_symlink():
        raise RuntimeError('Redirected PostgreSQL configuration file')
    if path.exists() and path.read_text(encoding='utf-8') == content:
        return
    metadata = owner.stat()
    fd, temporary = tempfile.mkstemp(prefix='.applypilot-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, metadata.st_mode & 0o777)
        if os.name != 'nt':
            os.chown(temporary, metadata.st_uid, metadata.st_gid)
        replace_with_retry(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_embedded_connections(data: Path, minimum_pool: int) -> None:
    """Called under pgembed's startup lock; never restart an active server."""
    config = data / 'postgresql.conf'
    managed = data / 'applypilot-connections.conf'
    if config.is_symlink() or managed.is_symlink():
        raise RuntimeError('Redirected PostgreSQL configuration file')
    from pgembed._commands import postgres
    import subprocess

    version = (data / 'PG_VERSION').read_text().strip()
    names = ['superuser_reserved_connections']
    if int(version.split('.')[0]) >= 16:
        names.append('reserved_connections')
    reserved = sum(int(postgres(['-C', name], pgdata=data, timeout=5,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).strip())
                   for name in names)
    maximum = embedded_connection_limit(version, minimum_pool, reserved_connections=reserved)
    _write_config(managed, '# Managed by ApplyPilot; recomputed at startup.\n'
                  f'max_connections = {maximum}\n', owner=config)
    text = config.read_text(encoding='utf-8')
    include = "include_if_exists = 'applypilot-connections.conf'"
    if include not in text.splitlines():
        _write_config(config, text.rstrip() + '\n\n' + include + '\n', owner=config)

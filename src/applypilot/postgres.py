"""PostgreSQL-only persistence for every ApplyPilot-owned relational store.

pgembed is the embedded process owner. No file database is opened, imported,
migrated, or used as a fallback. Logical *.pgstore directories contain namespace
metadata only.

A logical connection is owned by one thread/task and borrows a server connection
only while executing SQL or holding a transaction/session lock. Reads outside
explicit transactions are autocommitted. Multi-statement writers retain commit /
rollback boundaries. A commit failure is NEVER replayed: its outcome may be
ambiguous. Use SQLSTATE, not exception-message matching, for retry policy.
"""
from __future__ import annotations

from collections.abc import Sequence
from contextlib import contextmanager
import atexit
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit, parse_qs
import uuid

from .postgres_sql import bind_query, identifier, statement_kind
from .postgres_capacity import pool_size
from .runtime_policy import SQL_SESSION_SETTINGS, load_policy

try:
    import psycopg
    from psycopg import Error, OperationalError, ProgrammingError, InterfaceError
    from psycopg import DatabaseError as DatabaseError, IntegrityError as IntegrityError
    from psycopg.pq import TransactionStatus
    from psycopg_pool import ConnectionPool, PoolTimeout
except ImportError as exc:
    raise ImportError('PostgreSQL support requires psycopg[binary] and psycopg-pool. Install the project dependencies.') from exc

log = logging.getLogger(__name__)
LOCK_RETRY_STATES = frozenset({'40001', '40P01', '55P03'})
CONNECTION_RETRY_STATES = frozenset({'08000', '08001', '08003', '08006', '53300', '57P03'})
_servers: dict[tuple[int, str], object] = {}
_setup_lock = threading.RLock()
_pools: dict[tuple[int, str], ConnectionPool] = {}
_pool_workers: int | None = None
_NAMES = {'main.pgstore': 'ap_main', 'learning.pgstore': 'ap_learning',
          'events.pgstore': 'ap_operations', 'sources.pgstore': 'ap_sources',
          'signals.pgstore': 'ap_signals'}


def _prepare_pool_return(raw) -> bool:
    """A pooled socket must be idle or closed, never an unfinished transaction.

    This is connection hygiene, not a database retry mechanism. In particular a
    failed or ambiguous COMMIT is never replayed. Psycopg's pool replaces closed
    connections; active/unknown protocol state cannot be repaired safely here.
    """
    if raw.closed:
        return False
    try:
        status = raw.info.transaction_status
        if status in {TransactionStatus.INTRANS, TransactionStatus.INERROR}:
            raw.rollback()
        if raw.info.transaction_status != TransactionStatus.IDLE:
            raw.close()
            return False
        # DISCARD ALL is forbidden in a transaction. The pool contract is
        # autocommit=True, including a connection touched through .raw.
        if not raw.autocommit:
            raw.autocommit = True
        return True
    except Exception as exc:
        log.warning("Discarding unusable PostgreSQL connection (%s)", type(exc).__name__)
        raw.close()
        return False


def _reset_pooled_connection(raw):
    if not _prepare_pool_return(raw):
        return
    try:
        raw.execute('DISCARD ALL')
    except BaseException:
        raw.close()
        raise


def configure_worker_pool(workers: int) -> None:
    """Size PostgreSQL and existing process pools for the configured workers."""
    global _pool_workers
    requested = pool_size(workers)
    with _setup_lock:
        dsn = server_uri(minimum_pool=requested)
        from .postgres_capacity import refresh_connection_capacity
        maximum = min(requested, refresh_connection_capacity(dsn)['server_capacity'])
        _pool_workers = workers
        for (pid, _), pool in _pools.items():
            if pid == os.getpid() and pool.max_size != maximum:
                pool.resize(min_size=1, max_size=maximum)
        _connection_pool(dsn)


def _connection_pool(dsn: str) -> ConnectionPool:
    key = os.getpid(), dsn
    with _setup_lock:
        if key not in _pools:
            from .postgres_capacity import budget_connection_class, refresh_connection_capacity
            requested = pool_size(_pool_workers)
            status = refresh_connection_capacity(dsn)
            capacity = status['server_capacity']
            maximum = min(requested, capacity)
            policy = load_policy().database
            pool = ConnectionPool(
                dsn, min_size=1, max_size=maximum, open=False,
                connection_class=(psycopg.Connection if os.getenv("APPLYPILOT_DATABASE_URL")
                                  else budget_connection_class(dsn, capacity)),
                kwargs={'autocommit': True, 'cursor_factory': psycopg.ClientCursor,
                        'connect_timeout': policy.connect_seconds, 'prepare_threshold': None,
                        'application_name': 'applypilot:pool:' + str(os.getpid()),
                        'row_factory': _rows},
                check=ConnectionPool.check_connection, reset=_reset_pooled_connection,
                timeout=policy.pool_wait_seconds, max_idle=policy.pool_idle_seconds,
                max_lifetime=policy.pool_lifetime_seconds,
            )
            try:
                pool.open()
                pool.wait(timeout=10)
            except BaseException:
                pool.close()
                raise
            _pools[key] = pool
            log.info('PostgreSQL pool capacity: %s effective, %s requested; shared server budget: %s',
                     maximum, requested, capacity)
            # Registered after pgembed startup so leases close before the server.
            atexit.register(close_pools)
        return _pools[key]


def close_pools() -> None:
    with _setup_lock:
        for key, pool in list(_pools.items()):
            if key[0] == os.getpid():
                pool.close()
                del _pools[key]


def _after_fork() -> None:
    global _setup_lock, _pool_workers
    # Inherited registries remain keyed by their owning PID and are never used.
    # An inherited lock may have belonged to a thread absent from this child.
    _setup_lock = threading.RLock()
    _pool_workers = None


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def pool_stats() -> list[dict]:
    """Expose process-local pool demand without connection strings or credentials."""
    if not _setup_lock.acquire(blocking=False):
        return []
    try:
        return [pool.get_stats() for (pid, _), pool in _pools.items() if pid == os.getpid()]
    finally:
        _setup_lock.release()


def capacity_status(*, refresh=False) -> dict:
    """Report running/configured server limits separately from live admission."""
    from .postgres_capacity import connection_capacity_status, refresh_connection_capacity

    dsn = server_uri()
    status = refresh_connection_capacity(dsn) if refresh else connection_capacity_status(dsn)
    return {**status, 'requested_pool_size': pool_size(_pool_workers)}


class Row(Sequence):
    """DB-API row supporting both positional access and dict(row)."""
    __slots__ = ('_names', '_values', '_index')

    def __init__(self, names, values):
        self._names = tuple(names)
        self._values = tuple(values)
        self._index = {name: i for i, name in reversed(tuple(enumerate(self._names)))}

    def __getitem__(self, key):
        return self._values[self._index[key]] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._names


def _rows(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def error_info(exc: BaseException, *, query_id: str | None = None, elapsed: float | None = None) -> dict:
    """No SQL parameters, connection credentials, employer URLs or raw inbox data."""
    state = getattr(exc, 'sqlstate', None)
    return {'exception_type': type(exc).__name__, 'sqlstate': state,
            'retryable': is_retryable(exc), 'query_id': query_id,
            'duration_seconds': round(elapsed, 4) if elapsed is not None else None,
            'constraint': getattr(getattr(exc, 'diag', None), 'constraint_name', None)}


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, PoolTimeout) or getattr(exc, 'sqlstate', None) in LOCK_RETRY_STATES | CONNECTION_RETRY_STATES


def _path(value) -> tuple[Path, bool]:
    from .config import DB_PATH
    text = str(value or DB_PATH)
    readonly = False
    if text.startswith('file:'):
        parsed = urlsplit(text)
        readonly = parse_qs(parsed.query).get('mode', [''])[0] == 'ro'
        text = unquote(parsed.path)
        if os.name == 'nt' and re.match(r'^/[A-Za-z]:', text):
            text = text[1:]
    if text == ':memory:':
        raise ValueError('Use postgres.temporary_store() for isolated PostgreSQL tests/previews')
    unresolved = Path(text).expanduser().absolute()
    if any(parent.is_symlink() for parent in (unresolved, *unresolved.parents)):
        raise ValueError('Redirected logical database path')
    path = unresolved.resolve()
    if path.suffix.lower() in {'.db', '.sqlite', '.sqlite3'} or path.is_file():
        raise ValueError('Legacy database files are not accepted. Use a fresh *.pgstore namespace.')
    return path, readonly


def namespace(path: Path) -> str:
    from .config import APP_DIR
    path = Path(path).expanduser().resolve()
    if path.name in _NAMES and path.is_relative_to(APP_DIR):
        return _NAMES[path.name]
    # Test/preview stores must not share data merely because their basenames match.
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:20]
    return 'ap_isolated_' + digest


def _start_embedded(data: Path, minimum_pool: int, *, attach_only=False):
    try:
        import pgembed
        from pgembed import postgres_server
        from pgembed.utils import PostmasterInfo
        from .pgembed_compat import HandlePids, ManagedPostgresServer, _AttachLock
    except ImportError as exc:
        raise RuntimeError('pgembed is not installed; install the project dependencies. '
                           'There is no database fallback.') from exc

    # Install before construction: pgembed registers its atexit callback before
    # its first PID write. Keep its process lock and last-owner cleanup intact.
    postgres_server.DiskList = HandlePids
    try:
        # pgembed 0.2.0 caches the handle before startup completes. Resume that
# handle after a failure so its cleanup registration and owner stay intact.
        server = pgembed.PostgresServer._instances.get(data)
        try:
            if server is None:
                data.mkdir(exist_ok=True)
                if not attach_only:
                    # First provisor for this process; runs under the inbound
                    # provider lock, before pg_ctl takes the log handle.
                    from .pgembed_compat import ensure_timezone_files, rotate_postgres_log
                    ensure_timezone_files()
                    rotate_postgres_log(data)
                server = ManagedPostgresServer(data, minimum_pool=minimum_pool, cleanup_mode='stop',
                                               attach_only=attach_only)
            else:
                if attach_only and not isinstance(server._lock, _AttachLock):
                    server._lock = _AttachLock(server._lock)
                with server._lock:
                    server.minimum_pool = minimum_pool
                    server.attach_only = attach_only
                    server.ensure_pgdata_inited()
                    server.ensure_postgres_running()
        except (subprocess.TimeoutExpired, AssertionError):
            log.warning('Initial PostgreSQL startup wait expired; waiting for the same cluster to finish recovery')
            server = pgembed.PostgresServer._instances.get(data)
            if server is None:
                raise

        # pg_ctl's ten-second timeout does not stop PostgreSQL crash recovery.
        # Wait for the same cluster to become ready before publishing its URI.
        with server._lock:
            if attach_only:
                server.ensure_postgres_running()
                server.global_process_id_list.get_and_add(os.getpid())
                return server
            deadline = time.monotonic() + 60
            while True:
                try:
                    info = PostmasterInfo.read_from_pgdata(data)
                except (OSError, ValueError, AssertionError):
                    info = None  # postmaster.pid may still be getting rewritten.
                if (info is not None and info.pgdata.resolve() == data
                        and info.is_running() and info.status == 'ready'):
                    server._postmaster_info = info
                    server.get_uri(database='postgres')
                    # A timed-out constructor never reached owner registration.
                    server.global_process_id_list.get_and_add(os.getpid())
                    log.info('PostgreSQL is ready; startup/crash recovery completed')
                    return server
                if time.monotonic() >= deadline:
                    raise RuntimeError(f'Embedded PostgreSQL did not become ready within 60 seconds. '
                                       f'See {data / "log"}.')
                time.sleep(.25)
    except (OSError, ValueError, AssertionError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f'Embedded PostgreSQL startup failed ({type(exc).__name__}). '
                           f'See {data / "log"}.') from exc


def server_uri(*, minimum_pool: int | None = None) -> str:
    from .config import APP_DIR
    external = os.getenv('APPLYPILOT_DATABASE_URL', '').strip()
    if external:
        if not external.startswith(('postgresql://', 'postgres://')):
            raise ValueError('APPLYPILOT_DATABASE_URL must be a PostgreSQL URI')
        return external
    data = Path(os.getenv('APPLYPILOT_PGDATA', str(APP_DIR / 'postgres' / 'cluster-v1'))).expanduser().resolve()
    key = os.getpid(), str(data)
    with _setup_lock:
        if key not in _servers:
            data.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            from filelock import FileLock
            from .pgembed_compat import ready_postmaster
            provider_file = data.parent / (data.name + '.provider')
            if provider_file.is_symlink():
                raise RuntimeError('Redirected PostgreSQL provider marker')
            required = minimum_pool if minimum_pool is not None else pool_size(_pool_workers)
            if (provider_file.exists() and provider_file.read_text().strip() == 'pgembed'
                    and ready_postmaster(data) is not None):
                # Short-lived browser helpers still register a pgembed owner, but
                # never queue behind cluster initialization or capacity calculation.
                _servers[key] = _start_embedded(data, required, attach_only=True)
            else:
                # Cross-process ownership marker and first startup are one operation.
                # A second provider must never race a different binary onto this PGDATA.
                with FileLock(str(provider_file) + '.lock', timeout=120):
                    if provider_file.is_symlink():
                        raise RuntimeError('Redirected PostgreSQL provider marker')
                    if provider_file.exists() and provider_file.read_text().strip() != 'pgembed':
                        raise RuntimeError('Do not switch embedded providers on an existing cluster. '
                                           'Use a new APPLYPILOT_PGDATA or a PostgreSQL dump/restore.')
                    server = _start_embedded(data, required)
                    provider_file.write_text('pgembed\n', encoding='utf-8')
                    provider_file.chmod(0o600)
                    _servers[key] = server
        if minimum_pool is not None:
            # Explicit CLI worker counts can arrive after an initial DB read.
            # Stage the new server capacity without stopping any existing owner.
            from .postgres_capacity import configure_embedded_connections
            server = _servers[key]
            with server._lock:
                configure_embedded_connections(data, minimum_pool)
        return _servers[key].get_uri(database='postgres')


class Cursor:
    def __init__(self, connection: 'Connection'):
        self.connection = connection
        self._description = None
        self._rowcount = -1
        self._result = None
        self._position = 0
        self._closed = False
        self._batch_count = None

    def _assert_open(self):
        self.connection._assert_owner()
        if self._closed:
            raise InterfaceError('The PostgreSQL cursor has been closed')

    @property
    def description(self):
        return self._description

    @property
    def rowcount(self):
        return self._batch_count if self._batch_count is not None else self._rowcount

    def execute(self, query, parameters=()):
        self._assert_open()
        self._batch_count = None
        self._description, self._rowcount, self._result = None, -1, None
        self._position = 0
        self.connection._execute(self, query, parameters)
        return self

    def executemany(self, query, parameters):
        # Each batch participates in the caller's transaction; no per-row commit.
        count = 0
        for values in parameters:
            self.execute(query, values)
            count += max(0, self._rowcount)
        self._batch_count = count
        return self

    def executescript(self, script):
        return self.execute(script)

    def _assert_result(self):
        self._assert_open()
        if self._result is None:
            raise ProgrammingError('The last operation did not produce a result')

    def fetchone(self):
        self._assert_result()
        if self._position >= len(self._result):
            return None
        row = self._result[self._position]
        self._position += 1
        return row

    def fetchmany(self, size=1):
        self._assert_result()
        if size < 0:
            raise ValueError('fetchmany size must be nonnegative')
        rows = self._result[self._position:self._position + size]
        self._position += len(rows)
        return rows

    def fetchall(self):
        self._assert_result()
        rows = self._result[self._position:]
        self._position = len(self._result)
        return rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row

    def close(self):
        self.connection._assert_owner()
        self._closed = True
        self._result = None


class Connection:
    def __init__(self, raw, path: Path, schema: str, *, isolation_level='DEFERRED', readonly=False,
                 pool=None, timeout=None):
        self._raw, self.path, self.schema = raw, path, schema
        self._pool = pool
        self._closed = False
        self.isolation_level = isolation_level
        self.readonly = readonly
        self.row_factory = Row
        self.total_changes = 0
        self._contexts: list[str | None] = []
        self._transaction_started: float | None = None
        self._transaction_dirty = False
        self._transaction_lost = False
        self._lease_timeout = load_policy().database.pool_wait_seconds if timeout is None else max(.001, float(timeout))
        self._session_locks = 0
        self._owner_thread = threading.get_ident()
        self._owner_process = os.getpid()

    def _assert_owner(self):
        if self._owner_process != os.getpid() or self._owner_thread != threading.get_ident():
            raise InterfaceError("A PostgreSQL connection cannot cross ApplyPilot worker processes or threads")
        if self._closed:
            raise InterfaceError('The PostgreSQL connection has been closed')

    @property
    def raw(self):
        self._assert_owner()
        if self._transaction_lost:
            raise OperationalError('The PostgreSQL transaction connection was lost; rollback before using this handle again')
        if self._raw is None:
            if self._pool is None:
                raise InterfaceError('The PostgreSQL connection has no pool')
            self._raw = self._pool.getconn(timeout=self._lease_timeout)
            try:
                self._configure_lease()
            except BaseException:
                self._release_lease(discard=True)
                raise
        return self._raw

    def _configure_lease(self):
        self._raw.row_factory = _rows
        # Timezone data is provisioned from the tzdata wheel, so numeric UTC
        # resolves without the pgembed install's former missing-directory probe.
        self._raw.execute('SET TIME ZONE 0')
        self._raw.execute("""SELECT
            set_config('application_name', %s, false),
            set_config('search_path', %s, false),
            set_config('statement_timeout', %s, false),
            set_config('lock_timeout', %s, false),
            set_config('idle_in_transaction_session_timeout', %s, false),
            set_config('default_transaction_read_only', %s, false)""",
            ('applypilot:' + self.schema + ':' + str(os.getpid()),
             identifier(self.schema) + ', pg_catalog', SQL_SESSION_SETTINGS['statement_timeout'],
             SQL_SESSION_SETTINGS['lock_timeout'], SQL_SESSION_SETTINGS['idle_in_transaction_session_timeout'],
             'on' if self.readonly else 'off'))

    def _release_lease(self, *, discard=False):
        raw, self._raw = self._raw, None
        if raw is None:
            return
        try:
            if discard or self._pool is None:
                raw.close()
            else:
                _prepare_pool_return(raw)
        finally:
            if self._pool is not None:
                # Closed sockets are returned too: the pool must release its
                # checkout accounting and replace them rather than leak slots.
                self._pool.putconn(raw)

    def _release_if_idle(self):
        if self._raw is None or self._pool is None:
            return
        status = self._raw.info.transaction_status
        if status == TransactionStatus.UNKNOWN:
            self._transaction_lost = self._transaction_started is not None or bool(self._contexts or self._session_locks)
            self._release_lease(discard=True)
        elif not self._session_locks and not self._contexts and status == TransactionStatus.IDLE:
            self._release_lease()

    @property
    def closed(self):
        return (self._closed or (self._raw is not None and self._raw.closed)
                or (self._raw is None and self._pool is not None and self._pool.closed))

    @property
    def in_transaction(self):
        self._assert_owner()
        return self._raw is not None and self._raw.info.transaction_status in {
            TransactionStatus.INTRANS, TransactionStatus.INERROR}

    def _execute(self, cursor, query, parameters=()):
        self._assert_owner()
        sql, count = bind_query(query)
        parameters = tuple(parameters or ())
        if count != len(parameters):
            raise ProgrammingError(f'Expected {count} SQL parameters, received {len(parameters)}')
        kind = statement_kind(query).rstrip(';')
        write = kind in {'INSERT', 'UPDATE', 'DELETE', 'MERGE'} or (
            kind == 'WITH' and re.search(r'\b(INSERT|UPDATE|DELETE)\b', query, re.I))
        started = time.monotonic()
        query_id = hashlib.sha256(query.encode()).hexdigest()[:16]
        try:
            raw = self.raw
            if kind in {'COMMIT', 'END'} and raw.info.transaction_status == TransactionStatus.INERROR:
                self._transaction_dirty = False
            if self.in_transaction and self._transaction_started is None:
                self._transaction_started = time.monotonic()
            if write and self.isolation_level is not None and not self.in_transaction:
                raw.execute('BEGIN')
                self._transaction_started = time.monotonic()
            elif kind == 'BEGIN':
                self._transaction_started = time.monotonic()
            with raw.cursor(row_factory=_rows if self.row_factory is Row else psycopg.rows.tuple_row) as result:
                # Results are detached before returning the physical connection.
                # Callers may keep cursors across model calls or other SQL work.
                result.execute(sql if count else query, parameters if count else None, prepare=False)
                cursor._description, cursor._rowcount = result.description, result.rowcount
                cursor._result = result.fetchall() if result.description is not None else None
            if write and cursor._rowcount > 0:
                self.total_changes += cursor._rowcount
                self._transaction_dirty = True
            if kind in {'COMMIT', 'END'} or (write and not self.in_transaction):
                self._notify_commit()
            if kind in {'COMMIT', 'END', 'ROLLBACK'} and not self.in_transaction:
                self._transaction_dirty = False
                self._record_duration(kind.lower())
        except Error as exc:
            diagnostic = error_info(exc, query_id=query_id, elapsed=time.monotonic() - started)
            log.warning('postgres statement failed %s', diagnostic,
                        extra={'ops_exception': exc, 'ops_diagnostics': diagnostic})
            raise
        finally:
            self._release_if_idle()

    def execute(self, query, parameters=()):
        return self.cursor().execute(query, parameters)

    def executemany(self, query, parameters):
        return self.cursor().executemany(query, parameters)

    def executescript(self, script):
        return self.cursor().executescript(script)

    def cursor(self, factory=Cursor):
        self._assert_owner()
        return factory(self)

    def _notify_commit(self):
        dirty, self._transaction_dirty = self._transaction_dirty, False
        if not dirty or self.readonly or self._raw is None:
            return
        try:
            # The business transaction is already committed. Bypass the wrapper
            # so this coalescible wake hint cannot recurse or become a write.
            self._raw.execute("SELECT pg_notify('applypilot_changed', %s)", (self.schema,))
        except Exception:
            # Never return a failed notification connection to business work.
            try:
                self._release_lease(discard=True)
            except Exception:
                pass

    def commit(self):
        self._assert_owner()
        try:
            if self._transaction_lost:
                raise OperationalError('The PostgreSQL transaction connection was lost; commit cannot be replayed')
            if self._raw is not None:
                if self.in_transaction and self._transaction_started is None:
                    self._transaction_started = time.monotonic()
                if self._raw.info.transaction_status == TransactionStatus.INERROR:
                    self._transaction_dirty = False
                self._raw.commit()
                self._notify_commit()
        finally:
            self._release_if_idle()
            self._record_duration('commit')

    def rollback(self):
        self._assert_owner()
        try:
            if self._raw is not None:
                self._raw.rollback()
        finally:
            self._release_if_idle()
            self._transaction_lost = False
            self._transaction_dirty = False
            self._record_duration('rollback')

    def _record_duration(self, outcome):
        if self._transaction_started is not None:
            elapsed = time.monotonic() - self._transaction_started
            if elapsed >= 2:
                log.warning(f'PostgreSQL transaction lifetime {elapsed:.3f}s in {self.schema}; ended with {outcome}',
                            extra={'ops_diagnostics': {'schema': self.schema,
                                   'duration_seconds': round(elapsed, 3), 'phase': outcome}})
        self._transaction_started = None

    def close(self):
        if self._closed:
            return
        self._assert_owner()
        try:
            if self._raw is not None:
                try:
                    self._raw.rollback()
                except BaseException:
                    self._release_lease(discard=True)
                    raise
                self._release_lease(discard=bool(self._session_locks))
        finally:
            self._transaction_dirty = False
            self._transaction_lost = False
            self._record_duration('close_rollback')
            self._closed = True

    def __enter__(self):
        name = 'ap_context_' + uuid.uuid4().hex if self.in_transaction else None
        self.execute('SAVEPOINT ' + name if name else 'BEGIN')
        self._contexts.append(name)
        return self

    def __exit__(self, exc_type, exc, tb):
        name = self._contexts.pop()
        if name:
            if exc_type:
                self.execute('ROLLBACK TO SAVEPOINT ' + name)
            self.execute('RELEASE SAVEPOINT ' + name)
        elif exc_type:
            self.rollback()
        else:
            self.commit()
        return False

    def table_info(self, table):
        table = str(table).strip('"')
        return self.execute('''SELECT a.attnum-1 AS cid, a.attname AS name,
            format_type(a.atttypid,a.atttypmod) AS type, a.attnotnull::integer AS "notnull",
            pg_get_expr(d.adbin,d.adrelid) AS dflt_value,
            CASE WHEN a.attnum=ANY(i.indkey) THEN 1 ELSE 0 END AS pk
            FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum
            LEFT JOIN pg_index i ON i.indrelid=c.oid AND i.indisprimary
            WHERE n.nspname=current_schema() AND c.relname=? AND a.attnum>0 AND NOT a.attisdropped
            ORDER BY a.attnum''', (table,))

    def advisory_lock(self, resource, *, transaction=True, shared=False):
        from applypilot.orchestration.metrics import meter

        digest = int.from_bytes(hashlib.sha256((self.schema + ':' + str(resource)).encode()).digest()[:8],
                                'big', signed=True)
        function = 'pg_advisory_xact_lock' if transaction else 'pg_advisory_lock'
        if shared:
            function += '_shared'
        if transaction and not self.in_transaction:
            raise RuntimeError('A transaction-scoped lock requires an active transaction')
        if not transaction:
            self._session_locks += 1
        try:
            with meter('advisory_lock_wait'):
                self.execute(f'SELECT {function}(?)', (digest,))
        except BaseException:
            if not transaction:
                self._session_locks -= 1
                self._release_if_idle()
            raise
        return digest

    @contextmanager
    def schema_lock(self):
        """Serialize DDL across processes without serializing independent writes."""
        digest = self.advisory_lock('schema-v1', transaction=False)
        try:
            yield self
        finally:
            try:
                if self._raw is not None and self._raw.info.transaction_status == TransactionStatus.INERROR:
                    self.rollback()
                self.execute('SELECT pg_advisory_unlock(?)', (digest,))
            finally:
                self._session_locks -= 1
                self._release_if_idle()


def initialize_store(path=None) -> Path:
    """Explicit deployment/bootstrap of one business namespace.

    Ordinary connections never execute DDL. The advisory lock here serializes
    schema installation only; it is not a task lease or worker ownership record.
    Existing data is retained, and a failed installation is rolled back.
    """
    path, readonly = _path(path)
    if readonly:
        raise ValueError("Cannot initialize a read-only PostgreSQL store")
    schema = namespace(path)
    dsn = server_uri()
    connection = Connection(None, path, schema, isolation_level=None,
                            readonly=False, pool=_connection_pool(dsn))
    try:
        raw = connection.raw
        lock = int.from_bytes(hashlib.sha256(('applypilot:' + schema).encode()).digest()[:8],
                              'big', signed=True)
        raw.execute('SELECT pg_advisory_lock(%s)', (lock,))
        try:
            with raw.transaction():
                raw.execute('CREATE SCHEMA IF NOT EXISTS ' + identifier(schema))
                raw.execute('SET LOCAL search_path TO ' + identifier(schema) + ', pg_catalog')
                resource = Path(__file__).parent / 'config' / 'postgres' / 'functions.sql'
                raw.execute(resource.read_text(encoding='utf-8'), prepare=False)
        finally:
            # Never return a session with an unaccounted migration lock.
            try:
                raw.execute('SELECT pg_advisory_unlock(%s)', (lock,))
            except BaseException:
                raw.close()
                raise
    finally:
        connection.close()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    from applypilot.atomic_io import atomic_record
    atomic_record(path / 'namespace.json', {'format': 1, 'schema': schema})
    return path


def connect(path=None, *, timeout=None, isolation_level='DEFERRED', uri=False, factory=None,
            readonly=False, **kwargs) -> Connection:
    """Open an already deployed namespace. This function performs no DDL or writes."""
    if kwargs:
        raise TypeError('Unsupported PostgreSQL connection options: ' + ', '.join(kwargs))
    path, uri_readonly = _path(path)
    readonly = readonly or uri_readonly
    schema = namespace(path)
    dsn = server_uri()
    connection = Connection(None, path, schema, isolation_level=isolation_level, readonly=readonly,
                            pool=_connection_pool(dsn), timeout=timeout)
    try:
        exists = connection.raw.execute(
            'SELECT 1 FROM pg_namespace WHERE nspname=%s', (schema,)).fetchone()
        if not exists:
            raise OperationalError('PostgreSQL store is not initialized: ' + schema +
                                   '. Run applypilot temporal init-data at deployment before starting workers.')
        connection._release_if_idle()
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def temporary_store(source: Connection | None = None):
    """Isolated disposable PostgreSQL namespace for dry-run previews.

    Copies use PostgreSQL's native LIKE/COPY-independent INSERT SELECT, never
    database files. The namespace is dropped on exit, even after an exception.
    """
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix='applypilot-pg-preview-') as folder:
        path = initialize_store(Path(folder) / 'preview.pgstore')
        target = connect(path, isolation_level=None)
        try:
            if source is not None:
                with target:
                    target.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                    for row in source.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema() ORDER BY tablename"):
                        table = identifier(row[0])
                        origin = identifier(source.schema) + '.' + table
                        target.execute(f'CREATE TABLE {table} (LIKE {origin} INCLUDING ALL)')
                        target.execute(f'INSERT INTO {table} OVERRIDING SYSTEM VALUE SELECT * FROM {origin}')
                        for column in target.execute('''SELECT column_name FROM information_schema.columns
                                WHERE table_schema=current_schema() AND table_name=? AND is_identity='YES' ''', (row[0],)).fetchall():
                            sequence = target.execute('SELECT pg_get_serial_sequence(?,?)', (row[0],column[0])).fetchone()[0]
                            if sequence:
                                maximum = target.execute(f'SELECT MAX({identifier(column[0])}) FROM {table}').fetchone()[0]
                                target.execute('SELECT setval(?::regclass,?,?)', (sequence, maximum or 1, maximum is not None))
            yield target
        finally:
            target.rollback()
            target.raw.execute('DROP SCHEMA IF EXISTS ' + identifier(target.schema) + ' CASCADE')
            target.close()


def _binary(name: str) -> str:
    import shutil
    if os.getenv('APPLYPILOT_DATABASE_URL'):
        found = shutil.which(name)
        if not found:
            raise RuntimeError(name + ' must be on PATH when using an external PostgreSQL server')
        return found
    from pgembed._commands import POSTGRES_BIN_PATH
    suffix = '.exe' if os.name == 'nt' else ''
    return str(Path(POSTGRES_BIN_PATH) / (name + suffix))


def dump_store(path, destination: Path) -> None:
    dump_stores([path], destination)


def dump_stores(paths, destination: Path, *, compress=True) -> None:
    """Capture all selected schemas in one pg_dump transaction/snapshot."""
    schemas = sorted({namespace(_path(path)[0]) for path in paths})
    if not schemas:
        raise ValueError('Select at least one PostgreSQL store to back up')
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.with_name(destination.name + '.partial-' + uuid.uuid4().hex)
    # Passwords are passed via environment, never command-line arguments or logs.
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    info = conninfo_to_dict(server_uri())
    env = os.environ.copy()
    if 'password' in info:
        env['PGPASSWORD'] = info.pop('password')
    try:
        subprocess.run([_binary('pg_dump'), '--dbname', make_conninfo(**info), '--format=custom',
                        '--no-owner', '--no-privileges', '--strict-names',
                        *([] if compress else ['--compress=0']),
                        *['--schema=' + schema for schema in schemas],
                        '--file', str(temporary)], check=True, env=env, capture_output=True, timeout=300)
        temporary.chmod(0o600)
        temporary.replace(destination)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        from applypilot.operations.runtime import stderr_details
        exc.details = stderr_details(exc.stderr or b'', secrets=env)
        exc.stderr = exc.details['stderr']
        if isinstance(exc, subprocess.TimeoutExpired):
            raise
        raise RuntimeError('PostgreSQL backup failed (exit ' + str(exc.returncode) + ')') from exc
    finally:
        temporary.unlink(missing_ok=True)


def validate_dump_schemas(path: Path, *, expected_schemas=None, allowed_schemas=None) -> tuple[str, ...]:
    """Validate the namespace set before restoring an authenticated archive.

    This is a structural boundary, NOT proof that an archive's SQL is benign.
    Only owner-trusted authenticated backups may be restored.
    """
    try:
        result = subprocess.run([_binary('pg_restore'), '--list', str(path)], check=True,
                                capture_output=True, text=True, timeout=30)
    except subprocess.CalledProcessError as exc:
        raise ValueError('Invalid PostgreSQL backup archive') from exc
    schemas = set(re.findall(r'^\d+;\s+\d+\s+\d+\s+SCHEMA\s+-\s+(\S+)', result.stdout, re.M))
    if not schemas:
        raise ValueError('A backup must contain at least one application namespace')
    if any(not re.fullmatch(r'ap_(?:main|learning|operations|sources|signals|isolated_[0-9a-f]{20})', schema)
           for schema in schemas):
        raise ValueError('Backup namespace is not an ApplyPilot store')
    if expected_schemas is not None and schemas != set(expected_schemas):
        raise ValueError('Backup namespaces do not match their declared stores')
    if allowed_schemas is not None and not schemas <= set(allowed_schemas):
        raise ValueError('Backup contains a namespace outside the selected workspace')
    return tuple(sorted(schemas))


def validate_dump(path: Path, *, expected_schema: str | None = None) -> str:
    """Accept a legacy archive containing exactly one application namespace."""
    schemas = validate_dump_schemas(path, expected_schemas=[expected_schema] if expected_schema is not None else None)
    if len(schemas) != 1:
        raise ValueError('A backup must contain exactly one application namespace')
    return schemas[0]


def restore_dump(path: Path, *, clean=False, expected_schema: str | None = None) -> None:
    """Restore only an owner-confirmed backup after stopping all application workers."""
    schema = validate_dump(path, expected_schema=expected_schema)
    restore_dump_schemas(path, clean=clean, expected_schemas=[schema])


def restore_dump_schemas(path: Path, *, clean=False, expected_schemas) -> None:
    """Restore the entire selected namespace set in one transaction."""
    schemas = validate_dump_schemas(path, expected_schemas=expected_schemas)
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    info = conninfo_to_dict(server_uri())
    env = os.environ.copy()
    if 'password' in info:
        env['PGPASSWORD'] = info.pop('password')
    args = [_binary('pg_restore'), '--dbname', make_conninfo(**info), '--single-transaction',
            '--exit-on-error', '--no-owner', '--no-privileges',
            *['--schema=' + schema for schema in schemas]]
    if clean:
        args += ['--clean', '--if-exists']
    try:
        subprocess.run(args + [str(path)], check=True, env=env, capture_output=True, timeout=300)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        from applypilot.operations.runtime import stderr_details
        exc.details = stderr_details(exc.stderr or b'', secrets=env)
        exc.stderr = exc.details['stderr']
        if isinstance(exc, subprocess.TimeoutExpired):
            raise
        raise RuntimeError('PostgreSQL restore failed (exit ' + str(exc.returncode) + ')') from exc

# Named stores are deliberately separate schemas within one embedded server.
STORE_PATHS = ('main.pgstore', 'learning/learning.pgstore',
               'operations/events.pgstore', 'free_sources/sources.pgstore', 'signals.pgstore')


def store_size(path) -> int:
    from contextlib import closing
    with closing(connect(path, readonly=True, isolation_level=None)) as conn:
        return int(conn.execute('''SELECT coalesce(sum(pg_total_relation_size(c.oid)),0)
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relkind IN ('r','m')''').fetchone()[0])


def require_idle() -> None:
    """Refuse a destructive operation while any other ApplyPilot DB owner is connected."""
    close_pools()
    with psycopg.connect(server_uri(), autocommit=True, application_name='applypilot-admin') as raw:
        active = raw.execute("SELECT count(*) FROM pg_stat_activity WHERE pid<>pg_backend_pid() "
                             "AND application_name LIKE 'applypilot:%'").fetchone()[0]
        if active:
            raise RuntimeError(f'Stop all ApplyPilot workers and dashboards first ({active} database owners connected).')


def drop_store(path) -> None:
    """Administrative namespace drop. Call only after owned stop/confirmation."""
    path, _ = _path(path)
    with psycopg.connect(server_uri(), autocommit=True, application_name='applypilot-admin') as raw:
        raw.execute('DROP SCHEMA IF EXISTS ' + identifier(namespace(path)) + ' CASCADE')

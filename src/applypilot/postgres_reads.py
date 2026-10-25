"""Read-only PostgreSQL connections using the shared session policy."""
from . import postgres

ReadConnection = postgres.Connection
ReadCursor = postgres.Cursor


def connect_readonly(path, *, timeout=1.5) -> ReadConnection:
    return postgres.connect(path, timeout=timeout, readonly=True, isolation_level=None)

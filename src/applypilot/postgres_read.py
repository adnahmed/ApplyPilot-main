"""Consistent read snapshots; no schema writes from diagnostic requests."""
from contextlib import contextmanager
from .postgres_reads import ReadConnection, ReadCursor, connect_readonly


@contextmanager
def read_snapshot(path, *, timeout=1.5):
    conn = connect_readonly(path, timeout=timeout)
    try:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        yield conn
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()

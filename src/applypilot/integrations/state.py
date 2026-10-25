"""Private evidence cache and research history; no scheduling state or hot-path DDL."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from applypilot import postgres
import time

from .types import stable_id


class SourceState:
    def __init__(self, path: Path | str | None = None):
        if path is None:
            from applypilot.config import APP_DIR
            path = APP_DIR / "free_sources" / "sources.pgstore"
        self.path = Path(path)

    @contextmanager
    def connection(self):
        """Only business transactions. Initialization is an explicit deployment step."""
        con = postgres.connect(self.path, timeout=15)
        try:
            with con:
                yield con
        finally:
            con.close()

    def initialize(self):
        """Create cache/report schemas once at deployment, preserving old records."""
        postgres.initialize_store(self.path)
        with self.connection() as con:
            with con.schema_lock():
                con.executescript("""
                    CREATE TABLE IF NOT EXISTS cache (
                        key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL);
                    CREATE TABLE IF NOT EXISTS reports (
                        id TEXT PRIMARY KEY, target TEXT NOT NULL, payload TEXT NOT NULL, created DOUBLE PRECISION NOT NULL);
                    CREATE INDEX IF NOT EXISTS report_target ON reports(target, created);
                """)

    def retire_coordination(self):
        """Explicitly archive old scheduler tables without dropping their records.

        Ordinary connections never invoke this. Old metadata is not read by the
        new cache/report API and therefore cannot control a provider request.
        """
        archived = []
        with self.connection() as con:
            with con.schema_lock():
                for table in ('gates', 'slots'):
                    if con.execute('SELECT to_regclass(?)', (table,)).fetchone()[0] is None:
                        continue
                    archive = 'retired_source_' + table
                    if con.execute('SELECT to_regclass(?)', (archive,)).fetchone()[0] is not None:
                        raise ValueError('Existing coordination archive requires explicit operator review')
                    # Both identifiers come from the fixed tuple above.
                    con.execute('ALTER TABLE ' + table + ' RENAME TO ' + archive)
                    archived.append(archive)
        return archived

    def get(self, key):
        with self.connection() as con:
            row = con.execute("SELECT payload FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, key, payload, ttl=3600):
        with self.connection() as con:
            con.execute('INSERT INTO cache VALUES(?,?,?) ON CONFLICT (key) DO UPDATE SET payload=excluded.payload, expires=excluded.expires',
                        (key, json.dumps(payload, ensure_ascii=True, allow_nan=False), time.time() + ttl))


    def save_report(self, target, payload):
        key = stable_id(payload)
        with self.connection() as con:
            con.execute('INSERT INTO reports VALUES(?,?,?,?) ON CONFLICT DO NOTHING',
                        (key, target, json.dumps(payload, ensure_ascii=True, allow_nan=False), time.time()))
        return key

    def report(self, target, max_age=86400, *, complete_only=False, partial_max_age=None):
        with self.connection() as con:
            row = con.execute("SELECT payload,created FROM reports WHERE target=? AND created>? ORDER BY created DESC LIMIT 1",
                              (target, time.time() - max_age)).fetchone()
        report = json.loads(row[0]) if row else None
        if report is not None and report.get("status") != "complete":
            if complete_only or (partial_max_age is not None and row[1] <= time.time() - partial_max_age):
                return None
        return report

    def status(self):
        with self.connection() as con:
            return {"cached_items": con.execute("SELECT COUNT(*) FROM cache WHERE expires>?", (time.time(),)).fetchone()[0],
                    "reports": con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]}

    def prune(self, report_days=30):
        with self.connection() as con:
            con.execute("DELETE FROM cache WHERE expires<=?", (time.time(),))
            con.execute("DELETE FROM reports WHERE created<?", (time.time() - report_days * 86400,))

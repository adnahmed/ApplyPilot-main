"""Successful source observations: business evidence, never an executable queue.

Only a complete traversal records freshness. Temporal chooses when to scan and
passes a fixed observation cutoff; this table has no due dates, retries or owners.
DDL is installed explicitly by init-db, never while planning/publishing a page.
"""
from datetime import datetime, timezone, timedelta
import math


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_source_observations (
        context_key TEXT NOT NULL, source_key TEXT NOT NULL,
        last_complete_at TIMESTAMPTZ NOT NULL,
        receipt_id TEXT NOT NULL,
        PRIMARY KEY (context_key, source_key))""")


def record_complete(conn, context_key, source_key, receipt_id):
    # Must share the page-publication transaction. Re-delivery of its receipt
    # does not touch the timestamp or pretend a new provider observation occurred.
    if not conn.in_transaction:
        raise ValueError("Record source evidence inside its publication transaction")
    conn.execute("""INSERT INTO discovery_source_observations
        (context_key,source_key,last_complete_at,receipt_id)
        VALUES(?,?,CURRENT_TIMESTAMP,?) ON CONFLICT(context_key,source_key)
        DO UPDATE SET last_complete_at=excluded.last_complete_at,
                      receipt_id=excluded.receipt_id""", (context_key, source_key, receipt_id))


def recently_observed(conn, context_key, selection_time, age_seconds):
    for value in (selection_time, age_seconds):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Source freshness requires a finite Workflow clock and horizon")
    if age_seconds <= 0:
        raise ValueError("Source freshness horizon must be positive")
    cutoff = datetime.fromtimestamp(selection_time, timezone.utc) - timedelta(seconds=age_seconds)
    # No prior complete observation means stale. A partial observation does not
    # renew an older complete observation's freshness horizon.
    # A successful empty source is fresh; no fabricated job row is needed.
    return {row["source_key"] for row in conn.execute(
        "SELECT source_key FROM discovery_source_observations "
        "WHERE context_key=? AND last_complete_at>=?", (context_key, cutoff)).fetchall()}

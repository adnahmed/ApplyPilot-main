"""Content-addressed discovery documents and bounded historical retention.

The JSON reference envelope is deliberately readable by old SQLite schemas.
Consumers resolve it at their repository boundary; queue predicates use scalar
projections and never decompress documents merely to count available work.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from datetime import datetime, timedelta, timezone
import hashlib
import json
from applypilot import postgres
import threading
import time
import zlib

CODEC = "zlib-json-v1"
_maintenance_clock = {}
_maintenance_lock = threading.Lock()
RETRIEVAL_FIELDS = frozenset({
    "origins", "observed_at", "discovered_at", "last_seen_at", "fetched_at",
    "cycle_id", "task_id", "page_key", "request_metadata",
})


@lru_cache(maxsize=32)
def _type_hints(cls):
    from typing import get_type_hints
    return get_type_hints(cls)


@dataclass(frozen=True)
class Payload:
    digest: str
    data: bytes
    raw_bytes: int

    @property
    def reference(self):
        return json.dumps({"$payload": self.digest}, separators=(",", ":"))


def prepare(value: str) -> Payload:
    """Compress outside the database write transaction."""
    raw = value.encode("utf-8")
    return Payload(hashlib.sha256(raw).hexdigest(), zlib.compress(raw, 6), len(raw))


def source_record(record: dict) -> dict:
    return {key: value for key, value in record.items() if key not in RETRIEVAL_FIELDS}


def candidate_from_json(value):
    """Restore the typed canonical projection without re-inferring its facts."""
    from dataclasses import is_dataclass, fields
    from enum import Enum
    from types import UnionType
    from typing import Union, get_args, get_origin
    from .models import JobCandidate

    def restore(annotation, raw):
        if raw is None:
            return None
        origin, args = get_origin(annotation), get_args(annotation)
        if origin in (Union, UnionType):
            annotation = next(t for t in args if t is not type(None))
            return restore(annotation, raw)
        if origin is tuple:
            return tuple(restore(args[0], item) for item in raw)
        if origin is dict:
            return {key: restore(args[1], item) for key, item in raw.items()}
        if isinstance(annotation, type) and issubclass(annotation, Enum):
            return annotation(raw)
        if isinstance(annotation, type) and is_dataclass(annotation):
            hints = _type_hints(annotation)
            return annotation(**{f.name: restore(hints[f.name], raw[f.name])
                                 for f in fields(annotation) if f.name in raw})
        return raw

    return restore(JobCandidate, json.loads(value))


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_payloads (
        hash TEXT PRIMARY KEY, codec TEXT NOT NULL, data BYTEA NOT NULL,
        raw_bytes INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_storage_meta (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS discovery_payload_references (
        owner TEXT NOT NULL, slot TEXT NOT NULL, hash TEXT NOT NULL REFERENCES discovery_payloads(hash),
        PRIMARY KEY(owner,slot))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payload_references_hash ON discovery_payload_references(hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payload_created ON discovery_payloads(created_at)")


def save(conn, payload: Payload, at: str) -> str:
    conn.execute("INSERT INTO discovery_payloads VALUES(?,?,?,?,?) ON CONFLICT(hash) DO NOTHING",
                 (payload.digest, CODEC, payload.data, payload.raw_bytes, at))
    return payload.reference


def reference(conn, owner, slot, value):
    digest = reference_hash(value)
    if digest:
        conn.execute("""INSERT INTO discovery_payload_references VALUES(?,?,?)
            ON CONFLICT(owner,slot) DO UPDATE SET hash=excluded.hash""", (owner, slot, digest))
    else:
        conn.execute("DELETE FROM discovery_payload_references WHERE owner=? AND slot=?", (owner, slot))


def release(conn, table, ids):
    conn.executemany("DELETE FROM discovery_payload_references WHERE owner=?", ((table + ":" + r[0],) for r in ids))


def reference_hash(value):
    if not isinstance(value, str) or not value.startswith('{"$payload":'):
        return None
    decoded = json.loads(value)
    return decoded.get("$payload") if set(decoded) == {"$payload"} else None


def resolve_json(conn, value):
    digest = reference_hash(value)
    if digest is None:
        return value
    row = conn.execute("SELECT codec,data,raw_bytes FROM discovery_payloads WHERE hash=?", (digest,)).fetchone()
    if row is None:
        raise ValueError("Missing discovery payload " + digest)
    if row[0] != CODEC:
        raise ValueError("Unsupported discovery payload codec " + str(row[0]))
    raw = zlib.decompress(row[1])
    if len(raw) != row[2] or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Invalid discovery payload " + digest)
    return raw.decode("utf-8")


def compact_page(page, observation_ids=()):
    """Retain replay/coverage metadata without another copy of every record."""
    result = dict(page)
    records = result.pop("records", None)
    result.update(storage_version=2, record_count=len(records) if records is not None else result.get("record_count", 0),
                  observation_ids=list(observation_ids) or result.get("observation_ids", []))
    return json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)


def retain_history(conn, *, days=7, batch_size=256):
    """One short, restartable maintenance slice; never owns its caller's commit."""
    if type(days) is not int or days < 1 or type(batch_size) is not int or batch_size < 1:
        raise ValueError("Retention days and batch size must be positive integers")
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).isoformat()
    # Application snapshots must remain attributable to their exact assessment.
    pinned = "SELECT assessment_revision FROM application_attempts WHERE assessment_revision IS NOT NULL"
    rows = conn.execute(f"""SELECT o.observation_id FROM discovery_observations o
        WHERE o.last_seen_at < ?
        AND NOT EXISTS (SELECT 1 FROM discovery_postings p WHERE p.identity_key=o.identity_key
            AND p.content_hash=o.content_hash AND p.source_revision=o.source_revision)
        AND NOT EXISTS (SELECT 1 FROM discovery_postings p WHERE p.identity_key=o.identity_key
            AND o.inference IS NULL AND o.observation_id=(SELECT o2.observation_id
                FROM discovery_observations o2 WHERE o2.identity_key=p.identity_key AND o2.inference IS NULL
                ORDER BY o2.source_revision DESC,o2.last_seen_at DESC,o2.observation_id DESC LIMIT 1))
        AND NOT EXISTS (SELECT 1 FROM discovery_assessments a WHERE a.identity_key=o.identity_key
            AND a.content_hash=o.content_hash AND a.source_revision=o.source_revision
            AND a.revision IN ({pinned}))
        ORDER BY o.last_seen_at LIMIT ?""", (cutoff, batch_size)).fetchall()
    conn.executemany("DELETE FROM discovery_observations WHERE observation_id=?", rows)
    release(conn, "observation", rows)
    removed = len(rows)
    assessments = conn.execute(f"""
        SELECT a.revision FROM discovery_assessments a WHERE a.created_at < ?
        AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.discovery_assessment_revision=a.revision)
        AND a.revision NOT IN ({pinned}) ORDER BY a.created_at LIMIT ?""", (cutoff, batch_size)).fetchall()
    conn.executemany("DELETE FROM discovery_assessments WHERE revision=?", assessments)
    release(conn, "assessment", assessments)
    blobs_removed = conn.execute("""DELETE FROM discovery_payloads WHERE hash IN (
        SELECT p.hash FROM discovery_payloads p WHERE p.created_at < ?
        AND NOT EXISTS (SELECT 1 FROM discovery_payload_references r WHERE r.hash=p.hash)
        ORDER BY p.created_at LIMIT ?)""", (cutoff, batch_size)).rowcount
    conn.execute("INSERT INTO discovery_storage_meta VALUES('retention',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps({"days": days, "checked_at": time.time(), "observations_removed": removed}),))
    return removed + len(assessments) + blobs_removed


def maintain_history(conn):
    """At most one bounded maintenance slice per database per minute."""
    key = str(conn.path)
    now = time.monotonic()
    with _maintenance_lock:
        if now - _maintenance_clock.get(key, -60) < 60:
            return 0
        _maintenance_clock[key] = now
    from .repository import transaction
    try:
        with transaction(conn):
            return retain_history(conn)
    except postgres.OperationalError as exc:
        if getattr(exc,"sqlstate",None) not in {"57014", "55P03", "40001", "40P01"}:
            raise
        return 0

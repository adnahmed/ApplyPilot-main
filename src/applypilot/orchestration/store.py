"""Immutable input references and publication receipts, not a second task queue.

There are deliberately no runnable/running/leased/next_retry/worker-owner fields.
Temporal is the only owner of execution state for the native path. Personal data
and source payloads stay in the existing main PostgreSQL store, out of histories.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
from .contracts import digest


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_inputs (
        input_id TEXT PRIMARY KEY, body_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS temporal_publication_receipts (
        receipt_id TEXT PRIMARY KEY, body_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()


def put(conn, value: dict) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False)
    key = digest(value)
    conn.execute("INSERT INTO temporal_inputs(input_id,body_json) VALUES(?,?) "
                 "ON CONFLICT(input_id) DO NOTHING", (key, body))
    # The caller commits before crossing a client -> Temporal boundary.
    # Do not commit a surrounding publication/business transaction here.
    return key


def get(conn, key: str) -> dict:
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Invalid immutable input reference")
    row = conn.execute("SELECT body_json FROM temporal_inputs WHERE input_id=?", (key,)).fetchone()
    if row is None:
        raise LookupError("Input reference is missing from this workspace database")
    value = json.loads(row[0])
    if digest(value) != key:
        raise ValueError("Immutable input content does not match its identifier")
    return value


def receipt(conn, key):
    row = conn.execute("SELECT body_json FROM temporal_publication_receipts WHERE receipt_id=?", (key,)).fetchone()
    return json.loads(row[0]) if row else None


def save_receipt(conn, key, value):
    if not conn.in_transaction:
        raise RuntimeError("A publication receipt must share its data transaction")
    conn.execute("INSERT INTO temporal_publication_receipts(receipt_id,body_json) VALUES(?,?)",
                 (key, json.dumps(value, sort_keys=True, allow_nan=False)))


def snapshot_options(options: dict) -> dict:
    from applypilot import config
    from applypilot.discovery.context import build_context, _thaw
    from applypilot.discovery.repository import _context
    from applypilot.targets.intent import load_snapshot
    raw = config.read_discovery_config()
    profile = config.load_profile()
    context = build_context(raw_config=raw, profile=profile, sources=options.get("sources"))
    return {"options": options, "searches": raw, "profile": profile,
            "resume_text": config.RESUME_PATH.read_text(encoding="utf-8") if config.RESUME_PATH.is_file() else "",
            "runtime": _thaw(context.runtime), "targets": load_snapshot(),
            "context_key": _context(context)[0]}


def context_from(value):
    from dataclasses import replace
    from applypilot.discovery.context import build_context
    context = build_context(raw_config=value["searches"], profile=value["profile"],
        runtime=value["runtime"], sources=value["options"].get("sources"))
    return replace(context, intent=replace(context.intent, target_policies_json=value["targets"]))


def work_payload(conn, reference):
    value = get(conn, reference)
    root = get(conn, value["snapshot"]) if "snapshot" in value else value
    return value, root

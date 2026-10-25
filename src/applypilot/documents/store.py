"""Immutable business records, not rendering reservations or execution state."""
from __future__ import annotations

import json
from applypilot.orchestration.contracts import digest


def initialize(conn):
    """Explicit deployment migration. Ordinary reads/writes never execute DDL."""
    conn.execute('''CREATE TABLE IF NOT EXISTS document_inputs (
        document_id TEXT PRIMARY KEY, body_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS document_artifacts (
        artifact_id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES document_inputs(document_id),
        doc_format TEXT NOT NULL, body_json TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()


def capture(conn, value):
    body = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    document_id = digest(value)
    conn.execute('INSERT INTO document_inputs(document_id,body_json) VALUES(?,?) '
                 'ON CONFLICT(document_id) DO NOTHING', (document_id, body))
    return document_id


def read(conn, document_id):
    row = conn.execute('SELECT body_json FROM document_inputs WHERE document_id=?', (document_id,)).fetchone()
    if row is None:
        raise LookupError('Document input is not in this workspace')
    value = json.loads(row[0])
    if digest(value) != document_id:
        raise ValueError('Document input identity does not match its contents')
    return value


def record_artifact(conn, document_id, value):
    artifact_id = value['artifact_id']
    body = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    conn.execute('INSERT INTO document_artifacts(artifact_id,document_id,doc_format,body_json) '
                 'VALUES(?,?,?,?) ON CONFLICT(artifact_id) DO NOTHING',
                 (artifact_id, document_id, value['doc_format'], body))
    row = conn.execute('SELECT body_json FROM document_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
    if row is None or json.loads(row[0]) != value:
        raise ValueError('Immutable artifact identity conflicts with existing business evidence')

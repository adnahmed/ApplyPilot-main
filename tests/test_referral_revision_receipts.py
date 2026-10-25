"""Draft mutation and replay receipts commit together in an isolated PostgreSQL schema."""
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from applypilot import postgres
from applypilot.integrations import referrals
from applypilot.orchestration import store as receipts
from applypilot.referrals import outreach, store


@pytest.fixture
def revision_database(monkeypatch):
    value = os.getenv('APPLYPILOT_ISOLATED_DATABASE_URL')
    if not value:
        pytest.skip('APPLYPILOT_ISOLATED_DATABASE_URL is opt-in')
    parsed = urlsplit(value)
    directory = Path(os.environ.get('APPLYPILOT_DIR', '')).resolve()
    if (parsed.hostname not in {'localhost', '127.0.0.1'} or parsed.path != '/applypilot_validation'
            or parsed.username != 'validation' or not directory.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or not directory.name.startswith('applypilot-isolated-')):
        raise ValueError('Revision checks require the disposable validation database and temporary workspace')
    schema = 'revision_' + uuid4().hex
    admin = psycopg.connect(value, autocommit=True)
    admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    connections = []

    def connect():
        raw = psycopg.connect(value, autocommit=True)
        raw.execute(sql.SQL('SET search_path TO {}, pg_catalog').format(sql.Identifier(schema)))
        conn = postgres.Connection(raw, directory / 'revision.pgstore', schema, isolation_level=None, timeout=5)
        connections.append(conn)
        return conn

    conn = connect()
    try:
        receipts.initialize(conn)
        conn.execute('''CREATE TABLE referral_outbox (
            id TEXT PRIMARY KEY, state TEXT, attempted_at TEXT, payload_hash TEXT, payload_json TEXT,
            case_id TEXT, contact_id TEXT, channel TEXT, basis_hash TEXT, approved_hash TEXT,
            approved_by TEXT, approved_at TEXT, updated_at TEXT)''')
        conn.execute('''CREATE TABLE referral_generated (
            outbox_id TEXT PRIMARY KEY, payload_hash TEXT, template TEXT, version INTEGER, created_at TEXT)''')
        payload = {'body': 'Original message'}
        body = store.dump(payload)
        conn.execute('''INSERT INTO referral_outbox (
            id,state,payload_hash,payload_json,case_id,contact_id,channel,approved_hash,approved_by)
            VALUES(?,?,?,?,?,?,?,?,?)''',
            ('draft', 'approved', store.digest(body), body, 'case', 'contact', 'email', store.digest(body), 'owner-before'))
        contact = {'id': 'contact', 'relationship': 'professional'}
        case = {'id': 'case', 'plan_json': '{}'}
        monkeypatch.setattr(outreach, '_validate_current', lambda conn, row, profile=None: (
            store.obj(row['payload_json']), contact, case))
        monkeypatch.setattr(outreach, '_contact', lambda conn, identifier: contact)
        monkeypatch.setattr(outreach, 'basis', lambda conn, case, contact: 'current-basis')
        monkeypatch.setattr(store, 'require_case', lambda conn, identifier: case)
        monkeypatch.setattr(store, 'application_phase', lambda conn, identifier: 'before')
        monkeypatch.setattr(referrals, 'conversation_context', lambda *args: {})
        yield conn, connect
    finally:
        for opened in connections:
            opened.close()
        admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        admin.close()


def approve_current(conn):
    with store.transaction(conn):
        conn.execute("UPDATE referral_outbox SET state='approved',approved_hash=payload_hash,approved_by='owner-after' WHERE id='draft'")


def test_retried_revision_replays_result_and_preserves_later_approval(revision_database):
    conn, _ = revision_database
    with patch('applypilot.reasoning.workflow_json', return_value={'body': 'Revised message'}) as model:
        first = referrals.ai_revise(conn, 'draft', {}, operation_id='activity-revision')
        approve_current(conn)
        second = referrals.ai_revise(conn, 'draft', {}, operation_id='activity-revision')
    assert second == first
    model.assert_called_once()
    current = outreach._outbox(conn, 'draft')
    assert current['state'] == 'approved'
    assert current['approved_by'] == 'owner-after'
    assert current['approved_hash'] == first['outbox']['payload_hash']
    assert conn.execute('SELECT COUNT(*) FROM temporal_publication_receipts').fetchone()[0] == 1


def test_receipt_write_failure_rolls_back_mutation_before_retry(revision_database):
    conn, _ = revision_database
    original = outreach._outbox(conn, 'draft')
    with patch('applypilot.reasoning.workflow_json', return_value={'body': 'Revised message'}) as model:
        with patch.object(receipts, 'save_receipt', side_effect=RuntimeError('receipt store unavailable')):
            with pytest.raises(RuntimeError, match='receipt store unavailable'):
                referrals.ai_revise(conn, 'draft', {}, operation_id='activity-revision')
        assert outreach._outbox(conn, 'draft') == original
        assert conn.execute('SELECT COUNT(*) FROM temporal_publication_receipts').fetchone()[0] == 0
        result = referrals.ai_revise(conn, 'draft', {}, operation_id='activity-revision')
    assert model.call_count == 2
    assert store.obj(result['outbox']['payload_json'])['body'] == 'Revised message'
    assert conn.execute('SELECT COUNT(*) FROM temporal_publication_receipts').fetchone()[0] == 1


def test_attempt_finished_during_generation_is_replayed_without_overwriting_approval(revision_database):
    conn, connect = revision_database
    other = connect()
    saved = []

    def another_attempt_finishes(*args, **kwargs):
        with patch('applypilot.reasoning.workflow_json', return_value={'body': 'Already committed message'}):
            saved.append(referrals.ai_revise(other, 'draft', {}, operation_id='shared-activity'))
        approve_current(other)
        return {'body': 'Late obsolete generation'}

    with patch('applypilot.reasoning.workflow_json', side_effect=another_attempt_finishes):
        result = referrals.ai_revise(conn, 'draft', {}, operation_id='shared-activity')
    assert result == saved[0]
    current = outreach._outbox(conn, 'draft')
    assert store.obj(current['payload_json'])['body'] == 'Already committed message'
    assert current['state'] == 'approved'
    assert current['approved_by'] == 'owner-after'


def test_callers_without_operation_identity_keep_existing_revision_behavior(revision_database):
    conn, _ = revision_database
    with patch('applypilot.reasoning.workflow_json', return_value={'body': 'Legacy caller revision'}), \
         patch.object(receipts, 'receipt', side_effect=AssertionError('Unexpected receipt lookup')), \
         patch.object(receipts, 'save_receipt', side_effect=AssertionError('Unexpected receipt write')):
        result = referrals.ai_revise(conn, 'draft', {})
    assert store.obj(result['outbox']['payload_json'])['body'] == 'Legacy caller revision'


def test_automatic_draft_improvement_forwards_stable_operation_identity():
    with patch('applypilot.reasoning.settings.dspy_enabled', return_value=True), \
         patch.object(referrals, 'ai_revise', return_value={'outbox': {'id': 'draft'}}) as revise:
        assert referrals.improve_draft('connection', {'id': 'draft'}, {}, operation_id='activity') == {'id': 'draft'}
    revise.assert_called_once_with('connection', 'draft', {}, operation_id='activity')

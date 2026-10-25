"""One approved generation and one render at a time; no batch runner or scheduler."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from applypilot.orchestration.contracts import digest
from . import artifacts, store
from .contracts import PrepareDocument, PreparedDocument, RenderDocument, DocumentResult


def _read_job(job_url):
    from applypilot.database import get_connection, close_connection
    try:
        row = get_connection().execute('SELECT * FROM jobs WHERE url=?', (job_url,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        close_connection()


def _work(reference):
    from applypilot.database import get_connection, close_connection
    from applypilot.orchestration.store import work_payload
    try:
        value, root = work_payload(get_connection(), reference)
        row = get_connection().execute('SELECT * FROM jobs WHERE url=?', (value['job_url'],)).fetchone()
        return dict(row) if row is not None else None, root
    finally:
        close_connection()


def _capture(value):
    from applypilot.database import get_connection, close_connection
    try:
        conn = get_connection()
        result = store.capture(conn, value)
        conn.commit()
        return result
    finally:
        close_connection()


def _read(document_id):
    from applypilot.database import get_connection, close_connection
    try:
        return store.read(get_connection(), document_id)
    finally:
        close_connection()


def _record(document_id, value):
    from applypilot.database import get_connection, close_connection
    try:
        conn = get_connection()
        store.record_artifact(conn, document_id, value)
        conn.commit()
    finally:
        close_connection()


def _source_path(value, root):
    """Capture old absolute pointers once; native Activity data uses relative paths."""
    source = Path(value).with_suffix('.txt')
    if not source.is_absolute():
        source = root / source
    source = source.resolve()
    if not source.is_relative_to(root.resolve()):
        raise ValueError('Approved source is outside the shared workspace')
    return source


def _approved(job, proof, text, stage):
    from applypilot.preparation_guard import artifact_snapshot_matches
    from applypilot.career.documents import revoked_document_sources
    return (isinstance(proof, dict) and proof.get('status') == 'approved'
            and proof.get('stage') == stage and proof.get('job_url') == job.get('url')
            and proof.get('sha256') == hashlib.sha256(text.encode('utf-8')).hexdigest()
            and bool(text.strip())
            and artifact_snapshot_matches(job, proof.get('source_snapshot', {}))
            and not revoked_document_sources(proof.get('document_context')))


def prepare(request: PrepareDocument) -> PreparedDocument:
    request.validate()
    from applypilot import config
    from applypilot.scoring.artifact_store import manifest_path
    job, root = _work(request.job_reference)
    doc_format = root['options'].get('doc_format', 'pdf')
    if doc_format not in {'pdf', 'docx'}:
        raise ValueError('Unsupported output format')
    column = 'tailored_resume_path' if request.stage == 'tailor' else 'cover_letter_path'
    if job is None or not job.get(column):
        return PreparedDocument(None, 'not_applicable', doc_format)
    from applypilot.apply.application_contract import selected_documents
    if not selected_documents(job)['resume' if request.stage == 'tailor' else 'cover_letter']:
        return PreparedDocument(None, 'not_applicable', doc_format)
    source = _source_path(job[column], config.APP_DIR)
    try:
        text = source.read_bytes().decode('utf-8')
        proof = json.loads(manifest_path(source).read_text(encoding='utf-8'))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return PreparedDocument(None, 'approval_not_current', doc_format)
    if not _approved(job, proof, text, request.stage):
        return PreparedDocument(None, 'approval_not_current', doc_format)
    profile = root.get('profile', {})
    personal = profile.get('personal') or {}
    value = {
        'kind': 'approved_document', 'version': 1, 'job_url': job['url'],
        'stage': request.stage, 'source_column': column,
        'source_locator': job[column],  # compare business pointer, never route workers by this
        'source_relative': source.relative_to(config.APP_DIR.resolve()).as_posix(),
        'source_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'text': text, 'approval': proof,
        'metadata': {'author': personal.get('full_name') or personal.get('preferred_name') or '',
                     'title': ('Resume' if request.stage == 'tailor' else 'Cover Letter')
                              + ' — ' + str(job.get('title') or ''),
                     'subject': str(job.get('title') or ''),
                     'company': str(job.get('company') or job.get('site') or ''),
                     'category': 'Resume' if request.stage == 'tailor' else 'Cover Letter'},
    }
    # No file/network/model work is performed while persisting the immutable input.
    document_id = _capture(value)
    return PreparedDocument(document_id, 'prepared', doc_format)


def _still_current(value, source):
    from applypilot.scoring.artifact_store import manifest_path
    job = _read_job(value['job_url'])
    if job is None or job.get(value['source_column']) != value['source_locator']:
        return False
    # Files and database business pointers are rechecked after rendering. Worker
    # reservations, agent IDs, cooldowns and scheduler state are not consulted.
    try:
        text = source.read_bytes().decode('utf-8')
        proof = json.loads(manifest_path(source).read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return False
    return (text == value['text'] and digest(proof) == digest(value['approval'])
            and _approved(job, proof, text, value['stage']))


def render(request: RenderDocument, *, check_cancel=lambda: None, renderer=None) -> DocumentResult:
    request.validate()
    from applypilot import config
    value = _read(request.document_id)
    if value.get('kind') != 'approved_document':
        raise ValueError('Not an approved document input')
    source = artifacts.inside(config.APP_DIR, value['source_relative'])
    check_cancel()
    if not _still_current(value, source):
        return DocumentResult(request.document_id, 'superseded')
    if renderer is None:
        from applypilot.scoring.pdf import convert_to_pdf
        renderer = convert_to_pdf
    manifest = artifacts.publish(artifacts.storage_root(), request.document_id, value,
                                 request.doc_format, renderer, check_cancel)
    check_cancel()
    if not _still_current(value, source):
        # The immutable bundle is retained as evidence but not selected for upload.
        return DocumentResult(request.document_id, 'superseded', manifest['artifact_id'])
    _record(request.document_id, manifest)
    check_cancel()
    artifacts.publish_index(source, value, manifest)
    return DocumentResult(request.document_id, 'rendered', manifest['artifact_id'])

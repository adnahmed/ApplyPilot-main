"""Capture, extract and publish one signal. No scheduling or model-call budget."""
from __future__ import annotations

from dataclasses import asdict, replace

from applypilot.orchestration import store
from applypilot.orchestration.contracts import digest
from .contracts import SignalInput, SignalResult

ROLE_SCHEMA = {'type': 'object', 'required': ['roles'], 'properties': {
    'roles': {'type': 'array', 'items': {'type': 'object', 'required': ['title'],
        'properties': {'title': {'type': 'string', 'minLength': 1}}}}}}


def capture(snapshot, provider, post, revision, hiring_thread=False):
    from applypilot.database import get_connection, close_connection
    try:
        conn = get_connection()
        # Input is committed before the provider Activity may return a cursor.
        reference = store.put(conn, {'kind': 'signal', 'snapshot': snapshot,
            'provider': provider, 'post': post, 'revision': revision,
            'hiring_thread': bool(hiring_thread)})
        conn.commit()
        return reference
    finally:
        close_connection()


def _load(request):
    request.validate()
    from applypilot.database import get_connection, close_connection
    try:
        value, root = store.work_payload(get_connection(), request.signal_id)
        if value.get('kind') != 'signal':
            raise ValueError('Input does not describe a captured signal')
        content = {key: value['post'].get(key) for key in ('id', 'text', 'links', 'deleted')}
        from applypilot.discovery.signal_cache import digest as revision_digest
        if revision_digest(content) != value['revision']:
            raise ValueError('Signal revision does not match the captured source')
        return value, root
    finally:
        close_connection()


def extract(request: SignalInput, *, check_cancel=lambda: None) -> SignalResult:
    from applypilot.discovery.signal_cache import cache_for, extract_roles, prefilter, InvalidExtractionEvidence
    from applypilot.integrations.model_gateway import complete_json
    from applypilot.integrations.types import SourceError
    value, root = _load(request)
    context = store.context_from(root)
    post = value['post']
    if post['deleted'] or not prefilter(post['text'], context, hiring_thread=value['hiring_thread']):
        return SignalResult(request.signal_id, 'not_applicable')
    context = replace(context, llm_json=lambda prompt: complete_json(prompt, schema=ROLE_SCHEMA))
    check_cancel()
    try:
        roles = extract_roles(context, cache_for(context), value['provider'], post, value['revision'])
    except InvalidExtractionEvidence:
        raise
    except ValueError:
        # Invalid generated output is a model failure, not invalid user data.
        # Never serialize source text in the Activity failure.
        raise SourceError('Signal role extraction failed source validation', 'model') from None
    check_cancel()
    return SignalResult(request.signal_id, 'extracted', len(roles))


def publish(request: SignalInput, *, check_cancel=lambda: None) -> SignalResult:
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery import repository
    from applypilot.discovery.signal_cache import cache_for, normalize_roles, prefilter, validate_roles
    value, root = _load(request)
    context = store.context_from(root)
    post = value['post']
    selected = not post['deleted'] and prefilter(post['text'], context, hiring_thread=value['hiring_thread'])
    roles = cache_for(context).extraction(value['provider'], post['id'], value['revision']) if selected else []
    if selected and roles is None:
        raise LookupError('Signal has no completed extraction evidence')
    roles = validate_roles({'roles': roles}, post)
    records, leads = normalize_roles(context, value['provider'], post, value['revision'], roles,
                                    hiring_thread=value['hiring_thread'])
    receipt_id = digest(('signal_publication_v1', request.signal_id))
    check_cancel()
    try:
        conn = get_connection()
        # This lock serializes publication of the same business receipt only;
        # it never spans model/network I/O, represents ownership, or expires.
        with repository.transaction(conn, resource='signal-publication:' + request.signal_id):
            prior = store.receipt(conn, receipt_id)
            if prior is not None:
                return SignalResult(**prior)
            counts = repository.publish_records(records, value['provider'], context=context, conn=conn)
            if leads:
                from applypilot.discovery.freehire_registry import store_board_leads
                store_board_leads(conn, [{**lead, 'url': lead.get('observed_url')} for lead in leads],
                                  origin='signal:' + request.signal_id)
            result = SignalResult(request.signal_id, 'published' if records else 'no_roles', counts.new)
            store.save_receipt(conn, receipt_id, asdict(result))
        return result
    finally:
        close_connection()

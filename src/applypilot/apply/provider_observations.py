"""Attributable provider facts; Temporal decides when the account runs again."""
from __future__ import annotations

from datetime import UTC, datetime
import math
from urllib.parse import urlsplit

from applypilot.orchestration.contracts import digest
from applypilot.orchestration import store


KIND = 'application-provider-restriction-v1'


def bind_account(conn, attempt_id, workflow_id, account_id):
    key = digest(['application-account', attempt_id])
    value = {'workflow_id': workflow_id, 'account_id': account_id}
    with conn:
        conn.advisory_lock(key)
        previous = store.receipt(conn, key)
        if previous is not None and previous != value:
            raise ValueError('Application attempt belongs to another provider account')
        if previous is None:
            store.save_receipt(conn, key, value)


def restriction_fact(attempt_id, *, conn=None):
    from applypilot.database import get_connection
    return store.receipt(conn or get_connection(), digest([KIND, attempt_id]))


def capture_restriction(attempt_id, page_id, invocation, observation, evidence, retry_after_seconds=None):
    from applypilot.database import get_connection, close_connection
    from .guard import attempt_status, GuardRejected

    if (retry_after_seconds is not None and
            (type(retry_after_seconds) not in (int, float) or
             not math.isfinite(retry_after_seconds) or retry_after_seconds <= 0)):
        raise ValueError('A displayed retry delay must be positive and finite')
    quote = ' '.join(str(evidence or '').split())
    visible = ' '.join((str(observation.get('text') or '') + ' ' + str(observation.get('snapshot') or '')).split())
    if len(quote) < 8 or len(quote) > 4000 or quote not in visible:
        raise ValueError('Quote the restriction exactly from the current page')
    host = (urlsplit(observation.get('url', '')).hostname or '').lower()
    if host != 'linkedin.com' and not host.endswith('.linkedin.com'):
        raise ValueError('This observation does not come from LinkedIn')
    try:
        conn = get_connection()
        attempt = attempt_status(attempt_id, conn=conn)
        from applypilot import config
        from .browseros_mcp import read_state
        bridge = read_state(config.APP_DIR / 'browseros-sessions' / (attempt_id + '.json'))
        if (attempt['channel'] != 'linkedin-easy-apply' or
                not invocation or bridge.get('invocation') != invocation or
                (bridge.get('bound_pages') or {}).get(str(page_id)) != attempt['target']):
            raise GuardRejected('Provider observation does not belong to this account invocation')
        key = digest([KIND, attempt_id])
        with conn:
            conn.advisory_lock(key)
            existing = store.receipt(conn, key)
            if existing:
                return existing
            binding = store.receipt(conn, digest(['application-account', attempt_id]))
            if binding is None:
                raise GuardRejected('Application has no account Workflow binding')
            observed_at = datetime.now(UTC).timestamp()
            reference = store.put(conn, {'kind': KIND, 'attempt_id': attempt_id,
                'page_id': page_id, 'invocation': invocation, 'target': attempt['target'],
                'observation': observation, 'evidence': evidence, 'observed_at': observed_at,
                'retry_after_seconds': retry_after_seconds, **binding})
            fact = {'kind': KIND, 'attempt_id': attempt_id, 'observed_at': observed_at,
                    'retry_after_seconds': retry_after_seconds, 'evidence_reference': reference, **binding}
            store.save_receipt(conn, key, fact)
            return fact
    finally:
        close_connection()


async def notify_account(fact):
    from applypilot.orchestration.connection import connect
    client = await connect()
    await client.get_workflow_handle(fact['workflow_id']).signal('provider_restriction', fact)


def account_observations(conn, workflow_id):
    rows = conn.execute("SELECT body_json FROM temporal_publication_receipts "
        "WHERE body_json::jsonb->>'kind'=? AND body_json::jsonb->>'workflow_id'=?",
        (KIND, workflow_id)).fetchall()
    import json
    return [json.loads(row[0]) for row in rows]

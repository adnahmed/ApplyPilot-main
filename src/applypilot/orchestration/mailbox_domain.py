"""One mailbox semantic operation per Activity, with no local execution owner.

Provider bodies/cursors remain in PostgreSQL, not Workflow histories. Publication
receipts are committed atomically with message IDs and coverage, so an Activity
retry cannot skip a page whose acknowledgement was lost.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict

from . import store
from .contracts import ActivityRequest, Observation, digest

PHASES = frozenset({
    'mailbox_begin', 'mailbox_page', 'mailbox_pending_page', 'mailbox_fetch_message',
    'mailbox_classify_message', 'mailbox_publish_message', 'mailbox_observations',
    'mailbox_outcome_model', 'mailbox_tracking_document',
})


def _check(stop):
    if stop.is_set():
        from applypilot.execution import ExecutionStopped
        raise ExecutionStopped('Mailbox Activity cancelled')


def _message_reference(conn, snapshot, mailbox, key):
    return store.put(conn, {'snapshot': snapshot, 'mailbox_key': mailbox, 'message_key': key})


def _pending_references(conn, snapshot, mailbox, keys):
    return [{'reference': _message_reference(conn, snapshot, mailbox, key), 'subject': key}
            for key in dict.fromkeys(keys)]


def execute(request: ActivityRequest, stop, heartbeat) -> Observation:
    from applypilot.database import get_connection
    from applypilot.tracking import feedback_inbox as inbox
    from applypilot.tracking.feedback_store import backfill_submissions, iso, obj, policy, transaction
    from applypilot.tracking.gmail_client import open_feedback_mailbox, GmailReadError

    conn = get_connection()
    value, root = store.work_payload(conn, request.reference)
    snapshot = value.get('snapshot', request.reference)
    options = root['options']
    phase = request.phase
    _check(stop)

    if phase == 'mailbox_begin':
        if options.get('dry_run'):
            conn.commit()
            return Observation('dry_run')
        sweep = request.options.get('sweep')
        if not sweep:
            raise ValueError('A mailbox observation requires a stable scan identity')
        key = digest(['mailbox-begin-v1', request.reference, sweep])
        previous = store.receipt(conn, key)
        conn.commit()
        if previous:
            return Observation(**previous)

        async def identify():
            async with open_feedback_mailbox() as mailbox:
                return mailbox.mailbox_key

        identity = asyncio.run(identify())
        _check(stop)
        inbox.bind_mailbox(conn, identity)
        backfill_submissions(conn)
        cfg = policy(conn)
        with transaction(conn):
            previous = store.receipt(conn, key)
            if previous:
                return Observation(**previous)
            windows = []
            for lane in ('recent', 'history'):
                window = inbox.scan_window(conn, identity, days=options.get('track_days', 14),
                                           since=options.get('track_since'), recent=lane == 'recent')
                reference = store.put(conn, {'snapshot': snapshot, 'window': window,
                                             'mailbox_key': identity, 'scan': sweep,
                                             'page_size': cfg['sync_page_size']})
                windows.append({'lane': lane, 'reference': reference})
            backlog = store.put(conn, {'snapshot': snapshot, 'mailbox_key': identity})
            result = Observation('ready', {'account': identity, 'windows': windows, 'backlog': backlog})
            store.save_receipt(conn, key, asdict(result))
        return result

    identity = value['mailbox_key']
    if phase == 'mailbox_page':
        key = digest(['mailbox-page-v1', request.reference, request.cursor])
        previous = store.receipt(conn, key)
        conn.commit()
        if previous:
            return Observation(**previous)
        window = value['window']

        async def fetch_page():
            async with open_feedback_mailbox() as mailbox:
                if mailbox.mailbox_key != identity:
                    raise ValueError('Configured mailbox does not match this observation')
                return await mailbox.list_page(window['after'], window['before'], request.cursor,
                                               value['page_size'])

        messages, cursor = asyncio.run(fetch_page())
        _check(stop)
        if cursor and cursor == request.cursor:
            raise ValueError('Mailbox provider pagination did not advance')
        with transaction(conn):
            previous = store.receipt(conn, key)
            if previous:
                return Observation(**previous)
            keys = inbox.store_message_page(conn, window, messages, complete=cursor is None)
            items = _pending_references(conn, snapshot, identity, keys)
            result = Observation('complete' if cursor is None else 'page', {'messages': items}, cursor)
            store.save_receipt(conn, key, asdict(result))
        return result

    if phase == 'mailbox_pending_page':
        # This inventories durable incoming-message entities. It does not claim,
        # order by retry clocks, or replace Temporal's failed-Activity state.
        size = request.options.get('page_size', 100)
        if type(size) is not int or not 1 <= size <= 500:
            raise ValueError('Invalid mailbox inventory page size')
        rows = conn.execute("SELECT message_key FROM feedback_messages WHERE mailbox_key=? "
                            "AND message_key>? AND state IN ('pending','review') "
                            "AND COALESCE(link_reason,'') NOT IN ('manual_review','manual_ignore','event_retracted') "
                            "ORDER BY message_key LIMIT ?", (identity, request.cursor or '', size + 1)).fetchall()
        selected = rows[:size]
        items = _pending_references(conn, snapshot, identity, [r[0] for r in selected])
        conn.commit()
        return Observation('page' if len(rows) > size else 'complete', {'messages': items},
                           selected[-1][0] if len(rows) > size else None)

    if phase == 'mailbox_observations':
        result = inbox.refresh_observations(conn, identity, silence_days=options.get('silence_days'))
        conn.commit()
        return Observation('complete', result)

    if phase == 'mailbox_outcome_model':
        # A statistical business model, not an optimizer/scheduler controller.
        from applypilot.tracking.feedback_learning import build_model
        conn.commit()
        result = build_model(conn)
        conn.commit()
        return Observation('complete', {'model_id': result['model_id'],
                                        'ready': bool(result['summary']['ready'])})

    key = value['message_key']
    row = conn.execute('SELECT * FROM feedback_messages WHERE message_key=? AND mailbox_key=?',
                       (key, identity)).fetchone()
    if row is None:
        raise ValueError('Message does not belong to this mailbox')
    row = dict(row)
    if phase == 'mailbox_tracking_document':
        submission = conn.execute('SELECT job_url FROM feedback_submissions WHERE submission_id=?',
                                  (row.get('submission_id'),)).fetchone()
        if submission is None:
            conn.commit()
            return Observation('not_applicable')
        job = conn.execute('SELECT * FROM jobs WHERE url=?', (submission[0],)).fetchone()
        conn.commit()
        from applypilot.tracking.markdown_gen import generate_tracking_doc
        path = generate_tracking_doc(dict(job), conn=conn)
        if path is None:
            raise OSError('Tracking document was not published')
        # The document path is business storage data, not a Workflow result.
        return Observation('rendered')
    if row.get('link_reason') in inbox.MANUAL_DECISIONS:
        conn.commit()
        return Observation('skipped')
    if row['state'] in {'processed', 'ignored'}:
        conn.commit()
        return Observation(row['state'])

    if phase == 'mailbox_fetch_message':
        if row.get('payload_json'):
            conn.commit()
            return Observation('fetched')
        conn.commit()

        async def fetch_message():
            async with open_feedback_mailbox() as mailbox:
                if mailbox.mailbox_key != identity:
                    raise ValueError('Configured mailbox does not match this message')
                return await mailbox.get(row['message_id'])

        email = asyncio.run(fetch_message())
        _check(stop)
        state = inbox.cache_message(conn, key, email)
        conn.commit()
        return Observation(state)

    email = obj(row.get('payload_json'))
    if not email or email.get('id') != row['message_id']:
        raise ValueError('A verified message body is required')
    index = inbox.LinkIndex(conn, identity)

    if phase == 'mailbox_classify_message':
        # Revisit ambiguous links with saved evidence, without paying to classify
        # the same message again. Manual dispositions are checked above and on publish.
        result = obj(row.get('result_json')) if row['state'] == 'review' else None
        outgoing = (inbox.digest(email.get('sender', '').casefold()) == identity
                    or bool(set(email.get('labels', ())) & {'SENT', 'DRAFT'}))
        if not outgoing:
            from applypilot.referrals.inbox import ingest, outcome_review
            if ingest(conn, identity, key, email) and not outcome_review(conn, key):
                with transaction(conn):
                    conn.execute("UPDATE feedback_messages SET state='ignored',link_reason='networking_conversation',"
                                 "updated_at=? WHERE message_key=? AND COALESCE(link_reason,'') "
                                 "NOT IN ('manual_review','manual_ignore','event_retracted')", (iso(), key))
                conn.commit()
                return Observation('ignored')
        if not result:
            if outgoing:
                result = {'event_type': 'noise', 'confidence': 1.0, 'summary': 'outbound message'}
            else:
                sid, _, candidates = index.match(email)
                result = inbox.cheap_classify(email, bool(sid or candidates))
            if result is None:
                history = [obj(r[0]) for r in conn.execute(
                    'SELECT payload_json FROM feedback_messages WHERE mailbox_key=? AND thread_id=? '
                    'AND message_key<>? AND payload_json IS NOT NULL ORDER BY received_at,message_key',
                    (identity, email.get('thread_id'), key))] if email.get('thread_id') else []
                with transaction(conn):
                    conn.execute('UPDATE feedback_messages SET attempts=attempts+1 WHERE message_key=?', (key,))
                conn.commit()  # No DB transaction spans model/provider work.
                try:
                    result = inbox.classify_outcome({**email, 'thread_history': history})
                except inbox.ClassificationError as exc:
                    inbox.record_classification_review(conn, key, email, exc.code, index)
                    conn.commit()
                    return Observation('review')
                _check(stop)
        conn.commit()
        resolved = (index.match(email) if result['event_type'] in {'noise', 'unknown'}
                    else index.resolve(email))
        _check(stop)
        reference = store.put(conn, {'snapshot': snapshot, 'mailbox_key': identity, 'message_key': key,
                                     'result': result, 'resolved': resolved, 'payload_digest': inbox.digest(row['payload_json'])})
        conn.commit()
        return Observation('classified', {'reference': reference})

    if phase == 'mailbox_publish_message':
        result = value['result']
        state = inbox.accept_classification(conn, key, email, result, index,
                                           payload_digest=value['payload_digest'], resolved=value['resolved'])
        conn.commit()
        return Observation(state)
    raise ValueError('Unsupported mailbox operation')

"""Map existing, attributable invitation evidence into the career report lifecycle."""
from __future__ import annotations

import json
from . import store

INVITATIONS = {'screen', 'interview', 'technical_interview'}


def source_for_job(conn, job_url):
    event = conn.execute("""SELECT id,source,occurred_at,notes,payload_json,message_key
        FROM application_outcomes WHERE job_url=? AND voided_at IS NULL
        AND trust IN ('manual','automatic-v1') AND event_type IN ('screen','interview','technical_interview')
        ORDER BY occurred_at DESC,id DESC LIMIT 1""", (job_url,)).fetchone()
    if event:
        detail = json.loads(event['payload_json'] or '{}')
        message_key = event['message_key'] or detail.get('referral_message_key')
        source = {'id': 'message:' + message_key if message_key else 'outcome:' + str(event['id']),
                  'channel': 'gmail' if event['source'] == 'email' else event['source'],
                  'received_at': event['occurred_at'], 'text': detail.get('evidence_quote') or event['notes'],
                  'action_items': detail.get('action_items', [])}
    else:
        email = conn.execute("""SELECT email_id,received_at,subject,body_text FROM tracking_emails
            WHERE job_url=? AND classification='interview' ORDER BY received_at DESC LIMIT 1""", (job_url,)).fetchone()
        source = ({'id': 'message:' + email['email_id'].removeprefix('gmail:'), 'channel': 'gmail', 'received_at': email['received_at'],
                   'text': (email['subject'] or '') + '\n' + (email['body_text'] or '')} if email else
                  {'id': 'invited:' + job_url, 'channel': 'saved interview status', 'text': ''})
    return source



def queue_job(conn, job_url):
    store.queue_invitation(conn, job_url, source_for_job(conn, job_url))


def seed(conn, limit=100):
    from applypilot.database import interview_invitation_pending_sql
    rows = conn.execute('SELECT j.url FROM jobs j WHERE ' + interview_invitation_pending_sql()
                        + ' ORDER BY j.tracking_updated_at DESC NULLS LAST LIMIT ?', (limit,)).fetchall()
    for row in rows:
        queue_job(conn, row['url'])
    return len(rows)


def classify_replies(conn, limit=10, *, message_key=None):
    """Reuse captured direct threads and the existing grounded outcome classifier."""
    from applypilot.tracking.feedback_inbox import classify_outcome
    from applypilot.discovery.repository import transaction
    rows = conn.execute("""SELECT m.*,c.job_url,j.title AS job_title,j.company AS job_company FROM referral_messages m
        JOIN referral_cases c ON c.id=m.case_id
        JOIN jobs j ON j.url=c.job_url
        WHERE c.job_url IS NOT NULL AND c.job_url<>'' AND m.suggested_kind<>'opt_out'
        AND (? IS NULL OR m.message_key=?)
        AND m.context_json::jsonb->'career_classification' IS NULL
        AND m.context_json::jsonb->>'match_reason' IN ('verified_social_thread','verified_contact_thread')
        ORDER BY m.received_at DESC LIMIT ?""", (message_key, message_key, limit)).fetchall()
    for row in rows:
        context = json.loads(row['context_json'] or '{}')
        result = classify_outcome({'subject': row['subject'], 'body': row['excerpt'],
                                   'date': row['received_at'], 'sender': row['sender'],
                                   'application_context': {'job_url': row['job_url'], 'title': row['job_title'],
                                                           'company': row['job_company'], 'match': context['match_reason']}})
        with transaction(conn, resource='career-ops'):
            updated = conn.execute("""UPDATE referral_messages SET context_json=
                (context_json::jsonb || json_build_object('career_classification',?::jsonb)::jsonb)::text
                WHERE message_key=? AND case_id=?""", (store.dump(result), row['message_key'], row['case_id']))
            if updated.rowcount != 1:
                continue
            if result['event_type'] in INVITATIONS and result.get('evidence_quote'):
                store.queue_invitation(conn, row['job_url'], {'id': 'message:' + row['message_key'],
                    'channel': context.get('channel', 'gmail'), 'received_at': row['received_at'],
                    'text': row['excerpt'], 'action_items': result.get('action_items', [])})

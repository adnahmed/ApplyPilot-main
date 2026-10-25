"""Reuse already-fetched Gmail messages. Employee replies are NOT recruiter rewards.

No LLM executes mail instructions, and no URL/attachment in a message is opened.
Completed assistance and procedures are proposals until the operator confirms.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit
from applypilot.referrals import store as s


def fresh_text(email):
    text = str(email.get('body') or email.get('snippet') or '')
    text = re.split(r'(?im)^\s*(?:On .{5,250}wrote:|-----Original Message-----|From:.*)', text)[0]
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('>')).strip()


def _case_match(conn, mailbox_key, email):
    sender = str(email.get('sender') or '').casefold()
    # A delivery notification must reference our exact attempted Message-ID AND
    # original recipient. Generic rejection words or a company name are not enough.
    body = fresh_text(email)
    if sender.startswith(('mailer-daemon@', 'postmaster@')) and re.search(r'(?i)(delivery (?:failed|failure)|undeliverable|address not found)', body):
        rfc_ids = re.findall(r'<applypilot-referral-[0-9a-f]{32}@outreach.invalid>', body + str(email.get('references', '')))
        matches = []
        for rfc in rfc_ids:
            matches.extend(dict(r) for r in conn.execute('SELECT * FROM referral_outbox WHERE mailbox_key=? AND rfc_message_id=? AND attempted_at IS NOT NULL', (mailbox_key, rfc)))
        matches = {r['id']: r for r in matches if str(s.obj(r['payload_json']).get('to', '')).casefold() in body.casefold()}
        if len(matches) == 1:
            row = next(iter(matches.values()))
            return row['case_id'], row['contact_id'], 'verified_delivery_failure'
    contacts = [dict(r) for r in conn.execute('SELECT * FROM referral_contacts WHERE email=?', (sender,))]
    ids = {c['id'] for c in contacts}
    refs = set(re.findall(r'<[^<>\s]+>', str(email.get('references',''))+' '+str(email.get('in_reply_to',''))))
    rows = conn.execute('''SELECT * FROM referral_outbox WHERE mailbox_key=?
        AND (thread_id=? OR rfc_message_id IN (''' + ','.join('?' for _ in refs) + '))',
        [mailbox_key, email.get('thread_id'), *refs]).fetchall() if refs else conn.execute(
            'SELECT * FROM referral_outbox WHERE mailbox_key=? AND thread_id=?',
            (mailbox_key, email.get('thread_id'))).fetchall()
    matched = {r['case_id'] for r in rows if r['contact_id'] in ids}
    if len(matched) == 1:
        return next(iter(matched)), contacts[0]['id'], 'verified_contact_thread'
    if len(matched) > 1:
        return None, contacts[0]['id'], 'ambiguous_thread'
    text = fresh_text(email)
    if len(contacts)==1 and re.search(r"(?i)\b(?:do not|don't|stop)\s+(?:emailing|contacting|messaging|email|contact|message)\s+me\b|\bremove me from",text):
        return None,contacts[0]['id'],'known_contact_opt_out'
    if not re.search(r'(?i)\b(referr?al|referred|refer you|referring|forwarded (?:your|the) (?:resume|cv)|internal introduc)', text):
        return None
    links = list(email.get('links') or []) + re.findall(r'https://[^\s<>"\)]+', text)
    keys = {s.posting_key({'url': link, 'route_target': link}) for link in links}
    exact = set()
    for key in keys:
        exact.update(r[0] for r in conn.execute('SELECT id FROM referral_cases WHERE posting_key=?', (key,)))
    # Also recognize an exact saved source URL, without stripping identifying query fields.
    for link in links:
        exact.update(r[0] for r in conn.execute('SELECT case_id FROM referral_job_links WHERE job_url=?', (link,)))
    if len(exact) == 1:
        return next(iter(exact)), contacts[0]['id'] if contacts else None, 'exact_job_referral_notice'
    if exact or contacts:
        return None, contacts[0]['id'] if contacts else None, 'referral_needs_matching'
    return None


def ingest(conn, mailbox_key, message_key, email):
    """True consumes a networking message before the application-outcome classifier."""
    try:
        if conn.execute('SELECT 1 FROM referral_messages WHERE message_key=?', (message_key,)).fetchone():
            return True
    except Exception as exc:
        from applypilot import postgres
        if isinstance(exc, postgres.OperationalError) and 'no such table' in str(exc):
            return False
        raise
    if set(email.get('labels') or []) & {'SENT', 'DRAFT'}:
        return False
    match = _case_match(conn, mailbox_key, email)
    if match is None:
        return False
    cid, contact_id, reason = match
    text = fresh_text(email)
    # Rule labels are observations, not a proof of successful ATS attribution.
    suggested = 'reply_received'
    if reason == 'verified_delivery_failure':
        suggested = 'delivery_failed'
    elif re.search(r"(?i)\b(?:do not|don't|stop)\s+(?:emailing|contacting|messaging|email|contact|message)\s+me\b|\bremove me from", text):
        suggested = 'opt_out'
    elif re.search(r"(?i)\b(?:cannot|can't|unable to|not able to)\s+(?:refer|help)\b", text):
        suggested = 'declined'
    elif re.search(r"(?i)\b(?:have referred|i referred|i've referred|submitted (?:your|the) referral)\b", text):
        suggested = 'referral_claimed'
    elif re.search(r'(?i)\b(happy to refer|can refer you|will refer you)\b', text):
        suggested = 'referral_offered'
    links = list(dict.fromkeys(list(email.get('links') or []) + re.findall(r'https://[^\s<>"\)]+', text)))
    at = s.iso(email['date']) if email.get('date') else s.iso()
    with s.transaction(conn):
        conn.execute('INSERT INTO referral_messages\n            (message_key,case_id,contact_id,mailbox_key,message_id,thread_id,rfc_message_id,received_at,\n             sender,subject,excerpt,links_json,state,suggested_kind,created_at)\n            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
            (message_key, cid, contact_id, mailbox_key, email['id'], email.get('thread_id'),
             email.get('rfc_message_id'), at, email.get('sender',''), email.get('subject',''),
             text, s.dump(links), 'review', suggested, s.iso()))
        conn.execute('UPDATE referral_messages SET context_json=? WHERE message_key=?',
                     (s.dump({'match_reason': reason, 'auto_submitted': email.get('auto_submitted',''),
                              'precedence': email.get('precedence',''), 'reply_to': email.get('reply_to',''),
                              'labels': list(email.get('labels') or []), 'from_truncated': False, 'full_message': email.get('body') or text,
                              'in_reply_to': email.get('in_reply_to',''), 'references': email.get('references','')}), message_key))
        if cid:
            s.event(conn, cid, suggested, contact_id=contact_id, quote=text[:2000],
                    at=at, key='mail:'+message_key, message_key=message_key, trust='observed',
                    details={'link_reason': reason})
            # New context requires a real answer, never a pre-approved generic followup.
            conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE case_id=? AND contact_id IS NOT DISTINCT FROM ? AND state IN ('draft','approved')", (cid, contact_id))
        if contact_id and suggested == 'opt_out':
            s.block_contact(conn, contact_id, 'explicit opt-out in message '+message_key)
    return True


def review_message(conn, message_key, *, kind, quote, case_id=None, contact_id=None):
    """Confirm the meaning of a quoted message; no route changes or outgoing mail."""
    with s.transaction(conn):
        row = conn.execute('SELECT * FROM referral_messages WHERE message_key=?', (message_key,)).fetchone()
        if not row or not quote or quote not in row['excerpt']:
            raise ValueError('quote must appear in the captured message excerpt')
        cid = case_id or row['case_id']
        if not cid:
            raise ValueError('select the exact referral case for this ambiguous message')
        c = s.require_case(conn, cid)
        person = contact_id or row['contact_id']
        if person:
            verified = conn.execute('SELECT 1 FROM referral_contacts WHERE id=? AND company_key=?', (person, c['company_key'])).fetchone()
            if not verified:
                raise ValueError('contact does not belong to this employer')
        e = s.event(conn, cid, kind, contact_id=person, quote=quote,
                    key='review:'+message_key+':'+kind, message_key=message_key,
                    at=row['received_at'], trust='operator-confirmed')
        conn.execute("UPDATE referral_messages SET case_id=?,contact_id=?,state='reviewed',reviewed_at=? WHERE message_key=?", (cid, person, s.iso(), message_key))
        conn.execute("UPDATE feedback_messages SET state='ignored',link_reason='manual_ignore',updated_at=? WHERE message_key=? AND link_reason='networking_outcome_review'", (s.iso(), message_key))
    return e


def link_application_outcome(conn, message_key, submission_id, event_type, quote):
    """A real interview in a networking thread can be explicitly routed to feedback."""
    from applypilot.tracking.feedback_store import record_event
    row = conn.execute('SELECT * FROM referral_messages WHERE message_key=?', (message_key,)).fetchone()
    sub = conn.execute('SELECT * FROM feedback_submissions WHERE submission_id=?', (submission_id,)).fetchone()
    if not row or not sub or not quote or quote not in row['excerpt']:
        raise ValueError('use the actual message quote and a confirmed submission')
    case = s.case_for_job(conn, sub['job_url'])
    if not case or case['id'] != row['case_id']:
        raise ValueError('message and application must identify the same case')
    result = record_event(conn, sub['job_url'], event_type, submission_id=submission_id,
                        occurred_at=row['received_at'], notes=quote, source='manual',
                        event_key='networking-outcome:'+message_key+':'+event_type,
                        payload={'referral_message_key': message_key, 'quote': quote}, trust='manual')

    with s.transaction(conn):
        conn.execute("UPDATE referral_messages SET state='reviewed',reviewed_at=? WHERE message_key=?", (s.iso(), message_key))
        conn.execute("UPDATE feedback_messages SET state='processed',submission_id=?,link_reason='manual_review',updated_at=? WHERE message_key=?", (submission_id, s.iso(), message_key))
    return result


def outcome_review(conn, message_key):
    """Do not train silence labels from unresolved hiring progress inside a networking thread."""
    row = conn.execute('SELECT * FROM referral_messages WHERE message_key=?', (message_key,)).fetchone()
    if not row or row['state'] != 'review':
        return []
    return [{'submission_id': r[0]} for r in conn.execute('''SELECT s.submission_id FROM feedback_submissions s
        JOIN referral_job_links l ON l.job_url=s.job_url WHERE l.case_id=?''', (row['case_id'],))]

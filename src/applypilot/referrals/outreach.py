"""Reviewed or standing-authorized, bounded email outbox. The read-only outcome collector never sends.

A stable Message-ID supports reconciliation; it is NOT a Gmail idempotency key.
After an uncertain send we only search/read. We never retry that send blindly.
"""
from __future__ import annotations

from applypilot.unattended.budgets import exhausted

from applypilot import operations as ops

import asyncio
import base64
import hashlib
import re
from datetime import timedelta
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from uuid import uuid4

from applypilot.referrals import store as s


def _contact(conn, cid):
    row = conn.execute('SELECT * FROM referral_contacts WHERE id=?', (cid,)).fetchone()
    if not row:
        raise ValueError('unknown professional contact')
    return dict(row)


def _outbox(conn, oid):
    row = conn.execute('SELECT * FROM referral_outbox WHERE id=?', (oid,)).fetchone()
    if not row:
        raise ValueError('unknown outreach item')
    return dict(row)


def retire_ineligible_draft(conn, row, decision):
    """Retain rejected automatic prose and its exact gate; never touch a send attempt."""
    with s.transaction(conn):
        changed = conn.execute("""UPDATE referral_outbox SET state='cancelled',approved_hash=NULL,
            error_kind=?,updated_at=? WHERE id=? AND attempted_at IS NULL AND dispatch_token IS NULL
            AND (state='draft' OR (state='approved' AND approved_by LIKE 'standing:%'))
            AND EXISTS(SELECT 1 FROM referral_generated g WHERE g.outbox_id=referral_outbox.id
                AND g.payload_hash=referral_outbox.payload_hash AND g.version=1)""",
            ('recipient_gate:'+decision['reason_code'], s.iso(), row['id'])).rowcount
        if changed:
            case, contact = s.require_case(conn,row['case_id']), _contact(conn,row['contact_id'])
            s.event(conn, row['case_id'], 'draft_retired',
                    contact_id=row['contact_id'] if contact['company_key'] == case['company_key'] else None,
                    quote=decision['reason'], trust='observed',
                    details={'outbox_id':row['id'], 'contact_id':row['contact_id'],
                             'gate':decision, 'payload':s.obj(row['payload_json'])})
    return bool(changed)


def _proofs(profile, job):
    from applypilot.profile import resume_evidence_entries
    wanted = set(re.findall(r'[a-z0-9]+', (str(job.get('title', ''))+' '+str(job.get('full_description', ''))).lower()))
    candidates = []
    for entry in resume_evidence_entries(profile):
        for text in entry['allowed_claims']:
            # Do not truncate a factual sentence, dropping a material qualifier.
            if len(text) > 450:
                continue
            overlap = len(wanted & set(re.findall(r'[a-z0-9]+', text.lower())))
            candidates.append((overlap, {'source_id': entry.get('id'), 'project': entry.get('title', 'Project'),
                                         'text': text, 'url': entry.get('url', '')}))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [p for score, p in candidates[:2] if score > 0]


def attachment_info(path):
    if not path:
        return None
    p = Path(str(path)).expanduser().resolve()
    if p.suffix.lower() not in {'.pdf', '.docx'} or not p.is_file():
        raise ValueError('attachment must be an existing PDF or DOCX resume')
    data = p.read_bytes()
    if not 0 < len(data) <= 5_000_000:
        raise ValueError('resume attachment must be nonempty and no more than 5 MB')
    if p.suffix.lower() == '.pdf' and not data.startswith(b'%PDF'):
        raise ValueError('resume is not a PDF')
    if p.suffix.lower() == '.docx' and not data.startswith(b'PK'):
        raise ValueError('resume is not a DOCX')
    return {'path': str(p), 'sha256': hashlib.sha256(data).hexdigest(), 'name': 'Resume'+p.suffix.lower()}


def basis(conn, case, contact):
    job = s.case_subject(conn, case)
    latest = conn.execute('SELECT MAX(received_at) FROM referral_messages WHERE case_id=? AND contact_id=?',
                          (case['id'], contact['id'])).fetchone()[0]
    return s.digest(s.dump({'job': {k: job.get(k) for k in ('url','title','company','route_target',
                     'route_version','discovery_assessment_revision','discovery_content_hash','target_revision','owner_instructions')},
                     'phase': s.application_phase(conn, case['id']), 'latest_reply': latest,
                     'hiring_revision': s.hiring_context(conn, case['id'])['revision'],
                     'declared_context': {k: s.obj(case['plan_json']).get(k) for k in ('job_function','job_team','requisition')},
                     'contact': {k: contact.get(k) for k in ('id','name','company_key','email','verified_at',
                                'relationship','relationship_note','role_function','team','profile_url','profile_key',
                                'kind','email_allowed','email_basis','permission_note','blocked_at')}}))


def owner_profile_hash(profile):
    """Bind generated prose to owner facts without storing credentials in the outbox."""
    personal=profile.get('personal') or {}
    safe={'personal':{k:personal.get(k) for k in ('full_name','email','portfolio_url','github_url')}}
    for section in ('availability','compensation','work_authorization','experience'):
        safe[section]=profile.get(section)
    return s.digest(s.dump(safe))


@ops.observe('outreach', 'compose', stage='outreach')
def compose(conn, cid, contact_id, profile, *, channel=None, kind='request', reply_to=None,
            text=None, include_resume=True, _template=None, _refresh=False):
    """Zero-LLM draft using selected verbatim approved project claims, or reviewed user text."""
    if s.policy(conn)['mode'] == 'off':
        raise ValueError('referrals disabled')
    template = _template or (kind if text is None else None)
    case, contact = s.require_case(conn, cid), _contact(conn, contact_id)
    from applypilot.targets.store import require_outreach
    require_outreach(conn, case)
    if contact['company_key'] != case['company_key'] or contact['blocked_at']:
        raise ValueError('verify contact employer and suppression status')
    phase = s.application_phase(conn, cid)
    if phase == 'in_flight':
        raise ValueError('application in progress/uncertain; do not create conflicting outreach')
    job = s.case_subject(conn, case)
    sender = s.email_address((profile.get('personal') or {}).get('email'))
    name = str((profile.get('personal') or {}).get('full_name') or '').strip()
    if not name:
        raise ValueError('profile sender name required')
    if channel is None:
        from applypilot.unattended.policy import preferred_channel
        channel = preferred_channel(conn, contact)
    channel = channel or ('email' if contact['email'] and contact['email_allowed'] else 'manual')
    if template and s.policy(conn)['mode'] == 'unattended':
        from applypilot.unattended.policy import require_recipient
        require_recipient(conn, case, contact, kind=kind, channel=channel)
    if channel not in {'email', 'manual', 'linkedin', 'x'}:
        raise ValueError('Unsupported professional outreach channel')
    if channel == 'email' and (not contact['email'] or not contact['email_allowed']):
        raise ValueError('email outreach permission is not recorded for this contact')
    if kind not in {'request', 'followup', 'reply'}:
        raise ValueError('unknown outreach kind')
    parent = None
    if kind == 'reply':
        parent = conn.execute('SELECT * FROM referral_messages WHERE message_key=? AND case_id=? AND contact_id=?',
                              (reply_to, cid, contact_id)).fetchone()
        if not parent or not text or not str(text).strip():
            raise ValueError('reply requires a matched inbound message and response text')
    previous = conn.execute("SELECT * FROM referral_outbox WHERE case_id=? AND contact_id=? AND state='sent' ORDER BY sent_at DESC LIMIT 1", (cid, contact_id)).fetchone()
    if kind == 'followup':
        if not previous or previous['kind'] != 'request':
            raise ValueError('only one follow-up after a sent initial request')
        if conn.execute('SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?', (cid, contact_id)).fetchone():
            raise ValueError('a reply arrived; compose a real response, not a follow-up')
        if s.utc(previous['sent_at']) > s.utc() - timedelta(days=s.policy(conn)['followup_after_days']):
            raise ValueError('follow-up not yet due')
    req = s.obj(case['plan_json']).get('requisition', {}).get('name')
    proofs = _proofs(profile, job) if kind == 'request' and not text else []
    if text is None:
        if kind == 'followup':
            text = (f'Hi {contact["name"]},\n\nA single follow-up on {job["title"]} at {job["company"]}. '
                    'No pressure if you are not able to help; I will not keep following up.\n\n'+name)
        else:
            action = (f'I have applied for {job["title"]} at {job["company"]}.' if phase == 'submitted'
                      else f'I am considering {job["title"]} at {job["company"]}.')
            if contact['kind'] in {'recruiter', 'hiring_manager'}:
                ask = 'Would you be comfortable reviewing my background and advising whether this is the right opportunity or team?'
            elif phase == 'submitted':
                ask = ('If you think the fit is appropriate, could you flag my application to the relevant recruiter? '
                       'If your process permits a referral after applying, I would appreciate guidance on that too.')
            else:
                ask = ('Would you be comfortable reviewing my background and considering a referral if you think it fits? '
                       'Please let me know whether your process needs a referral link or whether I should apply first.')
            proof_text = '\n'.join(f'- {p["project"]} (project): {p["text"]}' for p in proofs)
            text = (f'Hi {contact["name"]},\n\n{action}\n' + (f'Requisition: {req}\n' if req else '') + f'{case["base_target"]}\n\n'
                    + (f'Relevant project work:\n{proof_text}\n\n' if proof_text else '')
                    + ask + '\n\nNo pressure if you are unable to help.\n'+name)
    if channel in {'manual','linkedin','x'} and template == 'request':
        intro = f'I have applied for {job["title"]}' if phase == 'submitted' else f'I am considering {job["title"]}'
        label = f' ({req})' if req else ''
        if phase == 'submitted':
            ask = 'Would you consider flagging my application to the recruiter if the fit looks right?'
        elif contact['relationship'] == 'cold':
            ask = 'Would you review my background and, if it fits, advise on your referral process? No pressure.'
        else:
            ask = 'Would you be comfortable referring me? Should I apply first or wait for your referral link?'
        text = f'Hi {contact["name"]}, {intro}{label} at {job["company"]}. {ask}\n{case["base_target"]}\n{name}'
    if case.get('case_type') == 'company' and phase == 'company' and template in {'request', 'followup'}:
        opening = ('Following up once on my introduction.' if kind == 'followup' else
                   f'I am interested in contributing to {job["company"]}.')
        proof_text = '\n'.join(f'- {p["project"]}: {p["text"]}' for p in proofs)
        text = (f'Hi {contact["name"]},\n\n{opening}\n'
                + (f'\nRelevant work:\n{proof_text}\n' if proof_text else '')
                + '\nWould you be open to discussing where my background could be useful to your team?\n\n' + name)
    hiring = s.hiring_context(conn, cid)
    interview_stage = hiring['stage'] in {'screen','interview','technical_interview','interview_completed'}
    if template == 'request' and interview_stage:
        progress = ('I have completed an interview' if hiring['stage'] == 'interview_completed' else
                    'My application has reached the ' + hiring['stage'].replace('_', ' ') + ' stage')
        ask = ('Could you advise on the next steps in the hiring process?' if contact['kind'] in {'recruiter','hiring_manager'}
               else 'Would you be open to sharing advice about the team and the remaining hiring process?')
        text = f'Hi {contact["name"]}, {progress} for {job["title"]} at {job["company"]}. {ask}\n\n{name}'
    if template and s.policy(conn)['mode'] == 'unattended':
        text += f"\n\nSent by {name}'s job-search assistant."
    if not isinstance(text, str) or not text.strip():
        raise ValueError('outreach text must be nonempty')
    oid = s.digest(s.dump([cid, contact_id, kind, reply_to]))[:32]
    subject = (f'Opportunities at {job["company"]}' if phase == 'company' else
               f'{job["title"]} — ' + ('hiring process' if interview_stage else 'referral process') + f' at {job["company"]}')
    thread_id = parent['thread_id'] if parent else previous['thread_id'] if kind == 'followup' else None
    reply_rfc = parent['rfc_message_id'] if parent else previous['rfc_message_id'] if kind == 'followup' else None
    if parent and s.obj(parent['context_json']).get('channel','email') != channel:
        # A verified DM may invite email, but a browser URL is never a Gmail thread ID.
        thread_id = reply_rfc = None
    if kind == 'followup' and previous['channel'] != channel:
        thread_id = reply_rfc = None
    if parent:
        subject = parent['subject']
    elif kind == 'followup':
        subject = s.obj(previous['payload_json'])['subject']
    if any(ord(c) < 32 for c in subject) or len(subject) > 250:
        raise ValueError('invalid email subject')
    resume = None
    if channel == 'email' and include_resume and (kind == 'request' or _template == 'materials') and job.get('tailored_resume_path'):
        candidate = Path(job['tailored_resume_path'])
        if candidate.suffix.lower() not in {'.pdf', '.docx'}:
            candidate = candidate.with_suffix('.pdf')
        if candidate.is_file():
            resume = attachment_info(candidate)
    payload = {'from': sender, 'to': contact['email'] if channel == 'email' else (contact['profile_url'] or contact['email']),
               'subject': subject, 'body': text, 'attachment': resume, 'proofs': proofs,
               'phase': phase, 'thread_id': thread_id, 'reply_rfc': reply_rfc,
               'hiring_stage': hiring['stage'], 'hiring_revision': hiring['revision'],
               'purpose': 'interview_progress' if interview_stage else 'referral_request',
               'reply_message_key': reply_to, 'kind': kind, 'channel': channel,
               'relationship': contact['relationship'], 'requisition_id': req,
               'short_message_target': 400 if channel in {'manual','linkedin','x'} else None,
               'needs_shortening': channel in {'manual','linkedin','x'} and len(text) > 400,
               'alignment': next((c['alignment'] for c in s.obj(case['plan_json']).get('candidates', []) if c['contact_id'] == contact_id), 'unknown')}
    if template:
        payload['owner_profile_hash'] = owner_profile_hash(profile)
    stored_kind = kind + ':' + reply_to if kind == 'reply' else kind
    operation_run_id, operation_span_id = s.operation_ids()
    with s.transaction(conn):
        old = conn.execute('SELECT * FROM referral_outbox WHERE case_id=? AND contact_id=? AND kind=?',
                           (cid, contact_id, stored_kind)).fetchone()
        if old:
            recipient_corrected = (old['state'] == 'cancelled' and
                str(old['error_kind'] or '').startswith('recipient_gate:') and
                template and s.policy(conn)['mode'] == 'unattended')
            if not _refresh or old['attempted_at'] or old['dispatch_token'] or (
                    old['state'] not in {'draft','approved'} and not recipient_corrected):
                return dict(old)  # Never replace a sent/cancelled/uncertain message.
            if not conn.execute('SELECT 1 FROM referral_generated WHERE outbox_id=?', (old['id'],)).fetchone():
                raise ValueError('manual draft needs human review, not automatic rewriting')
            oid = old['id']
            conn.execute('''UPDATE referral_outbox SET payload_json=?,payload_hash=?,basis_hash=?,channel=?,
                state='draft',approved_hash=NULL,approved_by=NULL,approved_at=NULL,error_kind=NULL,updated_at=? WHERE id=?''',
                (s.dump(payload), s.digest(s.dump(payload)), basis(conn,case,contact),channel,s.iso(),oid))
        else:
            conn.execute('''INSERT INTO referral_outbox
            (id,case_id,contact_id,kind,channel,payload_json,payload_hash,basis_hash,rfc_message_id,
             created_run_id,created_span_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (oid, cid, contact_id, stored_kind, channel, s.dump(payload), s.digest(s.dump(payload)),
             basis(conn, case, contact), f'<applypilot-referral-{oid}@outreach.invalid>',
             operation_run_id,operation_span_id,s.iso(),s.iso()))
        if template:
            conn.execute('''INSERT INTO referral_generated VALUES(?,?,?,?,?)
                ON CONFLICT(outbox_id) DO UPDATE SET payload_hash=excluded.payload_hash,
                template=excluded.template,version=excluded.version,created_at=excluded.created_at''',
                (oid, s.digest(s.dump(payload)), template, 1, s.iso()))
        conn.execute("UPDATE referral_cases SET state='draft',updated_at=? WHERE id=? AND state='research'", (s.iso(), cid))
    return _outbox(conn, oid)


def revise(conn, oid, text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('invalid outreach text')
    with s.transaction(conn):
        row = _outbox(conn, oid)
        if row['state'] not in {'draft', 'approved', 'cancelled'}:
            raise ValueError('never rewrite sent/uncertain outreach')
        contact, case = _contact(conn, row['contact_id']), s.require_case(conn, row['case_id'])
        payload = s.obj(row['payload_json'])
        phase = s.application_phase(conn, case['id'])
        if phase == 'in_flight':
            raise ValueError('application in flight')
        conn.execute('DELETE FROM referral_generated WHERE outbox_id=?', (oid,))
        payload.update(body=text, proofs=[], phase=phase, relationship=contact['relationship'],
                       needs_shortening=row['channel'] in {'manual','linkedin','x'} and len(text) > 400,
                       alignment=next((c['alignment'] for c in s.obj(case['plan_json']).get('candidates', []) if c['contact_id'] == contact['id']), 'unknown'))
        conn.execute("UPDATE referral_outbox SET payload_json=?,payload_hash=?,basis_hash=?,state='draft',approved_hash=NULL,approved_by=NULL,approved_at=NULL,updated_at=? WHERE id=?",
                     (s.dump(payload), s.digest(s.dump(payload)), basis(conn, case, contact), s.iso(), oid))
    return _outbox(conn, oid)


def approve(conn, oid, expected_hash, *, actor, profile=None):
    """Approval binds exact recipient, text, document hash, phase and contact facts."""
    if not str(actor).strip():
        raise ValueError('approval actor required')
    with s.transaction(conn):
        row = _outbox(conn, oid)
        if row['state'] != 'draft' or row['payload_hash'] != expected_hash:
            raise ValueError('review this exact current draft before approving')
        _validate_current(conn, row, profile=profile)
        conn.execute("UPDATE referral_outbox SET state='approved',approved_hash=payload_hash,approved_at=?,approved_by=?,updated_at=? WHERE id=?",
                     (s.iso(), actor, s.iso(), oid))
        if actor.startswith('standing:'):
            from applypilot.referrals.autonomy import grant
            g = grant(conn)
            if g and g['rules'].get('version') == 2:
                from applypilot.unattended.grace import schedule
                from applypilot.unattended.settings import load
                schedule(conn, _outbox(conn, oid), load(), g['id'])
    return _outbox(conn, oid)


def _validate_current(conn, row, *, profile=None):
    cfg, contact, case = s.policy(conn), _contact(conn, row['contact_id']), s.require_case(conn, row['case_id'])
    from applypilot.targets.store import require_outreach
    require_outreach(conn, case)
    if row['basis_hash'] != basis(conn, case, contact):
        raise ValueError('Outreach context changed; regenerate the draft')
    if cfg['mode'] == 'off' or contact['blocked_at']:
        raise ValueError('outreach disabled/suppressed')
    if contact['company_key'] != case['company_key']:
        raise ValueError('contact employer changed')
    payload = s.obj(row['payload_json'])
    if payload.get('phase') != s.application_phase(conn, case['id']):
        raise ValueError('Application phase changed; refresh the draft')
    if row['kind'] in {'request', 'followup'} and conn.execute(
            'SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?',
            (case['id'], contact['id'])).fetchone():
        raise ValueError('A reply arrived; respond in the thread instead of sending an introduction')
    if s.digest(s.dump(payload)) != row['payload_hash']:
        raise ValueError('outreach content changed')
    if row['channel'] == 'email' and (not contact['email_allowed'] or payload['to'] != contact['email']):
        raise ValueError('recipient or email permission changed')
    if row['channel'] in {'manual','linkedin','x'} and payload.get('to') != (contact['profile_url'] or contact['email']):
        raise ValueError('manual recipient changed; review again')
    if contact.get('profile_key') and conn.execute('SELECT 1 FROM referral_contacts WHERE profile_key=? AND blocked_at IS NOT NULL', (contact['profile_key'],)).fetchone():
        raise ValueError('professional identity is suppressed')
    if payload.get('attachment') and attachment_info(payload['attachment']['path']) != payload['attachment']:
        raise ValueError('resume attachment changed; review again')
    if profile is not None:
        sender = s.email_address((profile.get('personal') or {}).get('email'))
        if sender != payload['from']:
            raise ValueError('sender profile changed')
        from applypilot.profile import resume_evidence_entries
        allowed = {(str(e.get('id')), c) for e in resume_evidence_entries(profile) for c in e['allowed_claims']}
        if any((str(p['source_id']), p['text']) not in allowed for p in payload.get('proofs', [])):
            raise ValueError('selected evidence revoked/changed; review again')
    return payload, contact, case


def raw_message(row, payload):
    """Generate exact approved MIME bytes, with no CC/BCC/tracking pixel."""
    # Keep the stable Message-ID on its physical header line.  The default
    # 78-character fold can leave a leading space when the MIME is parsed,
    # breaking exact sent-message reconciliation on supported Python 3.11.
    msg = EmailMessage(policy=SMTP.clone(max_line_length=998))
    if str(row.get('approved_by') or '').startswith('standing:'):
        msg['Auto-Submitted'] = 'auto-replied' if row['kind'].startswith('reply:') else 'auto-generated'
        msg['X-Auto-Response-Suppress'] = 'All'
    msg['From'], msg['To'], msg['Subject'] = payload['from'], payload['to'], payload['subject']
    msg['Message-ID'] = row['rfc_message_id']
    if payload.get('reply_rfc'):
        if not re.fullmatch(r'<[^\s<>]+@[^\s<>]+>', payload['reply_rfc']):
            raise ValueError('invalid thread Message-ID')
        msg['In-Reply-To'] = payload['reply_rfc']
        msg['References'] = payload['reply_rfc']
    msg.set_content(payload['body'])
    attachment = payload.get('attachment')
    if attachment:
        data = Path(attachment['path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != attachment['sha256']:
            raise ValueError('attachment changed')
        subtype = 'pdf' if attachment['name'].endswith('.pdf') else 'vnd.openxmlformats-officedocument.wordprocessingml.document'
        msg.add_attachment(data, maintype='application', subtype=subtype, filename=attachment['name'])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode('ascii')


class ReferralMailbox:
    """Explicit sender wrapper; FeedbackMailbox's read-only allowlist is unchanged."""
    def __init__(self, mailbox, *, allow_send=True):
        self.mailbox = mailbox
        self.address = mailbox.address
        self.mailbox_key = mailbox.mailbox_key
        self.allow_send = allow_send
        if allow_send and 'send_message' not in mailbox.tools:
            raise ValueError('Gmail MCP send_message(raw,threadId) capability unavailable')

    async def send(self, raw, thread_id=None):
        if not self.allow_send:
            raise ValueError('reconciliation is read-only')
        from applypilot.tracking.gmail_client import _decode_tool_json
        args = {'raw': raw}
        if thread_id:
            args['threadId'] = thread_id
        result = await asyncio.wait_for(self.mailbox.session.call_tool('send_message', arguments=args), timeout=120)
        value = _decode_tool_json(result)
        if not isinstance(value.get('id'), str) or not value['id']:
            raise ValueError('Gmail did not confirm the send')
        return value

    async def find_sent(self, rfc_id):
        if not re.fullmatch(r'<applypilot-referral-[0-9a-f]{32}@outreach.invalid>', rfc_id):
            raise ValueError('invalid reconciliation Message-ID')
        result = await self.mailbox._read('list_messages',
            {'q': 'in:sent rfc822msgid:'+rfc_id[1:-1], 'maxResults': 10})
        if result.get('nextPageToken'):
            raise ValueError('ambiguous send reconciliation')
        return [await self.mailbox.get(x['id']) for x in result.get('messages', [])]


def _check_outreach_limits(conn, row, contact, case):
    """Caller must hold the write transaction. Applies across manual and email channels."""
    conn.execute('SELECT id FROM referral_settings WHERE id=1 FOR UPDATE').fetchone()
    cfg, oid = s.policy(conn), row['id']
    day_start = s.iso(s.utc().replace(hour=0, minute=0, second=0, microsecond=0))
    used = conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE attempted_at>=?", (day_start,)).fetchone()[0]
    if exhausted(used, cfg['daily_send_limit']):
        raise ValueError('daily outreach budget exhausted')
    if row['kind'] == 'request':
        previous = conn.execute("SELECT * FROM referral_outbox WHERE case_id=? AND attempted_at IS NOT NULL AND kind='request' AND id!=?", (case['id'], oid)).fetchall()
        if exhausted(len({r['contact_id'] for r in previous}), cfg['contacts_per_job']):
            raise ValueError('per-posting contact budget exhausted')
        if conn.execute("SELECT 1 FROM referral_outbox WHERE (contact_id=? OR contact_id IN (SELECT id FROM referral_contacts WHERE profile_key=? AND profile_key IS NOT NULL)) AND attempted_at>? AND id!=?",
                        (contact['id'], contact.get('profile_key'), s.iso(s.utc()-timedelta(days=cfg['contact_cooldown_days'])), oid)).fetchone():
            raise ValueError('contact cooling off across all jobs')
        for old in previous:
            declined = conn.execute("SELECT 1 FROM referral_events WHERE case_id=? AND contact_id=? AND kind IN ('declined','opt_out','delivery_failed') AND voided_at IS NULL",
                                    (case['id'], old['contact_id'])).fetchone()
            if not declined and (old['state'] in {'sending','uncertain'} or
                s.utc(old['attempted_at']) > s.utc()-timedelta(days=cfg['followup_after_days'])):
                raise ValueError('one primary contact at a time; fallback is not yet due')
        if conn.execute("SELECT 1 FROM referral_events WHERE case_id=? AND kind IN ('referral_offered','referral_claimed') AND voided_at IS NULL", (case['id'],)).fetchone():
            raise ValueError('An employee has offered/reported assistance; do not contact another employee')
        if conn.execute("SELECT 1 FROM referral_events WHERE case_id=? AND trust='operator-confirmed' AND kind IN ('ats_referral','employee_endorsement','recruiter_intro','hiring_manager_intro','resume_forwarded','application_flagged') AND voided_at IS NULL", (case['id'],)).fetchone():
            raise ValueError('assistance already confirmed; do not contact another employee')


def _reserve_send(conn, oid, sender, *, profile, expected_hash):
    operation_run_id, operation_span_id = s.operation_ids()
    with s.transaction(conn):
        cfg = s.policy(conn)
        row = dict(conn.execute('SELECT * FROM referral_outbox WHERE id=? FOR UPDATE', (oid,)).fetchone())
        if cfg['mode'] not in {'active','unattended'} or row['state'] != 'approved' or row['channel'] != 'email':
            raise ValueError('only currently authorized email in active/unattended mode may be sent')
        if row['approved_hash'] != row['payload_hash'] or row['payload_hash'] != expected_hash:
            raise ValueError('approval invalidated')
        payload, contact, case = _validate_current(conn, row, profile=profile)
        if str(row.get('approved_by') or '').startswith('standing:'):
            from .autonomy import validate_automatic
            validate_automatic(conn, row, profile=profile, for_send=True)
        if payload['from'] != sender.address or sender.mailbox_key != s.digest(sender.address):
            raise ValueError('connected Gmail account differs from approved sender')
        _check_outreach_limits(conn, row, contact, case)
        token = s.digest(s.dump(['email', oid, expected_hash]))
        conn.execute("UPDATE referral_outbox SET state='sending',dispatch_token=?,attempted_at=?,mailbox_key=?,dispatch_run_id=?,dispatch_span_id=?,updated_at=? WHERE id=?",
                     (token, s.iso(), sender.mailbox_key, operation_run_id, operation_span_id, s.iso(), oid))
    return row, payload, token


def _confirm_send(conn, oid, token, response, *, at=None):
    if not isinstance(response, dict) or not isinstance(response.get('id'), str) or not response['id']:
        raise ValueError('send not confirmed by the delivery transport')
    with s.transaction(conn):
        row = _outbox(conn, oid)
        if row['dispatch_token'] != token or row['state'] not in {'sending', 'uncertain'}:
            raise ValueError('Send operation identity or status changed')
        when = s.iso(at or row['attempted_at'])
        conn.execute("UPDATE referral_outbox SET state='sent',sent_at=?,message_id=?,thread_id=?,error_kind=NULL,updated_at=? WHERE id=?",
                     (when, response['id'], response.get('threadId'), s.iso(), oid))
        contact = _contact(conn, row['contact_id'])
        case = s.require_case(conn, row['case_id'])
        frozen = s.obj(row['payload_json'])
        if str(row.get('approved_by') or '').startswith('standing:') and frozen.get('reply_message_key'):
            conn.execute("UPDATE referral_auto_messages SET status='sent',updated_at=? WHERE message_key=?", (s.iso(),frozen['reply_message_key']))
            conn.execute("UPDATE referral_messages SET state='handled',reviewed_at=? WHERE message_key=?", (s.iso(),frozen['reply_message_key']))
        s.event(conn, case['id'], 'outreach_sent', contact_id=contact['id'], at=when,
                key='outbox:'+oid, trust='observed', details={
                    'outbox_id': oid, 'relationship': frozen['relationship'],
                    'proof_ids': [p['source_id'] for p in frozen.get('proofs', []) if p.get('source_id')],
                    'alignment': frozen['alignment'], 'channel': row['channel'],
                    'phase': s.obj(row['payload_json'])['phase'], 'kind': row['kind']})
        conn.execute("UPDATE referral_cases SET state='waiting',updated_at=? WHERE id=? AND state IN ('research','draft')", (s.iso(), case['id']))


async def send_message(conn, sender, outreach_id, *, profile):
    """Send one approved intent, or reconcile its stable Message-ID on retry."""
    row = _outbox(conn, outreach_id)
    if row['state'] == 'sent':
        return {'state': 'sent', 'message_id': row['message_id']}
    if row['state'] in {'sending', 'uncertain'}:
        status = await reconcile(conn, sender, outreach_id)
        return {'state': status, 'message_id': _outbox(conn, outreach_id).get('message_id')}
    raw = raw_message(row, s.obj(row['payload_json']))
    from applypilot.execution import check_cancelled
    check_cancelled()
    row, payload, operation_id = _reserve_send(conn, outreach_id, sender,
        profile=profile, expected_hash=row['payload_hash'])
    try:
        response = await sender.send(raw, payload.get('thread_id'))
        _confirm_send(conn, outreach_id, operation_id, response)
        return {'state': 'sent', 'message_id': response['id']}
    except BaseException as exc:
        with s.transaction(conn):
            conn.execute("""UPDATE referral_outbox SET state='uncertain',error_kind=?,updated_at=?
                WHERE id=? AND dispatch_token=? AND state='sending'""",
                (type(exc).__name__, s.iso(), outreach_id, operation_id))
        raise


def delivery_status(result):
    """Zero output is idle/deferred; only confirmation contributes to sent."""
    result.pop('no_work', None)
    if result.get('uncertain'):
        result.update(status='partial', reason_code='delivery_uncertain')
    elif result.get('deferred'):
        result.update(status='partial' if result.get('sent') else 'deferred', reason_code='delivery_gates')
    elif result.get('sent'):
        result.update(status='complete', reason_code='delivery_confirmed')
    elif result.get('eligible'):
        result.update(status='deferred', reason_code='delivery_not_attempted')
    else:
        result.update(status='empty', no_work=True, reason_code='no_due_outreach')
    return result


async def reconcile(conn, sender, oid):
    row = _outbox(conn, oid)
    payload = s.obj(row['payload_json'])
    if row['state'] == 'sent':
        return 'sent'
    if row['state'] not in {'sending', 'uncertain'} or row['mailbox_key'] != sender.mailbox_key:
        raise ValueError('reconcile only an uncertain send on the original mailbox')
    matches = await sender.find_sent(row['rfc_message_id'])
    matches = [m for m in matches if m.get('sender') == payload['from'] and
               m.get('rfc_message_id') == row['rfc_message_id'] and
               m.get('subject') == payload['subject'] and
               m.get('to','').strip().casefold() == payload['to'].casefold() and
               m.get('body','').replace('\r\n', '\n').strip() == payload['body'].replace('\r\n', '\n').strip() and 'SENT' in m.get('labels', [])]
    if len(matches) != 1:
        return 'uncertain'  # search absence is not proof of non-delivery
    match = matches[0]
    _confirm_send(conn, oid, row['dispatch_token'], {'id': match['id'], 'threadId': match.get('thread_id')}, at=match.get('date'))
    return 'sent'


def record_manual_send(conn, oid, expected_hash, *, reference):
    """For manually sent LinkedIn/X/email; recording is not permission to automate UI."""
    if not str(reference).strip():
        raise ValueError('manual send requires your reference')
    operation_run_id, operation_span_id = s.operation_ids()
    with s.transaction(conn):
        row = _outbox(conn, oid)
        if row['state'] != 'approved' or row['payload_hash'] != expected_hash or row['approved_hash'] != expected_hash:
            raise ValueError('record the approved exact item only')
        payload, contact, case = _validate_current(conn, row)
        _check_outreach_limits(conn, row, contact, case)
        conn.execute("UPDATE referral_outbox SET state='sent',sent_at=?,attempted_at=?,dispatch_run_id=?,dispatch_span_id=?,updated_at=? WHERE id=?",
                     (s.iso(), s.iso(), operation_run_id, operation_span_id, s.iso(), oid))
        s.event(conn, case['id'], 'manual_outreach', contact_id=contact['id'], quote=reference,
                key='outbox:'+oid, details={'outbox_id': oid,
                                         'proof_ids': [p['source_id'] for p in payload.get('proofs', []) if p.get('source_id')],
                                         'relationship': payload['relationship'], 'alignment': payload['alignment'], 'channel': row['channel'],
                                         'phase': payload['phase'], 'kind': row['kind']})


def referral_packet(conn, cid, profile):
    """Reviewable materials, separate from a short first-contact message."""
    case = s.require_case(conn, cid)
    job = s.case_subject(conn, case)
    plan = s.obj(case['plan_json'])
    resume = None
    path = job.get('tailored_resume_path')
    if path:
        candidate = Path(path)
        if candidate.suffix.lower() not in {'.pdf', '.docx'}:
            candidate = candidate.with_suffix('.pdf')
        if candidate.is_file():
            resume = attachment_info(candidate)
    return {'case_id': cid, 'job_title': job['title'], 'company': job['company'],
            'job_url': case['base_target'], 'requisition': plan.get('requisition') or None,
            'application_phase': s.application_phase(conn, cid),
            'tailored_resume': resume, 'missing_materials': [] if resume else ['tailored_resume'],
            'fit_evidence': _proofs(profile, job), 'candidates': plan.get('candidates', []),
            'ask': 'Ask which referral procedure and application sequence this employer requires.',
            'sent': 0}

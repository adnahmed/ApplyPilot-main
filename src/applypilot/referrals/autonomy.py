"""Standing permission for bounded routine email, not an unrestricted browser agent.

No model or network calls in these policy decisions. Inbox text supplies facts,
never permission, recipient addresses, local paths, tools, or new operating rules.
"""
from __future__ import annotations

from applypilot.unattended.budgets import exhausted

import re
from datetime import timedelta
from urllib.parse import urlsplit, parse_qsl
from uuid import uuid4

from . import store as s
from . import outreach as out

TEMPLATES = {'request', 'followup', 'materials', 'application_status', 'acknowledge', 'procedure_ack', 'contextual_reply', 'decline_feedback'}
DIRECT_RELATIONSHIPS = {'knows_work', 'professional', 'acquaintance'}


def grant(conn):
    row = conn.execute('SELECT * FROM referral_auto_grants WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1').fetchone()
    return dict(row, rules=s.obj(row['rules_json'])) if row else None


def authorize(conn, sender, *, actor='owner', daily_limit=5, followups=True, replies=True, routes=True):
    """One explicit operator action; future in-scope messages need no approval click."""
    sender = s.email_address(sender)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 100:
        raise ValueError('authorization requires an actor')
    from applypilot.unattended.budgets import validate_limit
    validate_limit(daily_limit, 'daily_limit')
    if any(type(x) is not bool for x in (followups, replies, routes)):
        raise ValueError('authorization switches must be explicit booleans')
    rules = {'version': 1, 'daily_limit': daily_limit, 'followups': followups,
             'routine_replies': replies, 'same_posting_routes': routes,
             'max_reply_rounds': 4, 'min_reply_minutes': 10,
             'channels': ['email'], 'new_recipients': 'known_relationship_or_explicit_invitation'}
    gid = uuid4().hex
    with s.transaction(conn):
        conn.execute('UPDATE referral_auto_grants SET revoked_at=? WHERE revoked_at IS NULL', (s.iso(),))
        conn.execute('INSERT INTO referral_auto_grants VALUES(?,?,?,?,?,NULL)',
                     (gid, sender, actor, s.dump(rules), s.iso()))
        conn.execute("UPDATE referral_outbox SET state='draft',approved_hash=NULL WHERE state='approved' AND approved_by LIKE 'standing:%'")
        s.configure(conn, mode='unattended', daily_send_limit=daily_limit)
    return {'grant_id': gid, 'sender': sender, 'rules': rules, 'mode': 'unattended', 'sent': 0}


def contact_allowed(contact):
    """Owner's permission AND a documented professional relationship/invitation."""
    if contact.get('blocked_at') or not contact.get('email_allowed') or not contact.get('email'):
        return False
    basis = contact.get('email_basis', 'manual_only')
    return ((basis == 'existing_relationship' and contact['relationship'] in DIRECT_RELATIONSHIPS
             and bool(contact.get('relationship_note','').strip()))
            or (basis == 'invited_referrals' and bool(contact.get('permission_note','').strip())))


def validate_automatic(conn, row, *, profile, for_send=False):
    g = grant(conn)
    if not g or s.policy(conn)['mode'] != 'unattended':
        raise ValueError('no active standing authorization')
    if g['rules'].get('version') == 2:
        from applypilot.unattended.policy import validate_automatic as validate_v2
        return validate_v2(conn, row, profile=profile, for_send=for_send)
    if g['rules'].get('version') != 1:
        raise ValueError('unsupported standing-authorization version')
    if conn.execute("SELECT 1 FROM referral_outbox WHERE mailbox_key=? AND state IN ('sending','uncertain')", (s.digest(g['sender']),)).fetchone():
        raise ValueError('mailbox has uncertain delivery; read-only reconciliation required')
    if for_send and row['approved_by'] != 'standing:' + g['id']:
        raise ValueError('standing authorization changed or revoked')
    generated = conn.execute('SELECT * FROM referral_generated WHERE outbox_id=?', (row['id'],)).fetchone()
    if not generated or generated['payload_hash'] != row['payload_hash'] or generated['version'] != 1:
        raise ValueError('manual or edited draft requires explicit review')
    if generated['template'] not in TEMPLATES:
        raise ValueError('unrecognized automatic message template')
    payload, contact, case = out._validate_current(conn, row, profile=profile)
    if row['channel'] != 'email' or payload['from'] != g['sender'] or not contact_allowed(contact):
        raise ValueError('recipient/channel outside standing permission')
    if row['kind'] in {'request','followup'}:
        # A referral is not useful grounds for automatically reopening a rejected
        # or withdrawn application. Consume observed status without mutating it.
        from applypilot.tracking.feedback_store import current_state
        sub=conn.execute('''SELECT f.* FROM feedback_submissions f JOIN referral_job_links l
            ON l.job_url=f.job_url WHERE l.case_id=? ORDER BY f.submitted_at DESC LIMIT 1''',(case['id'],)).fetchone()
        if sub:
            events=conn.execute('SELECT * FROM application_outcomes WHERE submission_identifier=?',(sub['submission_id'],)).fetchall()
            if current_state(events,submitted_at=sub['submitted_at']).get('terminal'):
                raise ValueError('application has an observed terminal outcome; no automatic solicitation')
    if row['kind'] == 'followup' and not g['rules']['followups']:
        raise ValueError('automatic follow-ups not authorized')
    if row['kind'].startswith('reply:'):
        if not g['rules']['routine_replies']:
            raise ValueError('automatic replies not authorized')
        parent = conn.execute('SELECT * FROM referral_messages WHERE message_key=?', (payload.get('reply_message_key'),)).fetchone()
        if not parent or parent['mailbox_key'] != s.digest(g['sender']) or parent['sender'] != contact['email']:
            raise ValueError('reply origin/account mismatch')
        if s.utc(parent['received_at']) > s.utc() - timedelta(minutes=g['rules']['min_reply_minutes']):
            raise ValueError('reply settling interval')
        replies = conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE case_id=? AND contact_id=? AND kind LIKE 'reply:%' AND attempted_at IS NOT NULL",
                               (case['id'], contact['id'])).fetchone()[0]
        if replies >= g['rules']['max_reply_rounds']:
            raise ValueError('automatic conversation limit reached')
        pending = conn.execute("SELECT 1 FROM referral_outbox WHERE contact_id=? AND id!=? AND state IN ('sending','uncertain')",
                               (contact['id'], row['id'])).fetchone()
        if pending:
            raise ValueError('previous delivery to this contact is uncertain')
    start = s.iso(s.utc().replace(hour=0, minute=0, second=0, microsecond=0))
    used = conn.execute('SELECT COUNT(*) FROM referral_outbox WHERE attempted_at>=?', (start,)).fetchone()[0]
    if exhausted(used, g['rules']['daily_limit'], s.policy(conn)['daily_send_limit']):
        raise ValueError('standing daily allowance exhausted')
    return payload, contact, case


def _finish_message(conn, key, status, reason, oid=None):
    conn.execute('''INSERT INTO referral_auto_messages VALUES(?,?,?,?,?)
        ON CONFLICT(message_key) DO UPDATE SET status=excluded.status,reason=excluded.reason,
        outbox_id=excluded.outbox_id,updated_at=excluded.updated_at''', (key,status,reason,oid,s.iso()))
    if status in {'ignored','handled'}:
        conn.execute("UPDATE referral_messages SET state='handled',reviewed_at=? WHERE message_key=?", (s.iso(),key))


def _body_lines(text):
    """Remove only salutations/sign-offs. Unknown substantive clauses stay unknown."""
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if lines and re.fullmatch(r'(?:hi|hello|dear)(?: [\w .\'-]{1,80})?[,!:]?', lines[0], re.I):
        lines.pop(0)
    for i, line in enumerate(lines):
        if re.fullmatch(r'(?:best(?: regards)?|regards|kind regards|sincerely|cheers)[,!]?', line, re.I):
            return ' '.join(lines[:i])
    return ' '.join(lines)


def _routine(text):
    """Conservative complete-clause grammar. Arbitrary questions are exceptions.

    This is intentionally not a probabilistic 'confidence > .9' send permission.
    Unknown wording remains reviewable rather than inventing an answer or promise.
    """
    body = _body_lines(text)
    urls = re.findall(r'https://[^\s<>"\)]+', body)
    for url in urls:
        body = body.replace(url, 'LINK')
    clauses = [c.strip(' .!?') for c in re.split(r'[.!?]+\s*|\n+', body) if c.strip(' .!?')]
    kinds = []
    for c in clauses:
        if re.fullmatch(r"(?:thank(?:s| you)(?: so much| for (?:your (?:message|email)|the information))?|no problem|you're welcome|happy to help)", c, re.I):
            kinds.append('courtesy'); continue
        if re.fullmatch(r"(?:i(?:'ve| have)? (?:already )?(?:referred you|submitted (?:your|the) referral)|(?:i(?:'m| am) )?happy to refer you|i (?:can|will) refer you)", c, re.I):
            kinds.append('acknowledge'); continue
        if re.fullmatch(r"(?:please |could you (?:please )?|can you (?:please )?|would you (?:please )?)(?:send|share|provide) (?:me )?(?:your |the )?(?:resume|résumé|cv)(?: (?:and|with) (?:the )?(?:job (?:link|url|id)|requisition id|posting))?(?:,? please)?", c, re.I):
            kinds.append('materials'); continue
        if re.fullmatch(r"(?:have you (?:already )?applied(?: (?:for|to) (?:this|the) (?:role|job))?|did you (?:already )?apply(?: (?:for|to) (?:this|the) (?:role|job))?)", c, re.I):
            kinds.append('application_status'); continue
        if re.fullmatch(r'(?:please )?apply (?:using|through|via|at) (?:this (?:referral )?link:? )?LINK', c, re.I):
            kinds.append('procedure_ack'); continue
        return None, urls
    substantive = set(kinds) - {'courtesy'}
    if not substantive:
        return 'courtesy', urls
    if substantive <= {'materials','acknowledge'}:
        return ('materials' if 'materials' in substantive else 'acknowledge'), urls
    if len(substantive) == 1:
        return substantive.pop(), urls
    return None, urls


def _same_posting_route(conn, case, person, target, quote):
    """Only exact known-ATS posting, same HTTPS host; never follow mail links."""
    target = s.public_url(target)
    p, original = urlsplit(target), urlsplit(case['base_target'])
    if p.hostname != original.hostname or p.fragment:
        raise ValueError('new host/fragment requires route review')
    if any(re.search(r'(redirect|return|next|url|continue|callback)', k, re.I) for k,v in parse_qsl(p.query)):
        raise ValueError('redirect parameter needs review')
    expected = s.posting_key({'url':case['job_url'],'route_target':case['base_target']})
    actual = s.posting_key({'url':case['job_url'],'route_target':target})
    known = expected.startswith(('greenhouse:','jobs.lever.co:','jobs.eu.lever.co:','jobs.ashbyhq.com:'))
    if known and expected != actual:
        raise ValueError('same exact ATS posting is not established')
    if not known:
        # Workday/SAP/custom exact URL paths: preserve every original query pair
        # and accept only referral-attribution additions, never a new resource.
        original_pairs = set(parse_qsl(original.query, keep_blank_values=True))
        target_pairs = set(parse_qsl(p.query, keep_blank_values=True))
        permitted = {'source','ref','referral','referrer','referrerid','referralid','gh_src','lever-source','lever-origin'}
        if p.path.rstrip('/') != original.path.rstrip('/') or not original_pairs <= target_pairs or any(
                k.casefold() not in permitted for k,v in target_pairs-original_pairs):
            raise ValueError('same posting and attribution-only parameters are not established')
    s.approve_route(conn, case['id'], target=target, mechanism='referral_link', evidence=quote,
                    contact_id=person['id'], confirmed_same_posting=True, trust='policy-verified')


class PendingInput(ValueError):
    """A local artifact/application can become ready without a human decision."""


def _reply_text(conn, case, person, message, template, urls, profile, g):
    phase=s.application_phase(conn,case['id'])
    if phase=='in_flight':
        raise PendingInput('application in flight; outcome not yet known')
    if template=='procedure_ack':
        if not g['rules']['same_posting_routes'] or len(urls)!=1:
            raise ValueError('route review required')
        if phase!='before':
            text='Thank you. I have already applied for this role. Does your process allow the referral to be linked to my existing application?'
        else:
            if case['approved_target']!=urls[0]:
                _same_posting_route(conn,case,person,urls[0],message['excerpt'])
            text='Thank you for the referral link. My application will use this link for the same posting; I have not submitted it yet.'
    elif urls:
        raise ValueError('unexpected link in routine request')
    elif template=='materials':
        # Do not create a draft promising a nonexistent attachment. Retry later.
        from pathlib import Path
        job=s.case_subject(conn,case)
        path=job.get('tailored_resume_path')
        if not path:
            raise PendingInput('requested resume is not ready')
        candidate=Path(path)
        if candidate.suffix.lower() not in {'.pdf','.docx'}:
            candidate=candidate.with_suffix('.pdf')
        if not candidate.is_file():
            raise PendingInput('requested resume is not ready')
        out.attachment_info(candidate)
        text=f"Thank you. My resume is attached. The role is {case['title']}:\n{case['base_target']}"
    elif template=='application_status':
        text=('I have already submitted my application for this role.' if phase=='submitted' else
              'I have not submitted my application for this role yet. Please let me know whether your referral process requires a particular link before applying.')
    else:
        text='Thank you for your help with this opportunity. I appreciate your support.'
    return text+'\n\n'+str(profile.get('personal',{}).get('full_name',''))


def process_messages(conn, profile, *, limit=50, case_id=None):
    g = grant(conn)
    result = {'drafted':0, 'handled':0, 'exceptions':0}
    if not g or s.policy(conn)['mode'] != 'unattended':
        return result
    if g['rules'].get('version') == 2:
        from applypilot.unattended.conversations import process
        return process(conn, profile, limit=limit, case_id=case_id)
    rows = conn.execute('''SELECT m.* FROM referral_messages m WHERE m.state='review' AND (? IS NULL OR m.case_id=?)
        AND NOT EXISTS(SELECT 1 FROM referral_auto_messages a WHERE a.message_key=m.message_key AND a.status!='waiting')
        ORDER BY m.received_at DESC,m.message_key LIMIT ?''', (case_id, case_id, limit)).fetchall()
    for raw in rows:
        m = dict(raw)
        with s.transaction(conn):
            # Idempotence under competing cycles.
            if conn.execute("SELECT 1 FROM referral_auto_messages WHERE message_key=? AND status!='waiting'", (m['message_key'],)).fetchone():
                continue
            try:
                if not m['case_id'] or not m['contact_id']:
                    raise ValueError('ambiguous application/contact')
                case, person = s.require_case(conn,m['case_id']), out._contact(conn,m['contact_id'])
                ctx = s.obj(m.get('context_json'))
                if ctx.get('match_reason') != 'verified_contact_thread' or m['mailbox_key'] != s.digest(g['sender']):
                    raise ValueError('no verified conversation on authorized mailbox')
                if m['sender'] != person['email'] or not contact_allowed(person):
                    raise ValueError('sender outside standing permission')
                if ctx.get('reply_to') and ctx['reply_to'] != person['email']:
                    raise ValueError('different Reply-To requires review')
                if ctx.get('from_truncated'):
                    raise ValueError('incomplete message')
                if ctx.get('auto_submitted','').casefold() not in ('','no') or ctx.get('precedence','').casefold() in {'bulk','list','junk'}:
                    _finish_message(conn,m['message_key'],'ignored','automatic/list message'); result['handled']+=1; continue
                if set(ctx.get('labels',[])) & {'SPAM','TRASH','SENT','DRAFT'}:
                    _finish_message(conn,m['message_key'],'ignored','mailbox label excluded'); result['handled']+=1; continue
                newer = conn.execute('''SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?
                    AND (received_at>? OR (received_at=? AND message_key>?))''',
                    (m['case_id'],m['contact_id'],m['received_at'],m['received_at'],m['message_key'])).fetchone()
                if newer:
                    _finish_message(conn,m['message_key'],'ignored','superseded by newer inbound'); result['handled']+=1; continue
                if m['suggested_kind'] in {'opt_out','declined','delivery_failed'}:
                    _finish_message(conn,m['message_key'],'handled',m['suggested_kind']);result['handled']+=1;continue
                template, urls = _routine(m['excerpt'])
                if not template:
                    raise ValueError('outside routine reply scope')
                if template == 'courtesy':
                    _finish_message(conn,m['message_key'],'handled','courtesy; no reply needed');result['handled']+=1;continue
                if not g['rules']['routine_replies']:
                    raise ValueError('automatic replies disabled')
                text=_reply_text(conn,case,person,m,template,urls,profile,g)
                draft=out.compose(conn,case['id'],person['id'],profile,channel='email',kind='reply',
                                  reply_to=m['message_key'],text=text,include_resume=template=='materials',_template=template)
                if template=='materials' and not s.obj(draft['payload_json']).get('attachment'):
                    conn.execute("UPDATE referral_outbox SET state='cancelled' WHERE id=?",(draft['id'],))
                    raise ValueError('requested resume is not ready; no invented attachment')
                _finish_message(conn,m['message_key'],'drafted',template,draft['id']);result['drafted']+=1
            except PendingInput as exc:
                _finish_message(conn,m['message_key'],'waiting',str(exc))
            except (ValueError,OSError) as exc:
                _finish_message(conn,m['message_key'],'review',str(exc));result['exceptions']+=1
    return result


def authorize_drafts(conn, profile, *, limit=100, outbox_id=None):
    """Grant only immutable locally generated drafts that meet standing policy."""
    result={'authorized':0,'deferred':[]}
    if s.policy(conn)['mode']!='unattended' or not grant(conn):
        return result
    if grant(conn)['rules'].get('version') == 2:
        for raw in conn.execute("SELECT o.* FROM referral_outbox o JOIN referral_generated g ON o.id=g.outbox_id WHERE o.state='approved' AND o.attempted_at IS NULL AND o.approved_by LIKE 'standing:%' AND g.template IN ('request','followup') AND (? IS NULL OR o.id=?)", (outbox_id, outbox_id)).fetchall():
            try:
                out._validate_current(conn,dict(raw),profile=profile)
            except (ValueError,OSError):
                with s.transaction(conn):
                    conn.execute("UPDATE referral_outbox SET state='draft',approved_hash=NULL,approved_by=NULL,approved_at=NULL WHERE id=?",(raw['id'],))
    rows=conn.execute("SELECT id FROM referral_outbox WHERE state='draft' AND (? IS NULL OR id=?) AND channel IN ('email','linkedin','x','manual') ORDER BY CASE WHEN kind LIKE 'reply:%' THEN 0 ELSE 1 END,created_at,id LIMIT ?",(outbox_id,outbox_id,limit)).fetchall()
    for r in rows:
        try:
            row=out._outbox(conn,r['id'])
            generated=conn.execute('SELECT * FROM referral_generated WHERE outbox_id=?',(row['id'],)).fetchone()
            if generated and row['kind'] in {'request','followup'} and grant(conn)['rules'].get('version') == 2:
                from applypilot.unattended.policy import require_recipient, RecipientIneligible
                try:
                    require_recipient(conn, s.require_case(conn,row['case_id']), out._contact(conn,row['contact_id']),
                                      kind=row['kind'], channel=None if row['channel']=='manual' else row['channel'],
                                      outbox_id=row['id'])
                except RecipientIneligible as exc:
                    out.retire_ineligible_draft(conn, row, exc.decision)
                    result['deferred'].append({'outbox_id':row['id'], **exc.decision})
                    continue
            refreshed = False
            if generated and generated['template'] in {'request','followup'} and not row.get('attempted_at'):
                try:
                    out._validate_current(conn,row,profile=profile)
                except (ValueError,OSError):
                    case,person=s.require_case(conn,row['case_id']),out._contact(conn,row['contact_id'])
                    if conn.execute('SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?',(case['id'],person['id'])).fetchone():
                        raise
                    row=out.compose(conn,case['id'],person['id'],profile,kind=row['kind'],channel=None if row['channel']=='manual' else row['channel'],_refresh=True)
                    refreshed = True
            if generated and not row.get('attempted_at'):
                from applypilot.reasoning.settings import dspy_enabled, rlm_enabled
                if refreshed or (generated['template'] in {'request','followup'}
                                 and (dspy_enabled('outreach_draft') or rlm_enabled())):
                    from applypilot.integrations.referrals import improve_draft
                    row = improve_draft(conn, row, profile)
            with s.transaction(conn):
                row=out._outbox(conn,r['id'])
                if row['state']!='draft':
                    continue
                generated=conn.execute('SELECT * FROM referral_generated WHERE outbox_id=?',(row['id'],)).fetchone()
                if grant(conn)['rules'].get('version') == 1 and generated and generated['template'] not in {'request','followup'} and row['channel']=='email':
                    payload=s.obj(row['payload_json'])
                    parent=conn.execute('SELECT * FROM referral_messages WHERE message_key=?',
                                        (payload.get('reply_message_key'),)).fetchone()
                    if parent:
                        newer=conn.execute('''SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?
                            AND (received_at>? OR (received_at=? AND message_key>?))''',
                            (parent['case_id'],parent['contact_id'],parent['received_at'],parent['received_at'],parent['message_key'])).fetchone()
                        if newer:
                            raise ValueError('newer inbound needs a different response')
                        case,person=s.require_case(conn,row['case_id']),out._contact(conn,row['contact_id'])
                        template,urls=_routine(parent['excerpt'])
                        if template!=generated['template']:
                            raise ValueError('routine intent changed')
                        text=_reply_text(conn,case,person,dict(parent),template,urls,profile,grant(conn))
                        row=out.compose(conn,case['id'],person['id'],profile,kind='reply',channel='email',
                            reply_to=parent['message_key'],text=text,include_resume=template=='materials',_template=template,_refresh=True)
                validate_automatic(conn,row,profile=profile)
                g=grant(conn)
                out.approve(conn,row['id'],row['payload_hash'],actor='standing:'+g['id'],profile=profile)
                result['authorized']+=1
        except ValueError as exc:
            from applypilot.unattended.failures import describe
            failure = describe(exc, operation='authorize_draft')
            result['deferred'].append({'outbox_id':r['id'], **failure})
    return result


async def reconcile_pending(conn, mailbox, *, limit=5):
    """Read-only exact delivery reconciliation; no absent-message retry."""
    sender=out.ReferralMailbox(mailbox,allow_send=False)
    rows=conn.execute("SELECT id FROM referral_outbox WHERE state IN ('sending','uncertain') AND mailbox_key=? ORDER BY attempted_at LIMIT ?",(mailbox.mailbox_key,limit)).fetchall()
    results=[]
    for row in rows:
        try:
            status=await out.reconcile(conn,sender,row['id'])
            results.append({'outbox_id':row['id'],'status':status})
        except Exception as exc:
            results.append({'outbox_id':row['id'],'status':'unresolved','error':type(exc).__name__})
            break
    return results


def digest(conn, *, hours=24):
    if type(hours) is not int or not 1<=hours<=720:
        raise ValueError('hours must be 1..720')
    since=s.iso(s.utc()-timedelta(hours=hours))
    counts={r[0]:r[1] for r in conn.execute('SELECT state,COUNT(*) FROM referral_outbox WHERE updated_at>=? GROUP BY state',(since,))}
    return {'since':since,'mode':s.policy(conn)['mode'],'outbox_states':counts,
            'standing_authorized':conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE approved_by LIKE 'standing:%' AND approved_at>=?",(since,)).fetchone()[0],
            'exceptions':[dict(r) for r in conn.execute("SELECT message_key,reason,updated_at FROM referral_auto_messages WHERE status='review' ORDER BY updated_at DESC LIMIT 50")],
            'manual_social_drafts':conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE channel='manual' AND state='draft'").fetchone()[0],
            'contacts_eligible_for_automatic_email':sum(contact_allowed(dict(r)) for r in conn.execute('SELECT * FROM referral_contacts')),
            'note':'Version-2 setup supports machine-observed contacts and bounded BrowserOS DMs. Exceptions do not indefinitely hold applications.'}

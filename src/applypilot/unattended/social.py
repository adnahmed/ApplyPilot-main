"""Durable social outbox with exact-recipient checks and no blind retry.

Read-only reconciliation is the only recovery after a persisted send intent.
Messages from unrelated inbox conversations are not collected.
"""
from __future__ import annotations

import re
from applypilot.referrals import store as s, outreach as out
from .policy import mailbox_key, validate_automatic
from .browser import normalize


def ingest(conn, row, snapshot, cfg):
    if not snapshot.get('verified'): raise ValueError('Unverified social conversation')
    payload=s.obj(row['payload_json']);contact=out._contact(conn,row['contact_id'])
    messages=snapshot.get('messages',[])
    # Do not ingest old personal history: require an exact sent assistant message
    # anchor in the visible conversation, then consume only later inbound entries.
    sent=conn.execute("SELECT message_id,payload_json FROM referral_outbox WHERE contact_id=? AND channel=? AND state='sent' ORDER BY sent_at",
                     (row['contact_id'],row['channel'])).fetchall()
    anchors=[]
    for i,m in enumerate(messages):
        if any(m.get('id')==r['message_id'] and normalize(m.get('text',''))==normalize(s.obj(r['payload_json'])['body']) for r in sent):
            anchors.append(i)
    if not anchors: return {'new':0,'read_complete':False,'reason':'sent anchor not visible'}
    count=0;complete=True
    for m in messages[min(anchors)+1:]:
        if any(normalize(m.get('text',''))==normalize(s.obj(r['payload_json'])['body']) for r in sent): continue
        if m.get('direction')=='out': continue
        if m.get('direction')!='in': complete=False;continue
        text=m.get('text','')
        if not isinstance(text,str) or not text.strip(): continue
        mk=mailbox_key(row['channel'],cfg)
        key=s.digest(s.dump([mk,contact['profile_key'],m['id']]))
        if conn.execute('SELECT 1 FROM referral_messages WHERE message_key=?',(key,)).fetchone(): continue
        # Observed receipt time, not guessed browser timestamps, starts settling.
        at=s.iso();suggested='reply_received'
        if re.search(r'(?i)\b(?:unsubscribe|stop (?:contacting|messaging)|do not (?:contact|message)|don.t (?:contact|message))\b',text): suggested='opt_out'
        elif re.search(r"(?i)\b(?:cannot|can.t|unable to) refer\b|\bnot interested\b",text): suggested='declined'
        elif re.search(r"(?i)\b(?:i.ve|i have) (?:already )?(?:referred|submitted (?:the |your )?referral)\b",text): suggested='referral_claimed'
        elif re.search(r"(?i)\b(?:i can|i will|happy to) refer\b",text): suggested='referral_offered'
        with s.transaction(conn):
            conn.execute("INSERT INTO referral_messages\n                (message_key,case_id,contact_id,mailbox_key,message_id,thread_id,rfc_message_id,received_at,\n                 sender,subject,excerpt,links_json,state,suggested_kind,created_at,context_json)\n                VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?,'review',?,?,?) ON CONFLICT DO NOTHING",
                (key,row['case_id'],row['contact_id'],mk,m['id'],snapshot['url'],at,contact['profile_key'],
                 payload['subject'],text,s.dump(re.findall(r'https://[^\s<>"\)]+',text)),suggested,s.iso(),
                 s.dump({'match_reason':'verified_social_thread','channel':row['channel'],'from_truncated':False})))
            s.event(conn,row['case_id'],suggested,contact_id=row['contact_id'],quote=text[:2000],
                    message_key=key,key='social:'+key,trust='observed')
            conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE case_id=? AND contact_id=? AND state IN ('draft','approved')",
                         (row['case_id'],row['contact_id']))
            if suggested=='opt_out': s.block_contact(conn,row['contact_id'],'Explicit social opt-out '+key)
        count+=1
    return {'new':count,'read_complete':complete}


def reserve(conn,oid,cfg,profile,expected_hash):
    with s.transaction(conn):
        row=dict(conn.execute('SELECT * FROM referral_outbox WHERE id=? FOR UPDATE',(oid,)).fetchone())
        if row['channel'] not in {'linkedin','x'} or row['state']!='approved' or row['payload_hash']!=expected_hash or row['approved_hash']!=expected_hash:
            raise ValueError('Social approval changed')
        payload,person,case=validate_automatic(conn,row,profile=profile,for_send=True)
        out._check_outreach_limits(conn,row,person,case)
        token=s.digest(s.dump([row['channel'],oid,expected_hash]))
        dispatch_run_id,dispatch_span_id=s.operation_ids()
        conn.execute("UPDATE referral_outbox SET state='sending',dispatch_token=?,attempted_at=?,mailbox_key=?,dispatch_run_id=?,dispatch_span_id=?,updated_at=? WHERE id=?",
                     (token,s.iso(),mailbox_key(row['channel'],cfg),dispatch_run_id,dispatch_span_id,s.iso(),oid))
    return token


def reconcile(conn,row,snapshot):
    """Exact outgoing identity+body proof, never absence-as-permission-to-retry."""
    if row['state'] not in {'sending','uncertain'} or not snapshot.get('verified'): return False
    p=s.obj(row['payload_json'])
    matches=[m for m in snapshot.get('messages',[]) if m.get('direction')=='out' and normalize(m.get('text',''))==normalize(p['body'])]
    if len(matches)!=1: return False
    # Persisted baseline blocks matching an identical message that predates intent.
    evidence=conn.execute('SELECT before_ids_json FROM unattended_social_intents WHERE outbox_id=?',(row['id'],)).fetchone()
    if not evidence or matches[0]['id'] in s.obj(evidence[0],[]): return False
    out._confirm_send(conn,row['id'],row['dispatch_token'],{'id':matches[0]['id'],'threadId':snapshot['url']})
    return True


async def observe_response(conn, browser, cfg, outreach_id):
    row = out._outbox(conn, outreach_id)
    contact = out._contact(conn, row['contact_id'])
    if contact['blocked_at']:
        return {'state': 'suppressed', 'new': 0}
    page = None
    try:
        page, snapshot = await browser.conversation(contact['profile_url'], row['channel'],
            cfg['accounts'][row['channel']])
        if row['state'] in {'sending', 'uncertain'}:
            reconcile(conn, row, snapshot)
        observed = ingest(conn, row, snapshot, cfg)
        return {'state': out._outbox(conn, outreach_id)['state'], **observed}
    finally:
        if page is not None:
            await browser.release_page(page)


async def send_message(conn, browser, cfg, profile, outreach_id):
    row = out._outbox(conn, outreach_id)
    if row['state'] == 'sent':
        return {'state': 'sent', 'message_id': row['message_id']}
    if row['state'] in {'sending', 'uncertain'}:
        return await observe_response(conn, browser, cfg, outreach_id)
    page = None
    operation_id = None
    try:
        payload, person, case = validate_automatic(conn, row, profile=profile, for_send=True)
        page, snapshot = await browser.conversation(person['profile_url'], row['channel'],
            cfg['accounts'][row['channel']])
        if row['kind'] != 'request':
            incoming = ingest(conn, row, snapshot, cfg)
            if incoming['new'] or not incoming['read_complete']:
                return {'state': 'conversation_changed'}
        before = await browser.fill(page, person['profile_url'], row['channel'], payload['body'])
        from applypilot.execution import check_cancelled
        check_cancelled()
        with s.transaction(conn):
            operation_id = reserve(conn, row['id'], cfg, profile, row['payload_hash'])
            conn.execute("""INSERT INTO unattended_social_intents VALUES(?,?,?)
                ON CONFLICT (outbox_id) DO NOTHING""",
                (row['id'], s.dump([m['id'] for m in before.get('messages', [])]), s.iso()))
        response = await browser.send(page, person['profile_url'], row['channel'], payload['body'])
        out._confirm_send(conn, row['id'], operation_id, response)
        return {'state': 'sent', 'message_id': response['id']}
    except BaseException as exc:
        if not operation_id and getattr(exc, 'code', None) == 'social_dm_unavailable':
            with s.transaction(conn):
                conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL,error_kind='unreachable_channel',updated_at=? WHERE id=? AND attempted_at IS NULL", (s.iso(), row['id']))
            return {'state': 'unreachable'}
        if operation_id:
            with s.transaction(conn):
                conn.execute("""UPDATE referral_outbox SET state='uncertain',error_kind=?,updated_at=?
                    WHERE id=? AND state='sending' AND dispatch_token=?""",
                    (type(exc).__name__, s.iso(), row['id'], operation_id))
        raise
    finally:
        if page is not None:
            await browser.release_page(page)


def unreachable_fallbacks(conn,profile,*,limit=3,case_id=None):
    """Skip a proven unavailable free DM, without pretending anybody was contacted."""
    from .policy import preferred_channel
    result=0
    cases=conn.execute("""SELECT DISTINCT c.* FROM referral_cases c JOIN referral_outbox o ON o.case_id=c.id
        WHERE o.state='cancelled' AND o.error_kind='unreachable_channel' AND (? IS NULL OR c.id=?)
        AND NOT EXISTS(SELECT 1 FROM referral_outbox x WHERE x.case_id=c.id AND
            (x.attempted_at IS NOT NULL OR x.state IN ('draft','approved','sending','uncertain')))
        LIMIT ?""",(case_id,case_id,limit)).fetchall()
    for case in cases:
        previous={r[0] for r in conn.execute('SELECT contact_id FROM referral_outbox WHERE case_id=?',(case['id'],))}
        if len(previous)>=4 or s.application_phase(conn,case['id'])=='in_flight': continue
        for candidate in s.obj(case['plan_json']).get('candidates',[]):
            if candidate['contact_id'] in previous: continue
            person=out._contact(conn,candidate['contact_id'])
            channel=preferred_channel(conn,person)
            if not channel or person['blocked_at']: continue
            try:
                draft=out.compose(conn,case['id'],person['id'],profile,channel=channel)
                validate_automatic(conn,draft,profile=profile)
                result+=1;break
            except (ValueError,OSError):
                # A newly composed ineligible draft must not block the next candidate.
                with s.transaction(conn):
                    conn.execute("UPDATE referral_outbox SET state='cancelled',error_kind='unreachable_channel' WHERE case_id=? AND contact_id=? AND state='draft' AND attempted_at IS NULL",(case['id'],person['id']))
                previous.add(person['id'])
                if len(previous)>=4: break
    return result

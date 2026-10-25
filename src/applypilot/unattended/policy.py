"""Account-bound delivery rules for relevant, private job-search outreach."""
from __future__ import annotations

from applypilot.unattended.budgets import exhausted

from datetime import timedelta
from applypilot.referrals import store as s, outreach as out
from applypilot.referrals.identity import profile_key
from applypilot.unattended.settings import load

TEMPLATES={'request','followup','materials','application_status','acknowledge','procedure_ack',
           'profile_answer','clarify','ask_email','name_on_form','contextual_reply','decline_feedback'}


def configure(conn, profile, cfg=None):
    """Sync delivery state from local account settings when networking starts."""
    from applypilot.referrals import autonomy
    with s.transaction(conn):
        saved = load()
        if cfg is not None and cfg != saved:
            return None  # A queued cycle must not replace newer settings.
        cfg = saved
        if not cfg['enabled'] or not cfg['channels']:
            return None
        sender = s.email_address(profile.get('personal', {}).get('email'))
        current = active(conn, cfg)
        if current and current['sender'] == sender:
            return current
        result = autonomy.authorize(conn, sender, actor='owner:configured-accounts',
                                    daily_limit=cfg['daily_send_limit'])
        rules = result['rules'] | {'version': 2, 'channels': cfg['channels'],
            'policy_hash': s.digest(s.dump(cfg)),
            'new_recipients': 'observed_current_professionals_relevant_to_high_fit_jobs',
            'routine_replies': cfg['automatic_replies']}
        conn.execute('UPDATE referral_auto_grants SET rules_json=? WHERE id=?',
                     (s.dump(rules), result['grant_id']))
        s.configure(conn, mode='unattended', daily_send_limit=cfg['daily_send_limit'],
            daily_search_limit=cfg['daily_research_limit'], referral_first_hours=cfg['referral_first_hours'],
            plan_limit=max(3, cfg['research_jobs_per_cycle']), daily_plan_limit=cfg['daily_research_limit'])
        from applypilot.referrals.graph import seed_profile
        seed_profile(conn, profile)
    return autonomy.grant(conn)


def active(conn,cfg=None):
    from applypilot.referrals.autonomy import grant
    cfg=cfg or load()
    g=grant(conn)
    if not cfg['enabled'] or not g or s.policy(conn)['mode']!='unattended': return None
    if g['rules'].get('version')!=2 or g['rules'].get('policy_hash')!=s.digest(s.dump(cfg)):
        return None
    return g


def preferred_channel(conn,contact):
    from applypilot.referrals.autonomy import grant
    g=grant(conn)
    if not g or g['rules'].get('version')!=2: return None
    channels=g['rules'].get('channels',[])
    if 'email' in channels and contact.get('email') and contact.get('email_allowed') and contact.get('email_basis')!='manual_only':
        return 'email'
    identity=contact.get('profile_key') or ''
    for c in ('linkedin','x'):
        if c in channels and identity.startswith(c+':'): return c
    return None


def mailbox_key(channel,cfg):
    return s.digest(channel+':'+profile_key(cfg['accounts'][channel]))


class RecipientIneligible(ValueError):
    def __init__(self, decision):
        self.decision = decision
        super().__init__(decision['reason'])


def recipient_eligibility(conn, case, contact, *, kind='request', channel=None, outbox_id=None, cfg=None):
    """One recipient gate for candidate selection, draft generation and dispatch."""
    cfg = cfg or load()
    def denied(code, reason, trigger='recipient_evidence_changed'):
        return {'eligible': False, 'reason_code': code, 'reason': reason, 'retry_trigger': trigger}
    if contact['blocked_at'] or contact['company_key'] != case['company_key']:
        return denied('recipient_employer_or_suppression', 'Verify contact employer and suppression status')
    if contact.get('profile_key') and conn.execute(
            'SELECT 1 FROM referral_contacts WHERE profile_key=? AND blocked_at IS NOT NULL',
            (contact['profile_key'],)).fetchone():
        return denied('recipient_suppressed', 'Professional identity is suppressed')
    if conn.execute("""SELECT 1 FROM referral_outbox WHERE id!=? AND state IN ('sending','uncertain')
        AND (contact_id=? OR contact_id IN (SELECT id FROM referral_contacts WHERE profile_key=?))""",
        (outbox_id or '', contact['id'], contact.get('profile_key'))).fetchone():
        return denied('delivery_uncertain', 'Delivery to this person is uncertain; read-only reconciliation required',
                      'delivery_reconciled')
    if s.application_phase(conn, case['id']) == 'in_flight':
        return denied('application_in_flight', 'Application in progress/uncertain; wait for verified outcome',
                      'application_reconciled')
    channel = channel or preferred_channel(conn, contact)
    if channel not in cfg['channels']:
        return denied('recipient_channel_unavailable', 'No authorized channel for this recipient', 'channel_or_recipient_changed')
    if channel == 'email' and (not contact.get('email') or not contact.get('email_allowed')
                               or contact.get('email_basis') == 'manual_only'):
        return denied('recipient_email_basis', 'No documented professional address basis')
    if kind in {'request', 'followup'}:
        if s.hiring_context(conn, case['id'])['terminal']:
            return denied('application_terminal', 'Application outcome is terminal', 'verified_application_outcome_changed')
        direct = contact['relationship'] in {'knows_work','professional','acquaintance'} and bool(contact['relationship_note'].strip())
        if channel == 'email' and contact.get('email_basis') == 'public_professional' and not cfg['allow_relevant_cold_outreach']:
            return denied('cold_outreach_disabled', 'Public-address outreach not authorized', 'owner_policy_changed')
        if not direct and contact.get('email_basis') != 'invited_referrals':
            if not cfg['allow_relevant_cold_outreach']:
                return denied('cold_outreach_disabled', 'Cold outreach not authorized', 'owner_policy_changed')
            from applypilot.referrals.ranking import evaluate
            job = s.case_subject(conn, case)
            plan = s.obj(case['plan_json'])
            job['role_family'] = plan.get('job_function', {}).get('name')
            job['referral_team'] = plan.get('job_team', {}).get('name')
            aligned = evaluate(contact, job)['alignment'] in {'declared_same_team','declared_same_function'}
            recruiter = contact['kind'] == 'recruiter' and bool(contact['source_note'])
            directed = case.get('case_type') == 'company' and conn.execute(
                'SELECT 1 FROM company_target_contacts WHERE target_id=? AND contact_id=?',
                (case.get('target_id'), contact['id'])).fetchone()
            if not directed and ((job.get('fit_score') or 0) < s.policy(conn)['min_fit_score'] or not (aligned or recruiter)):
                return denied('recipient_not_relevant', 'Cold outreach requires the configured fit threshold and a relevant contact',
                              'relevant_contact_or_verified_role_evidence_changed')
    return {'eligible': True, 'reason_code': 'recipient_eligible', 'channel': channel}


def require_recipient(conn, case, contact, **kwargs):
    decision = recipient_eligibility(conn, case, contact, **kwargs)
    if not decision['eligible']:
        raise RecipientIneligible(decision)
    return decision


def validate_automatic(conn,row,*,profile,for_send=False):
    cfg=load();g=active(conn,cfg)
    if not g: raise ValueError('Outreach is disabled or account settings have changed; restart networking')
    if for_send and row['approved_by']!='standing:'+g['id']:
        raise ValueError('Standing authorization changed or revoked')
    if for_send:
        from .grace import require_due
        require_due(conn, row, cfg, g['id'])
    gen=conn.execute('SELECT * FROM referral_generated WHERE outbox_id=?',(row['id'],)).fetchone()
    if not gen or gen['payload_hash']!=row['payload_hash'] or gen['version']!=1 or gen['template'] not in TEMPLATES:
        raise ValueError('Not an unchanged, locally generated automatic message')
    payload,contact,case=out._validate_current(conn,row,profile=profile)
    channel=row['channel']
    if channel not in cfg['channels'] or payload['from']!=g['sender']:
        raise ValueError('Channel/account outside owner permission')
    if channel=='email':
        if not contact.get('email_allowed') or contact.get('email_basis')=='manual_only':
            raise ValueError('No documented professional address basis')
        if row['kind'] in {'request','followup'} and contact['email_basis']=='public_professional' and not cfg['allow_relevant_cold_outreach']:
            raise ValueError('Public-address outreach not authorized')
    elif profile_key(payload['to'])!=contact.get('profile_key') or not contact['profile_key'].startswith(channel+':'):
        raise ValueError('Social recipient identity mismatch')
    require_recipient(conn, case, contact, kind=row['kind'], channel=channel, outbox_id=row['id'], cfg=cfg)
    if row['kind']=='followup' and not g['rules']['followups']:
        raise ValueError('Follow-ups disabled')
    if row['kind'].startswith('reply:'):
        if not cfg['automatic_replies']: raise ValueError('Automatic replies disabled')
        parent=conn.execute('SELECT * FROM referral_messages WHERE message_key=?',(payload.get('reply_message_key'),)).fetchone()
        if not parent or parent['contact_id']!=contact['id'] or parent['case_id']!=case['id']:
            raise ValueError('Reply not tied to a verified person and posting')
        ctx=s.obj(parent['context_json'])
        origin=ctx.get('channel','email')
        if ctx.get('match_reason')=='verified_contact_thread':
            if origin!='email' or parent['sender']!=contact.get('email') or parent['mailbox_key']!=s.digest(g['sender']):
                raise ValueError('Reply origin/account changed')
            if ctx.get('reply_to') not in (None,'',contact.get('email')):
                raise ValueError('Different Reply-To is not authorized')
        elif ctx.get('match_reason')=='verified_social_thread':
            if origin not in {'linkedin','x'} or origin not in cfg['channels'] or parent['sender']!=contact.get('profile_key') or parent['mailbox_key']!=mailbox_key(origin,cfg):
                raise ValueError('Social reply origin/account changed')
            if channel!=origin:
                from .conversations import email_from_reply
                if channel!='email' or email_from_reply(conn,dict(parent))!=contact.get('email') or contact.get('email_basis')!='invited_referrals':
                    raise ValueError('Cross-channel reply has no explicit same-person email invitation')
                if payload.get('thread_id') or payload.get('reply_rfc'):
                    raise ValueError('Social identifiers cannot become email thread headers')
        else:
            raise ValueError('Unverified conversation origin')
        if ctx.get('from_truncated') or ctx.get('auto_submitted','').casefold() not in {'','no'} or ctx.get('precedence','').casefold() in {'bulk','list','junk'} or set(ctx.get('labels',[])) & {'SPAM','TRASH','SENT','DRAFT'}:
            raise ValueError('Automatic, truncated or unsolicited bulk reply')
        if s.utc(parent['received_at'])>s.utc()-timedelta(minutes=g['rules']['min_reply_minutes']):
            raise ValueError('Reply settling interval')
        replies=conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE case_id=? AND contact_id=? AND kind LIKE 'reply:%' AND attempted_at IS NOT NULL",(case['id'],contact['id'])).fetchone()[0]
        if replies>=g['rules']['max_reply_rounds']: raise ValueError('Conversation reply budget exhausted')
        if conn.execute("""SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=? AND
            (received_at>? OR (received_at=? AND message_key>?))""",
            (case['id'],contact['id'],parent['received_at'],parent['received_at'],parent['message_key'])).fetchone():
            raise ValueError('Newer reply supersedes this draft')
    start=s.iso(s.utc().replace(hour=0,minute=0,second=0,microsecond=0))
    used=conn.execute('SELECT COUNT(*) FROM referral_outbox WHERE attempted_at>=?',(start,)).fetchone()[0]
    if exhausted(used, cfg['daily_send_limit'], s.policy(conn)['daily_send_limit']):
        raise ValueError('Daily cross-channel outreach budget exhausted')
    return payload,contact,case

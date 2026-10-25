"""Small, submission-aware networking store in the existing PostgreSQL database.

All mutations use the existing short, nested-safe transaction helper. No network,
LLM or mailbox operations occur here. Referrals are not application outcomes.
"""
from __future__ import annotations

import re
from applypilot import postgres
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from applypilot.discovery.repository import transaction as _transaction

def transaction(conn, *, immediate=True):
    return _transaction(conn, immediate=immediate, resource="networking-state")

from applypilot.tracking.feedback_store import digest, dump, iso, obj, utc

DEFAULTS = {
    'mode': 'off', 'min_fit_score': 8, 'plan_limit': 10,
    'daily_send_limit': -1, 'contacts_per_job': -1, 'contact_cooldown_days': 30,
    'contact_max_age_days': 180, 'max_hold_hours': 24, 'followup_after_days': 5,
    'referral_first_hours': 0, 'daily_plan_limit': -1, 'daily_search_limit': -1,
}
RELATIONSHIPS = ('knows_work', 'professional', 'acquaintance', 'mutual', 'cold')
KINDS = ('employee', 'hiring_manager', 'recruiter', 'introducer')
ASSISTANCE = frozenset({'ats_referral', 'employee_endorsement', 'recruiter_intro',
                        'hiring_manager_intro', 'resume_forwarded', 'application_flagged'})
EVENTS = ASSISTANCE | {'reply_received', 'referral_offered', 'referral_claimed', 'declined',
                      'opt_out', 'delivery_failed', 'procedure', 'hold_started', 'hold_released',
                      'outreach_sent', 'manual_outreach', 'route_approved', 'draft_retired'}
PROTECTED_ATTEMPTS = "'prepared','side_effect_intent','verification_required','verification_quarantined','submitted'"


def ensure_schema(conn):
    """Additive migration, owned by caller; default leaves all behavior disabled."""
    conn.execute('CREATE TABLE IF NOT EXISTS referral_schema(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)')
    row = conn.execute('SELECT version FROM referral_schema WHERE id=1').fetchone()
    if row and row[0] not in (1, 2, 3, 4):
        raise ValueError('unsupported referral schema')
    for sql in [
        '''CREATE TABLE IF NOT EXISTS referral_settings(id INTEGER PRIMARY KEY CHECK(id=1),
            config_json TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS referral_contacts(
            id TEXT PRIMARY KEY, name TEXT NOT NULL, company_key TEXT NOT NULL, company TEXT NOT NULL,
            role_function TEXT NOT NULL, team TEXT NOT NULL, relationship TEXT NOT NULL,
            relationship_note TEXT NOT NULL, kind TEXT NOT NULL, email TEXT, profile_url TEXT,
            source TEXT NOT NULL, source_note TEXT NOT NULL, verified_at TEXT NOT NULL,
            email_allowed INTEGER NOT NULL DEFAULT 0, blocked_at TEXT, blocked_reason TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE UNIQUE INDEX IF NOT EXISTS idx_referral_contact_email
            ON referral_contacts(email) WHERE email IS NOT NULL''',
        '''CREATE INDEX IF NOT EXISTS idx_referral_contact_company ON referral_contacts(company_key)''',
        '''CREATE TABLE IF NOT EXISTS referral_cases(
            id TEXT PRIMARY KEY, posting_key TEXT UNIQUE NOT NULL, job_url TEXT NOT NULL REFERENCES jobs(url),
            company_key TEXT NOT NULL, title TEXT NOT NULL, base_target TEXT NOT NULL, state TEXT NOT NULL,
            mechanism TEXT NOT NULL DEFAULT 'unknown', mechanism_evidence TEXT,
            approved_target TEXT, referrer_id TEXT REFERENCES referral_contacts(id), name_permission INTEGER NOT NULL DEFAULT 0,
            hold_until TEXT, hold_started_at TEXT, deadline_at TEXT,
            plan_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS referral_job_links(job_url TEXT PRIMARY KEY REFERENCES jobs(url),
            case_id TEXT NOT NULL REFERENCES referral_cases(id))''',
        '''CREATE INDEX IF NOT EXISTS idx_referral_job_case ON referral_job_links(case_id)''',
        '''CREATE TABLE IF NOT EXISTS referral_outbox(
            id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES referral_cases(id),
            contact_id TEXT NOT NULL REFERENCES referral_contacts(id), kind TEXT NOT NULL,
            channel TEXT NOT NULL, payload_json TEXT NOT NULL, payload_hash TEXT NOT NULL,
            basis_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'draft', approved_hash TEXT,
            approved_at TEXT, approved_by TEXT, dispatch_token TEXT, attempted_at TEXT,
            sent_at TEXT, mailbox_key TEXT, message_id TEXT, thread_id TEXT,
            rfc_message_id TEXT NOT NULL UNIQUE, error_kind TEXT,
            created_run_id TEXT, created_span_id TEXT,
            dispatch_run_id TEXT, dispatch_span_id TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(case_id,contact_id,kind))''',
        '''CREATE INDEX IF NOT EXISTS idx_referral_outbox_state ON referral_outbox(state,created_at)''',
        '''CREATE INDEX IF NOT EXISTS idx_referral_outbox_thread ON referral_outbox(mailbox_key,thread_id)''',
        '''CREATE TABLE IF NOT EXISTS referral_messages(
            message_key TEXT PRIMARY KEY, case_id TEXT REFERENCES referral_cases(id),
            contact_id TEXT REFERENCES referral_contacts(id), mailbox_key TEXT NOT NULL,
            message_id TEXT NOT NULL, thread_id TEXT, rfc_message_id TEXT, received_at TEXT NOT NULL,
            sender TEXT NOT NULL, subject TEXT NOT NULL, excerpt TEXT NOT NULL, links_json TEXT NOT NULL,
            state TEXT NOT NULL, suggested_kind TEXT NOT NULL, reviewed_at TEXT, created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS referral_events(
            id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL, case_id TEXT NOT NULL REFERENCES referral_cases(id),
            contact_id TEXT REFERENCES referral_contacts(id), kind TEXT NOT NULL, occurred_at TEXT NOT NULL,
            message_key TEXT, quote TEXT NOT NULL, details_json TEXT NOT NULL,
            trust TEXT NOT NULL, created_at TEXT NOT NULL, voided_at TEXT, void_reason TEXT)''',
        '''CREATE INDEX IF NOT EXISTS idx_referral_event_case ON referral_events(case_id,occurred_at)''',
    ]:
        conn.execute(sql)
    # Additive v1 -> v2 migration. Previous permissions are not broadened.
    contact_columns = {r[1] for r in conn.table_info('referral_contacts')}
    for name, declaration in [('email_basis', "TEXT NOT NULL DEFAULT 'manual_only'"),
                              ('permission_note', "TEXT NOT NULL DEFAULT ''"),
                              ('profile_key', 'TEXT')]:
        if name not in contact_columns:
            conn.execute(f'ALTER TABLE referral_contacts ADD COLUMN {name} {declaration}')
    message_columns = {r[1] for r in conn.table_info('referral_messages')}
    if 'context_json' not in message_columns:
        conn.execute("ALTER TABLE referral_messages ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}'")
    outbox_columns = {row[1] for row in conn.table_info('referral_outbox')}
    for name in ("created_run_id", "created_span_id", "dispatch_run_id", "dispatch_span_id"):
        if name not in outbox_columns:
            conn.execute(f"ALTER TABLE referral_outbox ADD COLUMN {name} TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_outbox_created_run ON referral_outbox(created_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_outbox_dispatch_run ON referral_outbox(dispatch_run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_referral_event_operation_run ON referral_events("
                 "COALESCE(ap_json_text(details_json,'$._operation_run_id'),ap_json_text(details_json,'$.operation_run_id')))")
    for sql in [
        """CREATE TABLE IF NOT EXISTS referral_auto_grants(
            id TEXT PRIMARY KEY, sender TEXT NOT NULL, actor TEXT NOT NULL, rules_json TEXT NOT NULL,
            created_at TEXT NOT NULL, revoked_at TEXT)""",
        """CREATE TABLE IF NOT EXISTS referral_generated(
            outbox_id TEXT PRIMARY KEY REFERENCES referral_outbox(id), payload_hash TEXT NOT NULL,
            template TEXT NOT NULL, version INTEGER NOT NULL, created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS referral_auto_messages(
            message_key TEXT PRIMARY KEY REFERENCES referral_messages(message_key),
            status TEXT NOT NULL, reason TEXT NOT NULL, outbox_id TEXT, updated_at TEXT NOT NULL)""",

    ]:
        conn.execute(sql)
    # v3 preserves old contacts, permissions and holds. Ambiguous legacy identities
    # are retained; future imports require explicit reconciliation instead of merging.
    from .identity import profile_key
    for contact in conn.execute('SELECT id,profile_url FROM referral_contacts WHERE profile_key IS NULL AND profile_url IS NOT NULL').fetchall():
        try:
            key = profile_key(contact['profile_url'])
        except ValueError:
            continue
        conn.execute('UPDATE referral_contacts SET profile_key=? WHERE id=?', (key, contact['id']))
    conn.execute('CREATE INDEX IF NOT EXISTS idx_referral_profile_key ON referral_contacts(profile_key)')
    conn.execute("""CREATE TABLE IF NOT EXISTS referral_leads(
        id TEXT PRIMARY KEY, identity_key TEXT UNIQUE NOT NULL, profile_url TEXT NOT NULL,
        name TEXT NOT NULL, company_hint TEXT NOT NULL, role_hint TEXT NOT NULL,
        evidence_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'review',
        contact_id TEXT REFERENCES referral_contacts(id), review_note TEXT,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS referral_search_runs(
        id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES referral_cases(id),
        provider TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        status TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}')""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_referral_search_date ON referral_search_runs(started_at)')
    conn.execute('INSERT INTO referral_schema VALUES(1,3) ON CONFLICT DO NOTHING')
    from applypilot.targets.store import ensure_schema as target_schema
    target_schema(conn)
    case_columns = {r[1] for r in conn.table_info('referral_cases')}
    if 'case_type' not in case_columns:
        conn.execute("ALTER TABLE referral_cases ADD COLUMN case_type TEXT NOT NULL DEFAULT 'posting'")
        conn.execute("ALTER TABLE referral_cases ADD COLUMN target_id TEXT REFERENCES company_targets(id)")
        conn.execute("ALTER TABLE referral_cases ALTER COLUMN job_url DROP NOT NULL")
        conn.execute("ALTER TABLE referral_cases ADD CONSTRAINT referral_case_subject CHECK "
                     "((case_type='posting' AND job_url IS NOT NULL) OR (case_type='company' AND target_id IS NOT NULL AND job_url IS NULL))")
        conn.execute("CREATE UNIQUE INDEX idx_referral_company_target ON referral_cases(target_id) WHERE case_type='company'")
    conn.execute('UPDATE referral_schema SET version=4 WHERE id=1')
    conn.execute('INSERT INTO referral_settings VALUES(1,?,?) ON CONFLICT DO NOTHING', (dump(DEFAULTS), iso()))
    from applypilot.unattended.state import ensure_schema as unattended_schema
    from applypilot.referrals.graph import ensure_schema as graph_schema
    from applypilot.enrichment.facts import ensure_schema as facts_schema
    unattended_schema(conn)
    graph_schema(conn)
    facts_schema(conn)


def policy(conn):
    try:
        row = conn.execute('SELECT config_json FROM referral_settings WHERE id=1').fetchone()
    except postgres.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        return dict(DEFAULTS)
    return validate_policy({'contacts_per_job': 2, 'daily_send_limit': 5, 'daily_plan_limit': 30, 'daily_search_limit': 20} | obj(row[0]) if row else {})


def validate_policy(values):
    if not isinstance(values, dict) or set(values) - set(DEFAULTS):
        raise ValueError('unknown referral policy key')
    cfg = DEFAULTS | values
    if cfg['mode'] not in {'off', 'draft', 'active', 'unattended'}:
        raise ValueError('referral mode must be off, draft, active or unattended')
    from applypilot.unattended.budgets import validate_limit
    for name in ('daily_send_limit', 'daily_plan_limit', 'daily_search_limit', 'contacts_per_job'):
        validate_limit(cfg[name], name)
    for name, low, high in [('min_fit_score', 1, 10), ('plan_limit', 0, 100),
                           ('contact_cooldown_days', 1, 365), ('contact_max_age_days', 1, 730),
                           ('max_hold_hours', 0, 24), ('followup_after_days', 3, 30),
                           ('referral_first_hours', 0, 24)]:
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError(f'{name} must be an integer in [{low},{high}]')
    if cfg['referral_first_hours'] > cfg['max_hold_hours']:
        raise ValueError('referral_first_hours cannot exceed max_hold_hours')
    return cfg


def configure(conn, **changes):
    cfg = validate_policy(policy(conn) | changes)
    with transaction(conn):
        if cfg['mode'] == 'unattended' and not conn.execute(
                'SELECT 1 FROM referral_auto_grants WHERE revoked_at IS NULL').fetchone():
            raise ValueError('use referrals authorize-unattended once to grant scoped permission')
        conn.execute('UPDATE referral_settings SET config_json=?,updated_at=? WHERE id=1', (dump(cfg), iso()))
        if changes.get('referral_first_hours') == 0:
            conn.execute("UPDATE referral_cases SET hold_until=NULL WHERE id IN (SELECT e.case_id FROM referral_events e WHERE e.event_key LIKE 'auto-hold:%' AND NOT EXISTS (SELECT 1 FROM referral_events newer WHERE newer.case_id=e.case_id AND newer.kind='hold_started' AND (newer.created_at,newer.id)>(e.created_at,e.id)))")
        if cfg['mode'] == 'off':
            conn.execute('UPDATE referral_auto_grants SET revoked_at=? WHERE revoked_at IS NULL', (iso(),))
            conn.execute("UPDATE referral_cases SET hold_until=NULL WHERE hold_until IS NOT NULL")
            conn.execute("UPDATE referral_outbox SET state='draft',approved_hash=NULL WHERE state='approved'")
    return cfg


def company_key(value):
    # No fuzzy merging of employers/clients/subsidiaries.
    return ' '.join(str(value or '').casefold().split())


def email_address(value):
    value = str(value or '').strip()
    if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        raise ValueError('supply one explicit professional email address; no guessed/recipient lists')
    if any(x in value.lower() for x in ('noreply', 'no-reply', 'notifications@', 'mailer-daemon')):
        raise ValueError('not a professional contact address')
    return value.casefold()


def public_url(value):
    import ipaddress
    text = str(value or '').strip()
    if not text or any(ord(c) < 33 for c in text):
        raise ValueError('expected public HTTPS URL')
    p = urlsplit(text)
    if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
        raise ValueError('expected public HTTPS URL without credentials/nonstandard port')
    host = p.hostname.lower()
    if host in {'localhost', 'localhost.localdomain'} or host.endswith(('.local', '.internal')):
        raise ValueError('private endpoint is not an application link')
    try:
        if not ipaddress.ip_address(host).is_global:
            raise ValueError('private IP is not an application link')
    except ValueError as exc:
        if 'private IP' in str(exc):
            raise
        if '.' not in host:
            raise ValueError('expected public host') from exc
    return text  # preserve query/fragment, including real referral tokens


def add_contact(conn, data):
    """Explicit professional facts from an owner or an evidence-checked source observation."""
    allowed = {'id', 'name', 'company', 'role_function', 'team', 'relationship', 'relationship_note',
               'kind', 'email', 'profile_url', 'source', 'source_note', 'verified_at', 'email_allowed', 'email_basis', 'permission_note'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('unknown contact field')
    for field in ('name', 'company', 'source', 'source_note'):
        if not isinstance(data.get(field), str) or not data[field].strip():
            raise ValueError(f'contact requires {field}')
    relation = data.get('relationship', 'cold')
    if relation not in RELATIONSHIPS or data.get('kind', 'employee') not in KINDS:
        raise ValueError('invalid relationship/contact kind')
    if relation != 'cold' and not str(data.get('relationship_note', '')).strip():
        raise ValueError('warm relationship requires your supporting note')
    if type(data.get('email_allowed', False)) is not bool:
        raise ValueError('email_allowed must be explicit boolean')
    email_basis = data.get('email_basis', 'manual_only')
    if email_basis not in {'manual_only', 'existing_relationship', 'invited_referrals', 'public_professional'}:
        raise ValueError('email_basis must be manual_only, existing_relationship, invited_referrals or public_professional')
    if email_basis == 'existing_relationship' and relation not in {'knows_work','professional','acquaintance'}:
        raise ValueError('existing relationship requires a direct relationship, not a cold or mutual lead')
    if email_basis in {'invited_referrals', 'public_professional'} and not str(data.get('permission_note','')).strip():
        raise ValueError('record the actual invitation to contact this person about referrals')
    address = email_address(data['email']) if data.get('email') else None
    url = public_url(data['profile_url']) if data.get('profile_url') else None
    from .identity import profile_key
    identity = profile_key(url)
    if not address and not url:
        raise ValueError('contact requires an explicit email or professional profile URL')
    at = iso(data.get('verified_at'))
    if utc(at) > utc() + timedelta(minutes=5):
        raise ValueError('contact verification cannot be in the future')
    with transaction(conn):
        matches = conn.execute('SELECT * FROM referral_contacts WHERE id=? OR (email IS NOT NULL AND email=?) OR (profile_key IS NOT NULL AND profile_key=?)',
                               (data.get('id', ''), address, identity)).fetchall()
        if len(matches) > 1:
            raise ValueError('conflicting contact identities; reconcile explicitly before importing')
        old = matches[0] if matches else None
        if old and old['profile_key']:
            if identity is None:
                # Omitting a field in a refresh must not erase a suppression key.
                url, identity = old['profile_url'], old['profile_key']
            elif identity != old['profile_key']:
                raise ValueError('profile identity changed; reconcile the contact explicitly instead of retargeting it')
        cid = old['id'] if old else data.get('id') or uuid4().hex
        values = (cid, data['name'].strip(), company_key(data['company']), data['company'].strip(),
                  str(data.get('role_function', '')).strip(), str(data.get('team', '')).strip(), relation,
                  str(data.get('relationship_note', '')), data.get('kind', 'employee'), address, url,
                  data['source'], data['source_note'], at, int(data.get('email_allowed', False)),
                  old['created_at'] if old else iso(), iso())
        conn.execute('''INSERT INTO referral_contacts
            (id,name,company_key,company,role_function,team,relationship,relationship_note,kind,email,profile_url,
             source,source_note,verified_at,email_allowed,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,company_key=excluded.company_key,company=excluded.company,
            role_function=excluded.role_function,team=excluded.team,relationship=excluded.relationship,
            relationship_note=excluded.relationship_note,kind=excluded.kind,email=excluded.email,
            profile_url=excluded.profile_url,source=excluded.source,source_note=excluded.source_note,
            verified_at=excluded.verified_at,email_allowed=excluded.email_allowed,updated_at=excluded.updated_at''', values)
        conn.execute('UPDATE referral_contacts SET profile_key=? WHERE id=?', (identity, cid))
        # New autonomy fields are explicit; reimport cannot infer or expand permission.
        conn.execute('UPDATE referral_contacts SET email_basis=?,permission_note=? WHERE id=?',
                     (email_basis, str(data.get('permission_note','')), cid))
        # Import/refresh never clears an opt-out or silently preserves changed approvals.
        conn.execute("UPDATE referral_outbox SET state='draft',approved_hash=NULL WHERE contact_id=? AND state='approved'", (cid,))
    return dict(conn.execute('SELECT * FROM referral_contacts WHERE id=?', (cid,)).fetchone())


def block_contact(conn, contact_id, reason):
    if not str(reason).strip():
        raise ValueError('suppression requires a reason')
    with transaction(conn):
        if conn.execute('UPDATE referral_contacts SET blocked_at=?,blocked_reason=? WHERE id=?',
                        (iso(), reason, contact_id)).rowcount != 1:
            raise ValueError('unknown contact')
        conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE contact_id=? AND state IN ('draft','approved')", (contact_id,))
        conn.execute('UPDATE referral_cases SET hold_until=NULL WHERE referrer_id=?', (contact_id,))


def job_with_route(conn, url):
    row = conn.execute('''SELECT j.*,r.target AS route_target,r.route AS route_kind,r.version AS route_version
        FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url WHERE j.url=?''', (url,)).fetchone()
    if row is None:
        raise ValueError('unknown job')
    return dict(row)


def posting_key(job):
    """Only exact ATS/posting identities; a shared email/board is not one vacancy."""
    target = str(job.get('route_target') or job.get('application_url') or job['url'])
    p = urlsplit(target)
    host = (p.hostname or '').lower().removeprefix('www.')
    parts = p.path.strip('/').split('/')
    # Known public posting shapes. No identity inferred from title/employer alone.
    if (host == 'greenhouse.io' or host.endswith('.greenhouse.io')) and 'jobs' in parts:
        pos = parts.index('jobs')
        if pos + 1 < len(parts) and parts[pos + 1].isdigit():
            return 'greenhouse:' + ':'.join(parts[:pos] + [parts[pos + 1]])
    if host in {'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com'} and len(parts) >= 2 and parts[1]:
        if re.fullmatch(r'[0-9a-fA-F-]{32,36}', parts[1]):
            return f'{host}:{parts[0]}:{parts[1]}'
    if p.scheme in {'http', 'https'} and len(parts) >= 2 and parts[-1] and ({'job', 'jobs'} & set(parts)):
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.startswith('utm_')]
        return 'url:' + digest(urlunsplit((p.scheme, p.netloc, p.path, urlencode(sorted(query)), '')))
    return 'job:' + digest(job['url'])


def case_for_job(conn, url):
    try:
        row = conn.execute('SELECT c.* FROM referral_job_links l JOIN referral_cases c ON c.id=l.case_id WHERE l.job_url=?', (url,)).fetchone()
    except postgres.OperationalError as exc:
        if 'no such table' in str(exc):
            return None
        raise
    return dict(row) if row else None


def require_case(conn, cid):
    row = conn.execute('SELECT * FROM referral_cases WHERE id=?', (cid,)).fetchone()
    if not row:
        raise ValueError('unknown referral case')
    return dict(row)


def application_phase(conn, cid):
    case = require_case(conn, cid)
    if case.get('target_id') and case.get('case_type') == 'company':
        from applypilot.targets.store import get, application_state
        phase = application_state(conn, get(conn, case['target_id']))
        return 'submitted' if phase == 'submitted' else 'in_flight' if phase in {'pending', 'uncertain'} else 'company'
    rows = conn.execute('''SELECT aa.status FROM application_attempts aa
        JOIN referral_job_links l ON l.job_url=aa.job_url WHERE l.case_id=?''', (cid,)).fetchall()
    if any(r[0] == 'submitted' for r in rows) or conn.execute(
            'SELECT 1 FROM feedback_submissions f JOIN referral_job_links l ON l.job_url=f.job_url WHERE l.case_id=?',
            (cid,)).fetchone():
        return 'submitted'
    if any(r[0] in {'prepared', 'side_effect_intent', 'verification_required', 'verification_quarantined'} for r in rows):
        return 'in_flight'
    rows = conn.execute('SELECT j.apply_status,j.state FROM jobs j JOIN referral_job_links l ON l.job_url=j.url WHERE l.case_id=?', (cid,)).fetchall()
    if any(r[0] == 'applied' or r[1] == 'applied' for r in rows):
        return 'submitted'
    if any(r[0] == 'in_progress' or r[1] in {'applying', 'verification_required', 'verification_quarantined'} for r in rows):
        return 'in_flight'
    return 'before'


def hiring_context(conn, cid):
    """Verified, current application episodes; an interview is not recipient familiarity."""
    from applypilot.tracking.feedback_store import current_state
    from applypilot.tracking.feedback_inbox import newest_text
    submissions = conn.execute('''SELECT f.* FROM feedback_submissions f
        JOIN referral_job_links l ON l.job_url=f.job_url WHERE l.case_id=?
        AND NOT EXISTS(SELECT 1 FROM feedback_submissions newer WHERE newer.job_url=f.job_url
            AND (newer.submitted_at>f.submitted_at OR
                (newer.submitted_at=f.submitted_at AND newer.submission_id>f.submission_id)))
        ORDER BY f.submitted_at,f.submission_id''', (cid,)).fetchall()
    applications = []
    for submission in submissions:
        events = [dict(row) for row in conn.execute('''SELECT * FROM application_outcomes
            WHERE submission_identifier=? AND voided_at IS NULL
            AND trust IN ('manual','automatic-v1') ORDER BY occurred_at,id''',
            (submission['submission_id'],))]
        correspondence = []
        for row in conn.execute('''SELECT m.message_key,m.received_at,m.payload_json
            FROM feedback_messages m WHERE m.submission_id=? AND m.state='processed'
            ORDER BY m.received_at,m.message_key''', (submission['submission_id'],)):
            payload = obj(row['payload_json'])
            correspondence.append({'message_key': row['message_key'], 'received_at': row['received_at'],
                'sender': payload.get('sender'), 'subject': payload.get('subject'), 'text': newest_text(payload)})
        applications.append({'submission_id': submission['submission_id'], 'job_url': submission['job_url'],
            'submitted_at': submission['submitted_at'],
            'state': current_state(events, submitted_at=submission['submitted_at']),
            'timeline': [{k: row.get(k) for k in ('id','event_type','occurred_at','source','trust','notes','message_key')}
                         for row in events], 'correspondence': correspondence})
    # Quiet-window changes do not invalidate exact-message approval. New evidence does.
    revision = digest(dump([{k: v for k, v in item.items() if k != 'state'} for item in applications]))
    current = applications[-1]['state'] if applications else {}
    return {'applications': applications, 'stage': current.get('stage'),
            'terminal': current.get('terminal'), 'revision': revision}


def case_subject(conn, case):
    """A company is a conversation subject, never a synthetic jobs row."""
    if case.get('job_url'):
        return job_with_route(conn, case['job_url'])
    from applypilot.targets.store import get, application_state
    target = get(conn, case['target_id'])
    value = {'url': target['website'], 'company': target['company'],
             'title': ', '.join(target['roles']) or 'opportunities',
             'target_revision': target['revision'], 'target_id': target['id'],
             'owner_instructions': target['instructions'], 'company_evidence': target['evidence']}
    if application_state(conn, target) == 'submitted':
        row = conn.execute("""SELECT j.* FROM jobs j JOIN company_target_jobs l ON l.job_url=j.url
            WHERE l.target_id=? AND (EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_url=j.url AND a.status='submitted')
            OR EXISTS(SELECT 1 FROM feedback_submissions f WHERE f.job_url=j.url)) ORDER BY j.url LIMIT 1""", (target['id'],)).fetchone()
        if row:
            value.update(dict(row))
    return value


def event(conn, cid, kind, *, contact_id=None, quote='', details=None, at=None,
          key=None, message_key=None, trust='operator-confirmed'):
    if kind not in EVENTS or trust not in {'operator-confirmed', 'observed', 'suggested', 'policy-verified'}:
        raise ValueError('invalid referral event/trust')
    if trust == 'policy-verified' and kind != 'route_approved':
        raise ValueError('policy verification is only for an exact referral route, not completed assistance')
    case = require_case(conn, cid)
    if contact_id and not conn.execute('SELECT 1 FROM referral_contacts WHERE id=? AND company_key=?', (contact_id, case['company_key'])).fetchone():
        historical_thread = trust == 'observed' and conn.execute(
            'SELECT 1 FROM referral_outbox WHERE case_id=? AND contact_id=? AND attempted_at IS NOT NULL',
            (cid, contact_id)).fetchone()
        if not historical_thread:
            raise ValueError('contact does not belong to this employer')
    if kind in ASSISTANCE and (trust != 'operator-confirmed' or not str(quote).strip()):
        raise ValueError('completed assistance needs explicit confirmation and supporting evidence')
    timestamp = iso(at)
    if utc(timestamp) > utc() + timedelta(minutes=5):
        raise ValueError('event cannot be in the future')
    eid, key = uuid4().hex, key or uuid4().hex
    operation_run_id, operation_span_id = operation_ids()
    stored_details = dict(details or {})
    if operation_run_id:
        stored_details["_operation_run_id"] = operation_run_id
    if operation_span_id:
        stored_details["_operation_span_id"] = operation_span_id
    with transaction(conn):
        existing = conn.execute('SELECT * FROM referral_events WHERE event_key=?', (key,)).fetchone()
        if existing:
            old_details = obj(existing['details_json'])
            for name in ("_operation_run_id", "operation_run_id", "_operation_span_id", "operation_span_id"):
                old_details.pop(name, None)
            if (existing['case_id'], existing['contact_id'], existing['kind'], existing['quote'], dump(old_details), existing['trust']) != (cid, contact_id, kind, quote[:4000], dump(details or {}), trust):
                raise ValueError('event key reused for conflicting evidence')
            return dict(existing)
        conn.execute('INSERT INTO referral_events\n            (id,event_key,case_id,contact_id,kind,occurred_at,message_key,quote,details_json,trust,created_at)\n            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
            (eid, key, cid, contact_id, kind, timestamp, message_key, quote[:4000], dump(stored_details), trust, iso()))
        if kind in {'declined', 'opt_out', 'delivery_failed'}:
            conn.execute('UPDATE referral_cases SET hold_until=NULL,updated_at=? WHERE id=?', (iso(), cid))
            conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE case_id=? AND contact_id IS NOT DISTINCT FROM ? AND state IN ('draft','approved')", (cid, contact_id))
        if kind in ASSISTANCE or kind == 'route_approved':
            conn.execute("UPDATE referral_cases SET hold_until=NULL,state='ready',updated_at=? WHERE id=?", (iso(), cid))
    return dict(conn.execute('SELECT * FROM referral_events WHERE event_key=?', (key,)).fetchone())


def operation_ids():
    """Return the active journal correlation without creating a synthetic link."""
    from applypilot.operations.runtime import correlation
    current = correlation()
    return current.get("run_id") or None, current.get("span_id") or None


def retract(conn, event_id, reason):
    if not str(reason).strip():
        raise ValueError('retraction requires a reason')
    with transaction(conn):
        row = conn.execute('SELECT * FROM referral_events WHERE id=?', (event_id,)).fetchone()
        if not row:
            raise ValueError('unknown event')
        conn.execute('UPDATE referral_events SET voided_at=?,void_reason=? WHERE id=?', (iso(), reason, event_id))
        if row['kind'] == 'route_approved':
            raise ValueError('restore the application route explicitly before retracting route approval')


def wait_for_referral(conn, cid, contact_id, hours, *, reason, deadline=None, now=None, event_key=None):
    """Bounded operator/policy hold. Unreviewed observations and cold contacts cannot hold."""
    cfg = policy(conn)
    if cfg['mode'] == 'off' or type(hours) is not int or not 0 < hours <= cfg['max_hold_hours']:
        raise ValueError('hold requires enabled mode and hours within configured maximum')
    if not str(reason).strip():
        raise ValueError('hold needs the actual reason/procedure')
    clock = utc(now)
    with transaction(conn):
        c = require_case(conn, cid)
        if not conn.execute('SELECT 1 FROM referral_cases nc WHERE nc.id=? AND ' +
                            verified_preapplication_sql('nc'), (cid,)).fetchone():
            raise ValueError('application holds require a verified employee-submission procedure')
        contact = conn.execute('SELECT * FROM referral_contacts WHERE id=?', (contact_id,)).fetchone()
        if not contact or contact['blocked_at'] or contact['company_key'] != c['company_key']:
            raise ValueError('hold requires a current contact for this employer')
        if contact['profile_key'] and conn.execute('SELECT 1 FROM referral_contacts WHERE profile_key=? AND blocked_at IS NOT NULL', (contact['profile_key'],)).fetchone():
            raise ValueError('professional identity is suppressed')
        if (contact['relationship'] not in {'knows_work', 'professional', 'acquaintance'}
                or contact['kind'] not in {'employee', 'hiring_manager'}):
            raise ValueError('only an established employee relationship can delay an application')
        if utc(contact['verified_at']) < clock - timedelta(days=cfg['contact_max_age_days']):
            raise ValueError('reverify contact before holding an application')
        if application_phase(conn, cid) != 'before':
            raise ValueError('cannot hold an active/uncertain/submitted application')
        first = utc(c['hold_started_at']) if c['hold_started_at'] else clock
        until = min(clock + timedelta(hours=hours), first + timedelta(hours=cfg['max_hold_hours']))
        deadlines = [utc(value) for value in (deadline, c['deadline_at']) if value]
        from applypilot.apply.application_contract import contract_for, _deadline
        job = conn.execute('SELECT * FROM jobs WHERE url=?', (c['job_url'],)).fetchone()
        contract_deadline = contract_for(dict(job) if job else {}).get('deadline')
        if contract_deadline and (closing_time := _deadline(contract_deadline)) is not None:
            deadlines.append(closing_time)
        close = iso(min(deadlines)) if deadlines else None
        if close:
            until = min(until, utc(close) - timedelta(hours=2))
        if until <= clock:
            raise ValueError('hold window exhausted or application deadline too close')
        conn.execute('UPDATE referral_cases SET hold_until=?,hold_started_at=?,deadline_at=?,referrer_id=?,updated_at=? WHERE id=?',
                     (iso(until), iso(first), close, contact_id, iso(), cid))
        event(conn, cid, 'hold_started', contact_id=contact_id, quote=reason, details={'until': iso(until)}, at=clock, key=event_key)
    return iso(until)


def release(conn, cid, reason):
    with transaction(conn):
        event(conn, cid, 'hold_released', quote=reason)
        conn.execute('UPDATE referral_cases SET hold_until=NULL,updated_at=? WHERE id=?', (iso(), cid))


def verified_preapplication_sql(alias='nc'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias):
        raise ValueError('invalid SQL alias')
    return f'''({alias}.mechanism='employee_submission'
        AND NULLIF(TRIM({alias}.mechanism_evidence),'') IS NOT NULL
        AND EXISTS(SELECT 1 FROM referral_events np WHERE np.case_id={alias}.id
            AND np.kind='route_approved' AND np.voided_at IS NULL
            AND np.trust IN ('operator-confirmed','policy-verified')
            AND ap_json_text(np.details_json,'$.mechanism')='employee_submission'))'''


def hold_sql(alias='j'):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', alias):
        raise ValueError('invalid SQL alias')
    return f'''NOT EXISTS(SELECT 1 FROM referral_job_links nl JOIN referral_cases nc ON nc.id=nl.case_id
        JOIN referral_settings ns ON ns.id=1 JOIN referral_contacts nh ON nh.id=nc.referrer_id
        WHERE nl.job_url={alias}.url AND {verified_preapplication_sql('nc')}
        AND nh.company_key=nc.company_key AND nh.blocked_at IS NULL
        AND nh.relationship IN ('knows_work','professional','acquaintance')
        AND nh.kind IN ('employee','hiring_manager')
        AND NOT EXISTS(SELECT 1 FROM referral_contacts nb WHERE nb.profile_key=nh.profile_key AND nb.blocked_at IS NOT NULL)
        AND ap_day(nh.verified_at)>=ap_day('now','-'||COALESCE(ap_json_text(ns.config_json,'$.contact_max_age_days'),'180')||' days')
        AND ap_json_text(ns.config_json,'$.mode') != 'off' AND ap_day(nc.hold_until)>ap_day('now')
        AND ap_day(nc.hold_started_at,'+'||COALESCE(ap_json_text(ns.config_json,'$.max_hold_hours'),'24')||' hours')>ap_day('now')
        AND (nc.deadline_at IS NULL OR ap_day(nc.deadline_at)>ap_day('now','+2 hours'))
        AND (NULLIF(ap_json_text({alias}.application_contract_json,'$.deadline'),'') IS NULL
             OR ap_day(CASE WHEN length(ap_json_text({alias}.application_contract_json,'$.deadline'))=10
                 THEN ap_json_text({alias}.application_contract_json,'$.deadline')||'T23:59:59+00:00'
                 ELSE ap_json_text({alias}.application_contract_json,'$.deadline') END)>ap_day('now','+2 hours')))'''


def snapshot(conn, url, *, at=None):
    """Fact snapshot; late advocacy is not mislabeled as present at application time."""
    c = case_for_job(conn, url)
    if not c:
        return {'status': 'not_recorded'}
    events = [dict(r) for r in conn.execute('''SELECT * FROM referral_events WHERE case_id=?
        AND voided_at IS NULL AND occurred_at<=? ORDER BY occurred_at,id''', (c['id'], iso(at)))]
    if utc(c['created_at']) > utc(at) and not events:
        return {'status': 'not_recorded'}
    confirmed = [e for e in events if e['kind'] in ASSISTANCE and e['trust'] == 'operator-confirmed']
    contacted = [e for e in events if e['kind'] in {'outreach_sent', 'manual_outreach'}]
    # Relationship/alignment at sending is kept in event details; never infer "warm" from replies.
    exposures = [obj(e['details_json']) for e in contacted]
    procedures = [e for e in events if e['kind'] == 'route_approved' and e['trust'] in {'operator-confirmed','policy-verified'}]
    mechanism = obj(procedures[-1]['details_json']).get('mechanism', 'unknown') if procedures else 'unknown'
    return {'case_id': c['id'], 'status': 'assistance_confirmed' if confirmed else 'outreach' if contacted else 'considered',
            'assistance': sorted({e['kind'] for e in confirmed}), 'event_ids': [e['id'] for e in confirmed],
            'outreach_started_at': contacted[0]['occurred_at'] if contacted else None,
            'exposures': exposures, 'mechanism': mechanism}


def for_application(conn, url):
    c = case_for_job(conn, url)
    if not c:
        return {}
    name = None
    if c['name_permission'] and c['referrer_id']:
        row = conn.execute('SELECT name FROM referral_contacts WHERE id=?', (c['referrer_id'],)).fetchone()
        name = row[0] if row else None
    return {'case_id': c['id'], 'mechanism': c['mechanism'], 'approved_url': c['approved_target'],
            'authorized_referrer_name': name, 'observed': snapshot(conn, url)}


def approve_route(conn, cid, *, target=None, mechanism='unknown', evidence, contact_id=None,
                  name_permission=False, confirmed_same_posting=False, trust='operator-confirmed'):
    """Verified route only. Automated callers must establish exact posting identity; no link fetching."""
    if trust not in {'operator-confirmed', 'policy-verified'}:
        raise ValueError('invalid route verification provenance')
    if mechanism not in {'unknown', 'referral_link', 'employee_submission', 'name_on_form', 'post_apply_allowed'}:
        raise ValueError('unknown referral procedure')
    if not str(evidence).strip() or not confirmed_same_posting:
        raise ValueError('verify employer, exact posting and procedure before approving')
    if target:
        target = public_url(target)
        p = urlsplit(target)
        if re.search(r'(?i)(reset.?password|password.?reset|/login|/signin|/auth/)', p.path):
            raise ValueError('account/login invitation needs manual handling, not an application route')
    if mechanism == 'referral_link' and not target:
        raise ValueError('referral_link needs a verified URL')
    if name_permission and not contact_id:
        raise ValueError('referrer name requires explicitly authorized contact')
    from applypilot.apply.routes import ApplicationRoute, persist_route
    with transaction(conn):
        c = require_case(conn, cid)
        if application_phase(conn, cid) != 'before':
            raise ValueError('do not reroute/resubmit active or submitted applications; record advocacy instead')
        if contact_id and not conn.execute('SELECT 1 FROM referral_contacts WHERE id=? AND company_key=? AND blocked_at IS NULL', (contact_id, c['company_key'])).fetchone():
            raise ValueError('referrer must be an unsuppressed contact at this employer')
        if target:
            expected = posting_key({'url': c['job_url'], 'route_target': c['base_target']})
            new = posting_key({'url': c['job_url'], 'route_target': target})
            # For recognized vendor posting IDs a contradictory ID is never an override.
            known = ('greenhouse:', 'jobs.lever.co:', 'jobs.eu.lever.co:', 'jobs.ashbyhq.com:')
            if expected.startswith(known) and new.startswith(known) and expected != new:
                raise ValueError('referral URL identifies a different posting')
            for row in conn.execute('SELECT job_url FROM referral_job_links WHERE case_id=?', (cid,)).fetchall():
                persist_route(conn, row[0], ApplicationRoute('external', target, 'referral-verified:'+cid, iso()))
        conn.execute('''UPDATE referral_cases SET mechanism=?,mechanism_evidence=?,approved_target=?,
            referrer_id=?,name_permission=?,hold_until=NULL,state='ready',updated_at=? WHERE id=?''',
            (mechanism, evidence[:4000], target, contact_id, int(name_permission), iso(), cid))
        event(conn, cid, 'route_approved', contact_id=contact_id, quote=evidence,
              details={'mechanism': mechanism, 'target_hash': digest(target or '')}, trust=trust)
        conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE case_id=? AND state IN ('draft','approved')", (cid,))


def preserve_route(conn, job_url, route):
    """A rediscovery must not silently strip a verified referral token."""
    c = case_for_job(conn, job_url)
    if not c or not c['approved_target'] or route.evidence in {'operator-verified-referral:'+c['id'], 'referral-verified:'+c['id']}:
        return route
    base = posting_key({'url': job_url, 'route_target': c['base_target']})
    incoming = posting_key({'url': job_url, 'route_target': route.target})
    if incoming == base or route.target == c['approved_target']:
        from applypilot.apply.routes import ApplicationRoute
        return ApplicationRoute('external', c['approved_target'], 'referral-verified:'+c['id'], route.resolved_at)
    # A different route/identity needs explicit reconciliation; do not overwrite history.
    raise ValueError('referral posting route changed; verify it before replacing the approved route')


def restore_route(conn, cid, reason):
    """Explicitly restore the original target before application, without reopening an attempt."""
    from applypilot.apply.routes import ApplicationRoute, persist_route
    if not str(reason).strip():
        raise ValueError('restore requires a reason')
    with transaction(conn):
        case = require_case(conn, cid)
        if application_phase(conn, cid) != 'before':
            raise ValueError('cannot change active/uncertain/submitted application route')
        # Clear protection inside this transaction so persist_route can restore.
        conn.execute("UPDATE referral_cases SET approved_target=NULL,mechanism='unknown',name_permission=0,referrer_id=NULL,hold_until=NULL,updated_at=? WHERE id=?", (iso(), cid))
        for row in conn.execute('SELECT job_url FROM referral_job_links WHERE case_id=?', (cid,)).fetchall():
            from applypilot.apply.routes import classify_application_route
            route = classify_application_route(application_url=case['base_target'], listing_url=row[0])
            if route is None:
                raise ValueError('original route cannot be restored; verify manually')
            persist_route(conn, row[0], route)
        conn.execute("UPDATE referral_events SET voided_at=?,void_reason=? WHERE case_id=? AND kind='route_approved' AND voided_at IS NULL", (iso(), reason, cid))
        conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE case_id=? AND state IN ('draft','approved')", (cid,))

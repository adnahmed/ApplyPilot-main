"""Submission-scoped outcome history. No Gmail/LLM/network/file IO.

The existing application_outcomes table remains the event store. Additive columns
provide idempotency, source attribution and reversible corrections; existing rows
are retained as legacy/unreviewed rather than silently becoming training labels.
"""
from __future__ import annotations

import hashlib
import json
import math
from applypilot import postgres
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from applypilot.discovery.repository import transaction as _transaction


def transaction(conn, *, immediate=True):
    """Serialize feedback updates without blocking discovery publication."""
    return _transaction(conn, immediate=immediate, resource="feedback-state")

UTC = timezone.utc
EVENTS = frozenset({
    'confirmation', 'recruiter_response', 'assessment', 'screen', 'interview',
    'technical_interview', 'interview_completed', 'offer', 'rejection',
    'withdrawn', 'accepted', 'hired', 'offer_withdrawn', 'delivery_failed',
})
POSITIVE = frozenset({'recruiter_response', 'screen', 'interview',
                      'technical_interview', 'interview_completed', 'offer', 'accepted', 'hired'})
TERMINAL = frozenset({'rejection', 'withdrawn', 'accepted', 'hired', 'offer_withdrawn', 'delivery_failed'})
POLICY_DEFAULTS = {
    'mode': 'shadow', 'horizon_days': 28, 'lookback_days': 365,
    'min_mature': 40, 'min_cell': 8, 'prior_strength': 20.0,
    'max_boost': 0.75, 'max_model_age_hours': 48,
    'min_confidence': 0.9,
    'retain_body_days': 30, 'silence_days': 14,
    'sync_page_size': 100,
}


def utc(value=None, *, now=None) -> datetime:
    """ISO/RFC2822 timestamps; naive manual times are UTC, not local guesses."""
    if value is None or value == '':
        return now or datetime.now(UTC)
    if isinstance(value, datetime):
        result = value
    else:
        text = str(value).strip()
        try:
            result = datetime.fromisoformat(text.replace('Z', '+00:00'))
        except ValueError:
            try:
                result = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError('invalid outcome timestamp; use ISO 8601 with offset') from exc
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def iso(value=None) -> str:
    return utc(value).isoformat()


def dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def obj(value, default=None):
    if value is None or value == '':
        return {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {} if default is None else default


def digest(value) -> str:
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def ensure_schema(conn) -> None:
    """Explicit v1→v2 schema upgrade; nested callers retain their transaction."""
    with conn:
        _ensure_schema(conn)


def _ensure_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS feedback_priority(
        job_url TEXT PRIMARY KEY REFERENCES jobs(url) ON DELETE CASCADE,
        model_id TEXT NOT NULL, input_revision TEXT,
        boost DOUBLE PRECISION NOT NULL, computed_at TEXT NOT NULL)""")
    conn.execute('CREATE TABLE IF NOT EXISTS feedback_schema (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)')
    row = conn.execute('SELECT version FROM feedback_schema WHERE id=1').fetchone()
    if row and row[0] not in (1, 2):
        raise ValueError('unsupported feedback schema version; do not overwrite it')
    statements = [
        '''CREATE TABLE IF NOT EXISTS feedback_settings (
            id INTEGER PRIMARY KEY CHECK(id=1), config_json TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS feedback_submissions (
            submission_id TEXT PRIMARY KEY, job_url TEXT NOT NULL REFERENCES jobs(url),
            submitted_at TEXT NOT NULL, features_json TEXT NOT NULL, snapshot_origin TEXT NOT NULL,
            created_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS feedback_mailboxes (
            mailbox_key TEXT PRIMARY KEY, scan_after INTEGER, last_complete_at TEXT,
            coverage_start TEXT, coverage_end TEXT,
            last_error TEXT, updated_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS feedback_messages (
            message_key TEXT PRIMARY KEY, mailbox_key TEXT NOT NULL,
            message_id TEXT NOT NULL, thread_id TEXT, received_at TEXT,
            state TEXT NOT NULL DEFAULT 'pending', payload_json TEXT, result_json TEXT,
            submission_id TEXT REFERENCES feedback_submissions(submission_id),
            link_reason TEXT, candidates_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(mailbox_key,message_id))''',
        '''CREATE TABLE IF NOT EXISTS feedback_reviews (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, message_key TEXT, action TEXT NOT NULL,
            actor TEXT NOT NULL, details_json TEXT NOT NULL, occurred_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS feedback_models (
            model_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, checked_at TEXT, config_json TEXT NOT NULL,
            summary_json TEXT NOT NULL, cells_json TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 0)''',
        '''CREATE TABLE IF NOT EXISTS feedback_decisions (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, decision_key TEXT UNIQUE NOT NULL,
            job_url TEXT NOT NULL, attempt_id TEXT, model_id TEXT, mode TEXT NOT NULL,
            base_score DOUBLE PRECISION, adjustment DOUBLE PRECISION NOT NULL, features_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS feedback_observations (
            submission_id TEXT PRIMARY KEY REFERENCES feedback_submissions(submission_id),
            observed_through TEXT NOT NULL, notes TEXT NOT NULL, updated_at TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS idx_feedback_submission_job ON feedback_submissions(job_url,submitted_at)',
        'CREATE INDEX IF NOT EXISTS idx_feedback_thread ON feedback_messages(mailbox_key,thread_id)',
    ]
    for statement in statements:
        conn.execute(statement)
    for table, extra in {'feedback_messages': {'priority':'INTEGER NOT NULL DEFAULT 0'},
                         'feedback_models': {'checked_at':'TEXT'}}.items():
        existing={r[1] for r in conn.table_info(table)}
        for key,kind in extra.items():
            if key not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {key} {kind}')
    cols = {r[1] for r in conn.table_info('application_outcomes')}
    for name, kind in {
        'event_key': 'TEXT', 'message_key': 'TEXT', 'payload_json': 'TEXT',
        'trust': "TEXT NOT NULL DEFAULT 'legacy-unreviewed'",
        'voided_at': 'TEXT', 'void_reason': 'TEXT', 'supersedes_id': 'INTEGER',
    }.items():
        if name not in cols:
            conn.execute(f'ALTER TABLE application_outcomes ADD COLUMN {name} {kind}')
    conn.execute("UPDATE application_outcomes SET event_key='legacy:' || id WHERE event_key IS NULL")
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_feedback_event_key ON application_outcomes(event_key)')
    # Gmail message identity replaces the old lossy date/type-only uniqueness.
    conn.execute('DROP INDEX IF EXISTS idx_application_outcomes_dedupe')
    from applypilot.migrations.retire_execution import mailbox_v2
    mailbox_v2(conn)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_feedback_message_state ON feedback_messages(mailbox_key,state,message_key)')
    conn.execute('INSERT INTO feedback_schema VALUES(1,2) ON CONFLICT(id) DO UPDATE SET version=2')
    conn.execute('INSERT INTO feedback_settings VALUES(1,?,?) ON CONFLICT DO NOTHING', (dump(POLICY_DEFAULTS), iso()))


def policy(conn) -> dict:
    try:
        row = conn.execute('SELECT config_json FROM feedback_settings WHERE id=1').fetchone()
    except postgres.OperationalError as exc:
        if 'no such table' in str(exc):
            return dict(POLICY_DEFAULTS, mode='off')
        raise
    return validate_policy(obj(row[0]) if row else {})


def validate_policy(values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(POLICY_DEFAULTS):
        raise ValueError('unknown feedback policy setting')
    cfg = dict(POLICY_DEFAULTS, **values)
    if cfg['mode'] not in {'off', 'shadow', 'active'}:
        raise ValueError('feedback mode must be off, shadow or active')
    ranges = {
        'horizon_days': (1, 365), 'lookback_days': (1, 3650), 'min_mature': (1, 100000),
        'min_cell': (1, 100000), 'max_model_age_hours': (1, 8760),
        'retain_body_days': (0, 3650), 'silence_days': (1, 365),
        'sync_page_size': (1, 500),
    }
    for name, (low, high) in ranges.items():
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError(f'{name} must be an integer in [{low},{high}]')
    for name, low, high in [('prior_strength', 1, 10000), ('max_boost', 0, 2), ('min_confidence', .5, 1)]:
        val = cfg[name]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or not low <= val <= high:
            raise ValueError(f'invalid {name}')
    if cfg['lookback_days'] < cfg['horizon_days']:
        raise ValueError('lookback_days must cover horizon_days')
    return cfg


def configure(conn, **changes) -> dict:
    cfg = validate_policy(policy(conn) | changes)
    with transaction(conn):
        conn.execute('UPDATE feedback_settings SET config_json=?,updated_at=? WHERE id=1', (dump(cfg), iso()))
        # A changed target/horizon must not reuse an incompatible learned model.
        conn.execute('UPDATE feedback_models SET active=0')
    return cfg


def job_features(job: dict, conn=None) -> dict:
    """Generic immutable facts, not an applicant's occupational taxonomy."""
    from applypilot.discovery.repository import read_job_document
    normalized = read_job_document(job, 'discovery_normalized_json', conn)
    selected = job.get('discovery_selected_arrangement')
    alternatives = normalized.get('work_arrangements', []) if isinstance(normalized, dict) else []
    arrangement = next((a for a in alternatives if isinstance(a, dict) and a.get('id') == selected), {})
    # Prefer established market assessment rather than labeling the arrangement ID a country.
    assessment = read_job_document(job, 'discovery_assessment_json', conn)
    results = assessment.get('arrangement_results', []) if isinstance(assessment, dict) else []
    selected_result = next((a for a in results if isinstance(a, dict) and a.get('id') == selected), {})
    markets = selected_result.get('markets') or arrangement.get('markets') or arrangement.get('market_labels') or []
    # Do not borrow another alternative's geography from the aggregate result.
    if not selected and isinstance(assessment, dict):
        markets = [m.get('label') for m in assessment.get('market_results', [])
                   if isinstance(m, dict) and m.get('compatibility') == 'compatible']
    evidence = obj(job.get('selected_evidence'))
    proofs = [str(r['id']) for r in evidence.get('records', []) if isinstance(r, dict) and r.get('id')] if isinstance(evidence, dict) else []
    # Use explicit family/query-family data only; unknown never means backend-only.
    family = job.get('role_family') or 'unknown'
    return {
        'source': str(job.get('site') or 'unknown').strip().casefold(),
        'company': ' '.join(str(job.get('company') or '').casefold().split()) or 'unknown',
        'role_family': str(family),
        'market': '|'.join(sorted(str(m) for m in markets if m)) or 'unknown',
        'arrangement_id': selected, 'work_mode': arrangement.get('work_mode'),
        'engagement': arrangement.get('engagement'), 'pay_basis': arrangement.get('pay_basis'),
        'employer_regions': selected_result.get('employer_regions', []), 'preference_rank': job.get('discovery_preference_rank'),
        'role_tier': job.get('discovery_role_tier'), 'fit_score': job.get('fit_score'),
        'title': str(job.get('title') or ''), 'job_url': job.get('url'),
        'target': job.get('application_url'), 'channel': job.get('application_route'),
        'document_variant': job.get('document_variant') or 'unknown',
        'proof_ids': proofs, 'jd_hash': digest(job.get('full_description') or job.get('description') or ''),
        'profile_revision': job.get('discovery_profile_version'),
        'intent_revision': job.get('discovery_intent_fingerprint'),
        'assessment_revision': job.get('discovery_assessment_revision'),
        'rules_revision': job.get('discovery_rules_version'),
        'timeline': {name: job.get(name) for name in ('posted_at','discovered_at','scored_at','tailored_at','cover_letter_at')},
    }


def capture_submission(conn, submission_id: str, *, job=None, origin='guarded') -> dict:
    """Called in the submission transaction. Existing snapshots never change."""
    existing = conn.execute('SELECT * FROM feedback_submissions WHERE submission_id=?', (submission_id,)).fetchone()
    if existing:
        return dict(existing)
    attempt = conn.execute('SELECT * FROM application_attempts WHERE attempt_id=? AND status=\'submitted\'', (submission_id,)).fetchone()
    if not attempt:
        raise ValueError('feedback snapshot requires a confirmed submitted attempt')
    a = dict(attempt)
    job = job or dict(conn.execute('SELECT * FROM jobs WHERE url=?', (a['job_url'],)).fetchone())
    snapshot = obj(a.get('snapshot_json'))
    features = job_features(snapshot or job, conn=conn)
    if not snapshot and origin=='historical-snapshot':
        origin='historical-current-fields'
    features['artifact_hashes'] = obj(a.get('artifact_hashes_json'))
    features['target'] = a.get('target') or features['target']
    features['channel'] = a.get('channel') or features['channel']
    features['document_variant'] = a.get('document_variant') or features['document_variant']
    features['proof_ids'] = obj(a.get('selected_proof_ids_json'), []) or features['proof_ids']
    features['selection'] = obj(a.get('feedback_selection_json'))
    from applypilot.referrals.store import snapshot as networking_snapshot
    features['networking_at_submission'] = networking_snapshot(conn, a['job_url'], at=a['submitted_at'])
    features['networking_snapshot_origin'] = 'live' if origin == 'guarded' else 'historical-reconstruction'
    if str(a.get('proof_kind') or '').startswith('already-submitted'):
        origin = 'prior-submission-time-unknown'
    if origin == 'guarded':
        from .pursuit import submission_strategy
        features['pursuit_at_submission'] = submission_strategy(
            conn, features, job_url=a['job_url'], submitted_at=a['submitted_at'], attempt=a)
    conn.execute('INSERT INTO feedback_submissions VALUES(?,?,?,?,?,?)',
                 (submission_id, a['job_url'], iso(a['submitted_at']), dump(features), origin, iso()))
    return dict(conn.execute('SELECT * FROM feedback_submissions WHERE submission_id=?', (submission_id,)).fetchone())


def backfill_submissions(conn) -> int:
    """Existing verified sends only. Does not promote email stubs into applications."""
    count = 0
    with transaction(conn):
        rows = conn.execute("SELECT attempt_id FROM application_attempts WHERE status='submitted' AND submitted_at IS NOT NULL AND attempt_id NOT IN (SELECT submission_id FROM feedback_submissions)").fetchall()
        for row in rows:
            capture_submission(conn, row[0], origin='historical-snapshot')
            count += 1
    return count


def manual_submission(conn, job_url: str, at: str, *, reference: str) -> dict:
    if not reference.strip():
        raise ValueError('a manual submission needs a reference, not an inferred email stub')
    when = utc(at)
    if when > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError('submission cannot be in the future')
    row = conn.execute('SELECT * FROM jobs WHERE url=?', (job_url,)).fetchone()
    if row is None:
        raise ValueError('job not found; add the job before recording its submission')
    sid = 'manual:' + digest(job_url + '\0' + when.isoformat() + '\0' + reference.strip())
    with transaction(conn):
        conn.execute('INSERT INTO feedback_submissions VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                     (sid, job_url, when.isoformat(), dump(job_features(dict(row), conn=conn) | {'manual_reference': reference}), 'manual', iso()))
    return dict(conn.execute('SELECT * FROM feedback_submissions WHERE submission_id=?', (sid,)).fetchone())


def resolve_submission(conn, job_url: str, at: str, submission_id=None) -> dict:
    if submission_id:
        rows = conn.execute('SELECT * FROM feedback_submissions WHERE submission_id=? AND job_url=?', (submission_id, job_url)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM feedback_submissions WHERE job_url=? AND submitted_at<=? ORDER BY submitted_at', (job_url, iso(at))).fetchall()
    if len(rows) != 1:
        raise ValueError('outcome needs one confirmed submission; supply --submission-id when ambiguous')
    return dict(rows[0])


def record_event(conn, job_url, event_type, *, occurred_at=None, source='manual', notes='',
                 submission_id=None, event_key=None, message_key=None, payload=None,
                 trust=None, supersedes_id=None) -> dict:
    if event_type not in EVENTS:
        raise ValueError(f'unsupported application outcome: {event_type}')
    when = utc(occurred_at)
    if when > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError('outcome cannot be in the future')
    submission = resolve_submission(conn, job_url, when.isoformat(), submission_id)
    if when < utc(submission['submitted_at']) - timedelta(minutes=5):
        raise ValueError('outcome predates its submission; review the linkage')
    trust = trust or ('manual' if source == 'manual' else 'legacy-unreviewed')
    if trust not in {'manual', 'automatic-v1', 'legacy-unreviewed'}:
        raise ValueError('invalid event trust')
    # Manual replay of the same explicit fact is idempotent; separate times remain separate events.
    key = event_key or 'manual:' + digest(dump([submission['submission_id'], event_type, when.isoformat(), notes]))
    features = obj(submission['features_json'])
    with transaction(conn):
        old = conn.execute('SELECT * FROM application_outcomes WHERE event_key=?', (key,)).fetchone()
        if old:
            if old['submission_identifier'] != submission['submission_id'] or old['event_type'] != event_type:
                raise ValueError('idempotency key was reused for a different outcome')
            return dict(old)
        if supersedes_id is not None:
            old = conn.execute('SELECT * FROM application_outcomes WHERE id=?', (supersedes_id,)).fetchone()
            if old is None or old['voided_at']:
                raise ValueError('correction target is absent or already voided')
            conn.execute('UPDATE application_outcomes SET voided_at=?,void_reason=? WHERE id=?',
                         (iso(), 'superseded by ' + key, supersedes_id))
        cur = conn.execute('''INSERT INTO application_outcomes
            (job_url,event_type,occurred_at,source,notes,submission_identifier,document_variant,
             document_hash,role_family,market,selected_proof_ids_json,created_at,
             event_key,message_key,payload_json,trust,supersedes_id)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING id''',
            (job_url, event_type, when.isoformat(), source, str(notes)[:4000], submission['submission_id'],
             features.get('document_variant'), digest(dump(features.get('artifact_hashes', {}))),
             features.get('role_family'), features.get('market'), dump(features.get('proof_ids', [])),
             iso(), key, message_key, dump(payload or {}), trust, supersedes_id))
        inserted_id = cur.fetchone()[0]
        projection = project_status(conn, job_url)
        if event_type in {'screen','interview','technical_interview'} and trust in {'manual','automatic-v1'} and not projection.get('terminal'):
            from applypilot.career_ops.invitations import queue_job
            queue_job(conn, job_url)
        elif projection.get('terminal'):
            conn.execute("""UPDATE career_ops_reports SET state='cancelled',
                error='Invitation closed by a later application outcome',updated_at=?
                WHERE job_url=? AND mode IN ('interview_brief','evaluate','interview_plan','interview_practice')
                AND trigger_key IS NOT NULL AND state IN ('pending','running','error')""", (iso(), job_url))
        if supersedes_id is not None and old['job_url'] != job_url:
            project_status(conn, old['job_url'])
        conn.execute('UPDATE feedback_models SET active=0')
        return dict(conn.execute('SELECT * FROM application_outcomes WHERE id=?', (inserted_id,)).fetchone())


def retract_event(conn, event_id: int, *, reason: str, actor='operator') -> None:
    if not reason.strip():
        raise ValueError('retraction requires a reason')
    with transaction(conn):
        row = conn.execute('SELECT * FROM application_outcomes WHERE id=?', (event_id,)).fetchone()
        if row is None:
            raise ValueError('event not found')
        conn.execute('UPDATE application_outcomes SET voided_at=COALESCE(voided_at,?),void_reason=? WHERE id=?', (iso(), reason, event_id))
        if row['message_key']:
            conn.execute("UPDATE feedback_messages SET state='review',link_reason='event_retracted',updated_at=? WHERE message_key=?",(iso(),row['message_key']))
        conn.execute('INSERT INTO feedback_reviews(message_key,action,actor,details_json,occurred_at) VALUES(?,?,?,?,?)',
                     (row['message_key'], 'retract_event', actor, dump({'event_id': event_id, 'reason': reason}), iso()))
        project_status(conn, row['job_url'])
        conn.execute('UPDATE feedback_models SET active=0')


def positive_engagement(events) -> bool:
    """Progress/contact before a decline counts; explaining a decline does not.

    A later explicit invitation/offer can reopen the episode. A subsequent
    rejection never erases a real interview that already occurred in the window.
    """
    terminal = None
    for event in sorted(events, key=lambda e: (utc(e['occurred_at']), e.get('source')=='manual', e['id'])):
        kind=event['event_type']
        if kind in {'offer','accepted','hired'}:
            return True
        if kind in TERMINAL:
            terminal=kind
        elif kind in {'assessment','screen','interview','technical_interview','interview_completed'}:
            if terminal=='withdrawn':
                continue
            terminal=None
            if kind in POSITIVE:
                return True
        elif kind=='recruiter_response' and terminal is None:
            return True
    return False


def current_state(events, *, submitted_at, now=None, silence_days=14) -> dict:
    """Actual chronology + monotone milestones, not a fixed status-priority ladder."""
    now = utc(now)
    live = [dict(e) for e in events if not e['voided_at'] and e['trust'] != 'legacy-unreviewed']
    live.sort(key=lambda e: (utc(e['occurred_at']), e['source'] == 'manual', e['id']))
    stage = 'submitted'
    terminal = None
    reached = set()
    stage_order={'submitted':0,'confirmation':0,'recruiter_response':1,'assessment':2,
                 'screen':3,'interview':4,'technical_interview':5,'interview_completed':6,'offer':7}
    reopening={'assessment','screen','interview','technical_interview','offer'}
    for e in live:
        kind = e['event_type']
        reached.add(kind)
        if kind == 'confirmation':
            if stage == 'submitted':
                stage = 'confirmation'
        elif kind in TERMINAL:
            stage = kind
            terminal = kind
        elif kind in POSITIVE or kind == 'assessment':
            # Feedback explaining a rejection is not a renewed invitation. Likewise,
            # onboarding mail cannot demote an accepted/hired application.
            if terminal in {'accepted','hired','withdrawn'}:
                continue
            if terminal and kind not in reopening:
                continue
            if terminal or stage_order.get(kind,0) >= stage_order.get(stage,0):
                stage = kind
            terminal = None
    quiet = not terminal and not (reached & POSITIVE) and now - utc(submitted_at) >= timedelta(days=silence_days)
    return {'stage': stage, 'no_response_yet': quiet, 'terminal': terminal, 'milestones': sorted(reached)}


def project_status(conn, job_url, *, now=None, silence_days=14) -> dict:
    latest = conn.execute('SELECT * FROM feedback_submissions WHERE job_url=? ORDER BY submitted_at DESC LIMIT 1', (job_url,)).fetchone()
    if latest is None:
        return {}
    events = conn.execute('SELECT * FROM application_outcomes WHERE submission_identifier=?', (latest['submission_id'],)).fetchall()
    status = current_state(events, submitted_at=latest['submitted_at'], now=now, silence_days=silence_days)
    state = status['stage']
    legacy = {'screen': 'interview', 'technical_interview': 'interview', 'interview_completed': 'interview',
              'assessment': 'follow_up', 'recruiter_response': 'follow_up', 'accepted': 'offer', 'hired': 'offer',
              'withdrawn': 'withdrawn', 'offer_withdrawn': 'rejection', 'delivery_failed': 'delivery_failed'}.get(state, state)
    if status['no_response_yet']:
        legacy = 'no_response_yet'
    if legacy == 'submitted':
        legacy = None
    latest_time = max((e['occurred_at'] for e in events if not e['voided_at'] and e['source']=='email'), default=None)
    conn.execute('UPDATE jobs SET tracking_status=?,tracking_updated_at=?,last_email_at=COALESCE(?,last_email_at) WHERE url=?',
                 (legacy, iso(now), latest_time, job_url))
    trusted=[e for e in events if not e['voided_at'] and e['trust']!='legacy-unreviewed']
    trusted.sort(key=lambda e: (utc(e['occurred_at']),e['id']))
    if trusted:
        latest_event=trusted[-1]
        actions=obj(latest_event['payload_json']).get('action_items') or []
        action = next((a for a in actions if isinstance(a, dict) and a.get('action')), {})
        if action or status['terminal']:
            conn.execute('UPDATE jobs SET next_action=?,next_action_due=? WHERE url=?',
                         (None if status['terminal'] else action.get('action'),
                          None if status['terminal'] else action.get('date_iso'), job_url))
        elif state in {'screen', 'interview', 'technical_interview'}:
            conn.execute("UPDATE jobs SET next_action=COALESCE(next_action,'Prepare interview brief and confirm scheduling') WHERE url=?", (job_url,))
    # Project post-submission status only. Never reopen the send/claim lifecycle.
    job = conn.execute('SELECT state,apply_status,agent_id FROM jobs WHERE url=?',(job_url,)).fetchone()
    canonical = {'submitted':'applied', 'confirmation':'applied', 'recruiter_response':'responded',
                 'assessment':'responded', 'screen':'interview', 'interview':'interview',
                 'technical_interview':'interview', 'interview_completed':'interview',
                 'offer':'offer', 'accepted':'offer', 'hired':'offer', 'rejection':'rejected',
                 'offer_withdrawn':'rejected', 'withdrawn':'archived'}.get(state)
    if (canonical and job and job['apply_status']=='applied'
            and job['state'] in {'applied','responded','interview','offer','rejected','ghosted','archived'}
            and job['state'] != canonical):
        from applypilot.database import transition_state
        transition_state(conn,job_url,canonical,reason='submission-scoped feedback projection',
                         metadata={'submission_id':latest['submission_id']},force=True)
    return status


def observe_through(conn, submission_id, when, *, notes):
    """Explicit operator coverage for interviews/outcomes outside Gmail."""
    row=conn.execute('SELECT submitted_at FROM feedback_submissions WHERE submission_id=?',(submission_id,)).fetchone()
    at=utc(when)
    if not row or at<utc(row[0]) or at>utc()+timedelta(minutes=5) or not notes.strip():
        raise ValueError('coverage requires a submission, past time after submission, and notes')
    with transaction(conn):
        conn.execute('INSERT INTO feedback_observations VALUES(?,?,?,?) ON CONFLICT(submission_id) DO UPDATE SET observed_through=excluded.observed_through,notes=excluded.notes,updated_at=excluded.updated_at',
                     (submission_id,iso(at),notes,iso()))
        conn.execute('INSERT INTO feedback_reviews(action,actor,details_json,occurred_at) VALUES(?,?,?,?)',
                     ('observe_through','operator',dump({'submission_id':submission_id,'through':iso(at),'notes':notes}),iso()))
        conn.execute('UPDATE feedback_models SET active=0')

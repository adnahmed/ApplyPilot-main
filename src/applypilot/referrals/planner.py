"""Local, bounded planning. No LinkedIn scraping, email guessing or model calls."""
from __future__ import annotations

from applypilot import operations as ops

from datetime import timedelta
from uuid import uuid4

from applypilot.discovery.admission import current_admission_sql, order_sql
from applypilot.operations.runtime import correlation
from applypilot.referrals.store import (
    application_phase, company_key, dump, event, iso, job_with_route,
    obj, policy, posting_key, require_case, transaction, utc,
)


def research_target(job, previous=None):
    """Choose a fetchable job URL without treating an internal posting ID as a URL."""
    from applypilot.integrations.net import public_url
    from applypilot.integrations.types import SourceError

    for value in (job.get('route_target'), job.get('application_url'), job.get('url'), previous):
        if not isinstance(value, str):
            continue
        try:
            public_url(value, resolve=False)
        except SourceError:
            continue
        return value
    return None


def refresh_research_target(conn, case, job):
    """Repair an unresolved base URL without changing an approved referral route."""
    target = research_target(job, case['base_target'])
    if (target and str(case['base_target']).startswith('discovery:')
            and case['state'] in {'research', 'draft'} and not case['approved_target']):
        with transaction(conn):
            conn.execute(
                "UPDATE referral_cases SET base_target=?,updated_at=? WHERE id=? AND base_target=? "
                "AND state IN ('research','draft') AND COALESCE(approved_target,'')='' "
                "AND mechanism='unknown' AND NOT EXISTS (SELECT 1 FROM referral_outbox o "
                "WHERE o.case_id=referral_cases.id AND (o.approved_at IS NOT NULL OR o.attempted_at IS NOT NULL))",
                (target, iso(), case['id'], case['base_target']),
            )
    return target


def shortlist(conn, job, *, limit=2, now=None, job_value=1.0, mechanism_confidence=.5):
    """Observed relationships and declared team/function; no inferred familiarity."""
    from .ranking import evaluate
    from applypilot.unattended.budgets import batch_size
    limit = batch_size(limit)
    cfg, clock = policy(conn), utc(now)
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('shortlist limit must be 0..100')
    rows = conn.execute('''SELECT * FROM referral_contacts WHERE company_key=? AND blocked_at IS NULL
        AND ap_day(verified_at)<=ap_day(?)''',
        (company_key(job.get('company')), iso(clock + timedelta(minutes=5)))).fetchall()
    ranked = []
    seen = set()
    for row in rows:
        c = dict(row)
        if c.get('profile_key') and conn.execute(
                'SELECT 1 FROM referral_contacts WHERE profile_key=? AND blocked_at IS NOT NULL',
                (c['profile_key'],)).fetchone():
            continue
        last = conn.execute("SELECT MAX(attempted_at) FROM referral_outbox WHERE (contact_id=? OR contact_id IN (SELECT id FROM referral_contacts WHERE profile_key=? AND profile_key IS NOT NULL)) AND state IN ('sending','sent','uncertain')", (c['id'], c.get('profile_key'))).fetchone()[0]
        if last and utc(last) > clock - timedelta(days=cfg['contact_cooldown_days']):
            continue
        item = evaluate(c, job, job_value=job_value, mechanism_confidence=mechanism_confidence)
        from .graph import affinity
        item['network_affinity'] = affinity(conn, c.get('profile_key'))
        item['opportunity_score'] = round(item['opportunity_score'] * (1 + item['network_affinity']['score']), 3)
        ranked.append((item, c.get('profile_key') or c.get('email') or c['id']))
    results = []
    for item, identity in sorted(ranked, key=lambda pair: (pair[0]['tier'], -pair[0]['opportunity_score'], pair[0]['contact_id'])):
        if identity not in seen:
            results.append(item)
            seen.add(identity)
    return results[:limit]


def employer_process_hint(target):
    """Provider capability is a hint; tenant policy requires direct confirmation."""
    from urllib.parse import urlsplit
    host = (urlsplit(str(target)).hostname or '').lower()
    if host == 'greenhouse.io' or host.endswith('.greenhouse.io'):
        return 'Greenhouse supports employee-generated links if enabled; ask for the real link.'
    if 'successfactors' in host:
        return 'SAP can reject a referral after an application or saved draft; ask before portal entry.'
    if 'myworkdayjobs' in host:
        return 'Workday employer configuration is unknown; ask the contact for its procedure.'
    return 'Employer procedure unknown; discovery source does not determine referral mechanics.'


def open_case(conn, job_url, *, job_function=None, function_evidence=None,
              job_team=None, team_evidence=None, job_value=None, value_evidence=None,
              requisition_id=None, requisition_evidence=None, deadline=None):
    cfg = policy(conn)
    if cfg['mode'] == 'off':
        raise ValueError('enable draft/active referral mode before planning')
    job = job_with_route(conn, job_url)
    from applypilot.targets.store import permit_sql, job_targets
    if not conn.execute(f'SELECT 1 FROM jobs j WHERE j.url=? AND {permit_sql(action="outreach")}', (job_url,)).fetchone():
        raise ValueError('Target instructions do not permit job outreach')
    directed = [target for target in job_targets(conn, job_url)
                if target['action'] in {'outreach', 'both'}]
    if directed:
        # One company conversation covers all its applications and existing threads.
        from applypilot.targets.outreach import open_company_case
        return open_company_case(conn, directed[0]['id'])
    from applypilot.enrichment.facts import context as fact_context
    observed = fact_context(conn, job_url)
    prior_case = conn.execute('SELECT c.* FROM referral_cases c JOIN referral_job_links l ON l.case_id=c.id WHERE l.job_url=?', (job_url,)).fetchone()
    prior_fields = obj(prior_case['plan_json']) if prior_case else {}
    for field, key in [('job_function','job_function'),('job_team','job_team'),('requisition_id','requisition')]:
        if prior_fields.get(key):
            observed.pop(field, None)
    if prior_case and prior_case['deadline_at']:
        observed.pop('deadline', None)
    if job_function is None and observed.get('job_function'):
        job_function, function_evidence = observed['job_function'], observed['function_evidence']
    if job_team is None and observed.get('job_team'):
        job_team, team_evidence = observed['job_team'], observed['team_evidence']
    if requisition_id is None and observed.get('requisition_id'):
        requisition_id, requisition_evidence = observed['requisition_id'], observed['requisition_evidence']
    deadline = deadline or observed.get('deadline')
    # Explicit post-application advocacy is supported, without manufacturing a submission.
    submitted = conn.execute("SELECT 1 FROM application_attempts WHERE job_url=? AND status='submitted'", (job_url,)).fetchone()
    submitted = submitted or conn.execute('SELECT 1 FROM feedback_submissions WHERE job_url=?', (job_url,)).fetchone()
    allowed = conn.execute(f'SELECT 1 FROM jobs j WHERE j.url=? AND {current_admission_sql(stage="prepare")} AND j.fit_score>=?',
                           (job_url, cfg['min_fit_score'])).fetchone()
    if not submitted and not allowed:
        raise ValueError('referral planning requires an admitted high-fit job or confirmed submission')
    if not company_key(job.get('company')) or not job.get('title'):
        raise ValueError('resolve the actual hiring employer and role before contacting people')
    key = posting_key(job)
    with transaction(conn):
        old = conn.execute('SELECT * FROM referral_cases WHERE posting_key=?', (key,)).fetchone()
        cid = old['id'] if old else uuid4().hex
        if old and old['company_key'] != company_key(job['company']):
            raise ValueError('same posting has conflicting employers; resolve before contacting people')
        prior = obj(old['plan_json']) if old else {}
        function = prior.get('job_function') or {}
        if job_function is not None:
            if not str(job_function).strip() or not str(function_evidence or '').strip():
                raise ValueError('declaring the job function requires its supporting source/note')
            function = {'name': str(job_function).strip(), 'evidence': str(function_evidence).strip()}
        if function:
            job['role_family'] = function['name']
        from .ranking import unit
        team = prior.get('job_team') or {}
        value = prior.get('job_value') or {'value': 1.0, 'evidence': 'Neutral default; no separate job value declared.'}
        req = prior.get('requisition') or {}
        for supplied, evidence, record, label in ((job_team, team_evidence, team, 'team'),
                                                  (requisition_id, requisition_evidence, req, 'requisition')):
            if supplied is not None:
                if not isinstance(supplied, str) or not supplied.strip() or len(supplied) > 200 or not str(evidence or '').strip():
                    raise ValueError(f'declaring {label} requires a bounded value and supporting evidence')
                record.update(name=supplied.strip(), evidence=str(evidence).strip())
        if job_value is not None:
            if not str(value_evidence or '').strip():
                raise ValueError('declaring job value requires supporting rationale')
            value = {'value': unit(job_value, 'job_value'), 'evidence': str(value_evidence)}
        if deadline is not None:
            deadline = iso(deadline)
        if team:
            job['referral_team'] = team['name']
        mechanism_confidence = 1.0 if old and old['mechanism'] != 'unknown' and old['mechanism_evidence'] else .5
        candidates = shortlist(conn, job, limit=100 if cfg['contacts_per_job'] else 0, job_value=value['value'],
                               mechanism_confidence=mechanism_confidence)
        origin = {'site': job.get('site'), 'source_url': job_url,
                  'application_route': job.get('route_kind'), 'target': job.get('route_target')}
        plan = {'candidates': candidates, 'job_function': function, 'job_team': team,
                'job_value': value, 'requisition': req,
                'opportunity_score': candidates[0]['opportunity_score'] if candidates else 0.0, 'origin': origin, 'base_fit': job.get('fit_score'),
                'preference_rank': job.get('discovery_preference_rank'),
                'assessment_revision': job.get('discovery_assessment_revision'),
                'mechanism_hint': employer_process_hint(job.get('route_target')),
                'research_queries': [f'"{job["company"]}" "{job["title"]}" hiring team',
                                     f'"{job["company"]}" employee referral careers'],
                'no_contact_action': 'Apply normally; import a verified professional contact if found.'}
        for field in ('preparation', 'recipient_gates', 'eligible_contact_ids'):
            if field in prior:
                plan[field] = prior[field]
        if not old:
            conn.execute('''INSERT INTO referral_cases
                (id,posting_key,job_url,company_key,title,base_target,state,plan_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)''',
                (cid, key, job_url, company_key(job['company']), job['title'],
                 research_target(job) or job_url, 'research', dump(plan), iso(), iso()))
        else:
            prior = obj(old['plan_json'])
            plan['origins'] = prior.get('origins', [prior.get('origin', {})])
            if origin not in plan['origins']:
                plan['origins'].append(origin)
            conn.execute('UPDATE referral_cases SET plan_json=?,updated_at=? WHERE id=?', (dump(plan), iso(), cid))
            refresh_research_target(conn, old, job)
        if deadline is not None:
            conn.execute('UPDATE referral_cases SET deadline_at=? WHERE id=?', (deadline, cid))
            # A newly learned deadline can only shorten an existing hold.
            conn.execute('UPDATE referral_cases SET hold_until=LEAST(hold_until,?) WHERE id=? AND hold_until IS NOT NULL',
                         (iso(utc(deadline) - timedelta(hours=2)), cid))
        conn.execute('INSERT INTO referral_job_links VALUES(?,?) ON CONFLICT DO NOTHING', (job_url, cid))
    return require_case(conn, cid)


def plan_jobs(conn, *, limit=None, errors=None):
    """Plan a bounded batch; one invalid posting cannot roll back other cases."""
    cfg = policy(conn)
    limit = cfg['plan_limit'] if limit is None else limit
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('plan limit must be 0..100')
    if cfg['mode'] == 'off' or limit == 0 or cfg['contacts_per_job'] == 0:
        return []
    from applypilot.tracking.feedback_learning import register_priority
    score_expr = register_priority(conn)  # existing cached local model; no new inference
    # Exclude already planned rows so new work gets a turn; known cases can be
    # explicitly refreshed with `open` after a contact import.
    with transaction(conn):
        submitted = """EXISTS(SELECT 1 FROM application_attempts a WHERE a.job_url=j.url
            AND a.status='submitted' AND ap_day(a.submitted_at)>=ap_day('now','-30 days'))"""
        rows = conn.execute(f'''SELECT j.url FROM jobs j WHERE {current_admission_sql(stage='prepare')}
            AND j.fit_score>=? AND length(trim(COALESCE(j.company,'')))>0
            AND (j.apply_status IS NULL OR j.apply_status='failed' OR {submitted})
            AND COALESCE(j.state,'')!='archived'
            AND NOT EXISTS(SELECT 1 FROM referral_job_links l WHERE l.job_url=j.url)
            ORDER BY {order_sql(score_expr=score_expr)} LIMIT 100''',
            (cfg['min_fit_score'],)).fetchall()
        planned = []
        for row in rows:
            if len(planned) >= limit:
                break
            try:
                with transaction(conn):
                    case = open_case(conn, row['url'])
                if case['id'] not in {item['id'] for item in planned}:
                    planned.append(case)
            except ValueError as exc:
                _record_preparation_failure(exc, job_url=row['url'])
                if errors is not None:
                    errors.append({'job_url': row['url'], 'reason': str(exc)})
        return planned

def create_initial_drafts(conn, cases, profile, *, improve=True):
    from applypilot.referrals import outreach as out
    from applypilot.unattended.policy import recipient_eligibility
    from applypilot.integrations.referrals import improve_draft
    drafted = []
    for c in cases:
        candidates = obj(c['plan_json']).get('candidates', [])
        if not candidates or application_phase(conn, c['id']) == 'in_flight':
            continue
        automatic = policy(conn)['mode'] == 'unattended'
        gates = []
        previous = [dict(row) for row in conn.execute('SELECT * FROM referral_outbox WHERE case_id=?', (c['id'],))]
        for row in previous:
            if automatic and row['kind'] == 'request' and not row['attempted_at'] and row['state'] in {'draft','approved'}:
                decision = recipient_eligibility(conn,c,out._contact(conn,row['contact_id']),outbox_id=row['id'],
                                                channel=None if row['channel']=='manual' else row['channel'])
                if not decision['eligible']:
                    gates.append({'contact_id':row['contact_id'], **decision})
                    if out.retire_ineligible_draft(conn,row,decision):
                        row['state'] = 'cancelled'
        if any(row['attempted_at'] or row['dispatch_token'] or row['state'] != 'cancelled' for row in previous):
            continue
        eligible = []
        for candidate in candidates:
            person = out._contact(conn,candidate['contact_id'])
            decision = recipient_eligibility(conn,c,person) if automatic else {'eligible':True}
            if not decision['eligible']:
                gates.append({'contact_id':person['id'], **decision})
                continue
            eligible.append(person)
        with transaction(conn):
            fresh = require_case(conn,c['id'])
            plan = obj(fresh['plan_json'])
            plan['recipient_gates'] = gates
            plan['eligible_contact_ids'] = [person['id'] for person in eligible]
            conn.execute('UPDATE referral_cases SET plan_json=? WHERE id=?', (dump(plan),c['id']))
        for person in eligible:
            old = next((row for row in previous if row['contact_id'] == person['id'] and row['kind'] == 'request'), None)
            if old and not str(old['error_kind'] or '').startswith('recipient_gate:'):
                continue
            out._check_outreach_limits(conn, {'id':'','kind':'request'}, person, c)
            draft = out.compose(conn,c['id'],person['id'],profile,_refresh=bool(old))
            if draft['state'] == 'draft':
                drafted.append(improve_draft(conn,draft,profile) if improve else draft)
                break
    return drafted


def follow_up(conn, outreach_id, profile):
    """Prepare the exact request's follow-up after its Workflow timer."""
    from applypilot.referrals.outreach import compose, _outbox
    from applypilot.integrations.referrals import improve_draft
    original = _outbox(conn, outreach_id)
    if original['state'] != 'sent' or original['kind'] != 'request':
        return None
    existing = conn.execute("SELECT * FROM referral_outbox WHERE case_id=? AND kind='followup'",
                            (original['case_id'],)).fetchone()
    if existing:
        return dict(existing)
    if conn.execute('SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?',
                    (original['case_id'], original['contact_id'])).fetchone():
        return None
    if conn.execute("""SELECT 1 FROM referral_events WHERE case_id=? AND voided_at IS NULL
        AND kind IN ('declined','opt_out','delivery_failed','ats_referral','employee_endorsement',
                     'recruiter_intro','hiring_manager_intro','resume_forwarded','application_flagged')""",
                     (original['case_id'],)).fetchone():
        return None
    return improve_draft(conn, compose(conn, original['case_id'], original['contact_id'],
        profile, kind='followup', channel=original['channel']), profile)


def expire_holds(conn, *, now=None):
    """Cleanup is bookkeeping; queue/guard also enforce expiry directly in SQL."""
    clock = utc(now)
    with transaction(conn):
        rows = conn.execute('SELECT id FROM referral_cases WHERE hold_until IS NOT NULL AND ap_day(hold_until)<=ap_day(?)', (iso(clock),)).fetchall()
        for row in rows:
            event(conn, row['id'], 'hold_released', quote='Referral TTL expired; apply normally, continue networking.',
                  at=clock, trust='observed', key='expired-hold:' + row['id'])
            conn.execute('UPDATE referral_cases SET hold_until=NULL,updated_at=? WHERE id=?', (iso(clock), row['id']))
    return len(rows)


def maybe_hold(conn, cid):
    """One opt-in referral-first window. Never renewed by replanning or replies."""
    cfg = policy(conn)
    if not cfg['referral_first_hours'] or cfg['mode'] == 'off':
        return None
    from . import store
    with transaction(conn):
        case = require_case(conn, cid)
        if case['hold_started_at'] or case['mechanism'] != 'employee_submission':
            return None
        if application_phase(conn, cid) != 'before':
            return None
        job = job_with_route(conn, case['job_url'])
        if (job.get('fit_score') or 0) < cfg['min_fit_score']:
            return None
        if conn.execute("SELECT 1 FROM referral_events WHERE case_id=? AND voided_at IS NULL AND kind IN ('hold_released','declined','opt_out','delivery_failed','ats_referral','employee_endorsement','recruiter_intro','hiring_manager_intro','resume_forwarded','application_flagged')", (cid,)).fetchone():
            return None
        plan = obj(case['plan_json'])
        for candidate in plan.get('candidates', [])[:1]:
            if not candidate.get('hold_eligible') or candidate.get('opportunity_score', 0) <= 0:
                continue
            message = conn.execute("SELECT 1 FROM referral_outbox WHERE case_id=? AND contact_id=? AND kind='request' AND state IN ('draft','approved','sent','sending','uncertain')", (cid, candidate['contact_id'])).fetchone()
            if not message:
                continue
            if cfg['mode'] == 'unattended' and not conn.execute(
                    "SELECT 1 FROM referral_outbox WHERE case_id=? AND contact_id=? AND state='sent'",
                    (cid, candidate['contact_id'])).fetchone():
                continue
            try:
                until = store.wait_for_referral(conn, cid, candidate['contact_id'], cfg['referral_first_hours'],
                    reason='Configured window for a verified employee-submission procedure and established contact.', deadline=case['deadline_at'],
                    event_key='auto-hold:' + cid)
            except ValueError:
                return None  # Changed contact, close deadline or active attempt: do not block.
            return until
    return None


def _record_preparation_failure(exc, *, job_url, case_id=None):
    import hashlib
    import uuid
    parent = correlation().get('span_id', '')
    with ops.scope(span_id=uuid.uuid4().hex, parent_span_id=parent,
                   job_id=hashlib.sha256(job_url.encode()).hexdigest()[:24]):
        ops.capture(exc, component_name='referrals', operation='prepare_job', stage='referrals',
                    kind='finish', details={'case_id': case_id or '', 'origin': 'referrals.planner.prepare_jobs'})


def preparation_cases(conn, *, limit=None, new_case_limit=None):
    """Local scoring-to-referral bridge. No source calls, mailbox access or sends."""
    cfg = policy(conn)
    if cfg['mode'] == 'off':
        return [], {'status': 'disabled', 'reason_code': 'referrals_disabled',
                'message': 'Referrals are off; enable draft mode or explicitly authorize unattended outreach.',
                'planned': 0, 'drafted': 0, 'held': 0, 'expired': 0, 'errors': [], 'sent': 0}
    size = cfg['plan_limit'] if limit is None else limit
    if type(size) is not int or not 0 <= size <= 100:
        raise ValueError('prepare limit must be 0..100')
    if new_case_limit is not None and (type(new_case_limit) is not int or new_case_limit < 0):
        raise ValueError('new_case_limit must be a nonnegative integer')
    result = {'planned': 0, 'drafted': 0, 'held': 0, 'expired': expire_holds(conn), 'errors': [], 'sent': 0}
    # Refreshing old cases cannot consume the independent new-case allowance.
    new_slots = min(size, new_case_limit if new_case_limit is not None else size)
    cases = plan_jobs(conn, limit=new_slots, errors=result['errors']) if new_slots else []
    result['planned'] = len(cases)
    from applypilot.networking_capacity import pending_case_rows
    existing_rows = pending_case_rows(conn, cfg['min_fit_score'], limit=size)
    for row in existing_rows:
        if row['id'] in {c['id'] for c in cases}:
            continue
        try:
            if row['case_type'] == 'company':
                from applypilot.targets.outreach import open_company_case
                current = open_company_case(conn, row['target_id'])
            else:
                current = open_case(conn, row['job_url'])
            if current['id'] not in {c['id'] for c in cases}:
                cases.append(current)
        except ValueError as exc:
            _record_preparation_failure(exc, job_url=row['job_url'])
            result['errors'].append({'job_url': row['job_url'], 'reason': str(exc)})
    return cases, result


def prepared_case(conn, case_id):
    from applypilot.networking_capacity import preparation_policy_hash
    with transaction(conn):
        case = require_case(conn, case_id)
        plan = obj(case['plan_json'])
        plan['preparation'] = {'checked_at': iso(), 'policy_hash': preparation_policy_hash(conn)}
        conn.execute('UPDATE referral_cases SET plan_json=?,updated_at=? WHERE id=?',
                     (dump(plan), iso(), case_id))


def preparation_result(conn, cases, result):
    if result.get('status') == 'disabled':
        return result
    result['research_enqueued'] = 0
    result['selected'] = len(cases)
    result['with_candidates'] = sum(bool(obj(c['plan_json']).get('candidates')) for c in cases)
    gates = [gate for case in cases for gate in obj(require_case(conn,case['id'])['plan_json']).get('recipient_gates',[])]
    result['recipient_gates'] = gates
    result['relevant_contact_ids'] = sorted({identifier for case in cases
        for identifier in obj(require_case(conn,case['id'])['plan_json']).get('eligible_contact_ids',[])})
    result['verified_relevant_contacts'] = len(result['relevant_contact_ids'])
    if result['errors']:
        result.update(status='partial', reason_code='preparation_errors')
    elif result['drafted']:
        result.update(status='complete', reason_code='drafts_created')
    elif result['research_enqueued']:
        result.update(status='complete', reason_code='research_queued',
                      message='Referral cases queued for research; no messages drafted or sent.')
    elif result['planned']:
        result.update(status='complete', reason_code='cases_planned',
                      message='Referral cases created; no research task enqueued or draft yet. Inspect referrals doctor.')
    elif gates:
        result.update(status='deferred',reason_code='recipient_ineligible',
                      message='Recipient evidence or authorized channel must change before drafting.')
    elif cases and not result['with_candidates']:
        result.update(status='deferred', reason_code='no_verified_contacts',
                      message='No verified contact candidates yet; inspect referrals doctor and research results.')
    else:
        result.update(no_work=True, status='empty', reason_code='no_drafts_created',
                      message='No new referral drafts in this pass; inspect referrals doctor for eligibility and gates.')
    return result



def create_fallback_drafts(conn, profile, *, limit=10, case_id=None):
    """At most one next candidate after decline/bounce or the existing quiet window.

    Replies, promised/confirmed assistance and uncertain sends need review instead.
    These are drafts only; ordinary approvals/standing scopes still govern sending.
    """
    from . import outreach as out
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('fallback limit must be 0..100')
    drafted, deferred = [], []
    if policy(conn)['mode'] == 'off' or policy(conn)['contacts_per_job'] in {0, 1}:
        return drafted, deferred
    rows = conn.execute("""SELECT DISTINCT c.* FROM referral_cases c JOIN referral_outbox o ON o.case_id=c.id
        WHERE o.kind='request' AND o.attempted_at IS NOT NULL AND (? IS NULL OR c.id=?)
        AND NOT EXISTS(SELECT 1 FROM referral_outbox p WHERE p.case_id=c.id AND p.state IN ('draft','approved','sending','uncertain'))
        AND NOT EXISTS(SELECT 1 FROM referral_events e WHERE e.case_id=c.id AND e.voided_at IS NULL
            AND e.kind IN ('referral_offered','referral_claimed','ats_referral','employee_endorsement','recruiter_intro','hiring_manager_intro','resume_forwarded','application_flagged'))
        ORDER BY c.updated_at,c.id LIMIT ?""", (case_id,case_id,limit)).fetchall()
    for case in rows:
        try:
            with transaction(conn):
                prior = conn.execute("SELECT * FROM referral_outbox WHERE case_id=? AND kind='request' AND attempted_at IS NOT NULL", (case['id'],)).fetchall()
                from applypilot.unattended.budgets import exhausted
                if exhausted(len({p['contact_id'] for p in prior}), policy(conn)['contacts_per_job']):
                    continue
                # A real inbound reply is not silence. Only a confirmed negative permits fallback.
                unresolved_reply = any(conn.execute('SELECT 1 FROM referral_messages WHERE case_id=? AND contact_id=?', (case['id'], p['contact_id'])).fetchone()
                    and not conn.execute("SELECT 1 FROM referral_events WHERE case_id=? AND contact_id=? AND kind IN ('declined','opt_out','delivery_failed') AND voided_at IS NULL",
                                         (case['id'], p['contact_id'])).fetchone() for p in prior)
                if unresolved_reply:
                    continue
                if case['case_type'] == 'company':
                    from applypilot.targets.outreach import open_company_case
                    refreshed = open_company_case(conn, case['target_id'])
                else:
                    refreshed = open_case(conn, case['job_url'])
                for candidate in obj(refreshed['plan_json']).get('candidates', []):
                    if candidate['contact_id'] in {p['contact_id'] for p in prior}:
                        continue
                    person = out._contact(conn, candidate['contact_id'])
                    if policy(conn)['mode'] == 'unattended':
                        from applypilot.unattended.policy import recipient_eligibility
                        if not recipient_eligibility(conn,refreshed,person)['eligible']:
                            continue
                    # Same checks as dispatch, but without creating an undeliverable draft.
                    out._check_outreach_limits(conn, {'id': '', 'kind': 'request'}, person, refreshed)
                    drafted.append(out.compose(conn, case['id'], person['id'], profile))
                    break
        except ValueError as exc:
            from applypilot.unattended.failures import describe
            deferred.append({'case_id': case['id'], **describe(exc, operation='draft_fallback')})
    return drafted, deferred

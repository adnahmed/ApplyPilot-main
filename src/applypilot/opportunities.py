"""Opportunity decisions and owner actions projected from the existing records.

This module owns no dispatch policy or work queue. Human completion receipts live
in feedback_reviews; applications, replies and practice retain their own lifecycle.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
import math
from urllib.parse import quote, urlencode

from applypilot.tracking.feedback_store import current_state, digest, dump, iso, obj, utc


COMPLETION = 'opportunity_action_completed'
OWNER_INPUT = {'clarify', 'No invented answer; owner notified', 'Requested profile fact is not supplied'}
ANSWERED_INTENTS = {'profile_answer', 'materials', 'application_status', 'contextual_reply', 'procedure_ack'}
JOB_FIELDS = ('url', 'title', 'company', 'fit_score', 'eligibility', 'state', 'apply_status',
              'apply_error', 'applied_at', 'application_url', 'tailored_resume_path', 'cover_letter_path',
              'selected_evidence', 'score_reasoning', 'discovered_at', 'application_contract_json',
              'next_action', 'next_action_due', 'tracking_updated_at', 'full_description',
              'discovery_source_status')


def _dict(value):
    parsed = obj(value)
    return parsed if isinstance(parsed, dict) else {}


def _date(value):
    if not value:
        return None
    try:
        return iso(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _deadline(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None, False
    return (iso(parsed), True) if parsed.tzinfo else (str(value), False)


def _href(job_url):
    return '/ui/opportunities?' + urlencode({'job_url': job_url}) if job_url else '/ui/outreach'


def _snapshot(conn, job_url=None, *, human_only=False):
    tables = {row[0] for row in conn.execute('SELECT tablename FROM pg_tables WHERE schemaname=current_schema()')}
    data = {'tables': tables}
    if 'jobs' not in tables:
        return data
    columns = {row[1] for row in conn.table_info('jobs')}
    fields = ','.join('j.' + name for name in JOB_FIELDS if name in columns)
    where, params = (' WHERE j.url=?', (job_url,)) if job_url else ('', ())
    if human_only and not job_url:
        candidates = []
        if 'application_outcomes' in tables:
            candidates.append("EXISTS(SELECT 1 FROM application_outcomes ha WHERE ha.job_url=j.url AND ha.voided_at IS NULL)")
        if 'career_ops_sessions' in tables:
            candidates.append('EXISTS(SELECT 1 FROM career_ops_sessions hp WHERE hp.job_url=j.url)')
        if 'referral_job_links' in tables:
            candidates.append('EXISTS(SELECT 1 FROM referral_job_links hr WHERE hr.job_url=j.url)')
        if {'feedback_messages', 'feedback_submissions'} <= tables:
            candidates.append("EXISTS(SELECT 1 FROM feedback_submissions hs JOIN feedback_messages hm "
                              "ON hm.submission_id=hs.submission_id WHERE hs.job_url=j.url AND hm.state='review')")
        if 'application_contract_json' in columns:
            candidates.append("(j.eligibility='eligible' AND j.applied_at IS NULL AND "
                "(NULLIF(NULLIF(j.application_contract_json,'')::jsonb->>'deadline','') IS NOT NULL OR "
                "EXISTS(SELECT 1 FROM jsonb_array_elements(COALESCE(NULLIF(j.application_contract_json,'')::jsonb"
                "->'requirements','[]'::jsonb)) hq WHERE hq->>'required'='true')))" )
        where = ' WHERE (' + ' OR '.join(candidates or ['FALSE']) + ')'
    data['jobs'] = [dict(row) for row in conn.execute('SELECT ' + fields + ' FROM jobs j' + where + ' ORDER BY j.url', params)]
    related = ['feedback_submissions', 'application_outcomes']
    if not human_only:
        related += ['application_routes', 'application_attempts', 'application_email_deliveries', 'career_ops_reports']
    for table in related:
        if table in tables:
            clause = ' WHERE job_url=?' if job_url else ''
            data[table] = [dict(row) for row in conn.execute('SELECT * FROM ' + table + clause, params)]
    if 'feedback_messages' in tables:
        clause = ' WHERE f.job_url=?' if job_url else ''
        data['feedback_messages'] = [dict(row) for row in conn.execute(
            'SELECT m.*,f.job_url FROM feedback_messages m LEFT JOIN feedback_submissions f '
            'ON f.submission_id=m.submission_id' + clause, params)]
    if 'feedback_reviews' in tables:
        data['feedback_reviews'] = [dict(row) for row in conn.execute(
            'SELECT * FROM feedback_reviews WHERE action=? ORDER BY occurred_at,id', (COMPLETION,))]
    for table in ('referral_messages', 'referral_outbox') if human_only else ('referral_cases', 'referral_messages', 'referral_outbox', 'referral_events'):
        if table not in tables or 'referral_job_links' not in tables:
            continue
        link = 'r.id' if table == 'referral_cases' else 'r.case_id'
        clause = ' WHERE l.job_url=?' if job_url else ''
        person = '' if table == 'referral_cases' else ' LEFT JOIN referral_contacts p ON p.id=r.contact_id'
        name = '' if table == 'referral_cases' else ',p.name AS contact_name'
        data[table] = [dict(row) for row in conn.execute(
            'SELECT r.*,l.job_url AS linked_job_url' + name + ' FROM ' + table + ' r LEFT JOIN referral_job_links l '
            'ON l.case_id=' + link + person + clause, params)]
    if 'referral_auto_messages' in tables:
        data['referral_auto_messages'] = [dict(row) for row in conn.execute('SELECT * FROM referral_auto_messages')]
    from applypilot.career_ops.readiness import overview
    data['practice'] = overview(conn, job_url=job_url)['lanes'] if 'career_ops_sessions' in tables else []
    return data


def _group(data):
    grouped = defaultdict(lambda: defaultdict(list))
    for table, rows in data.items():
        if table in {'jobs', 'tables', 'feedback_reviews', 'referral_auto_messages', 'practice'}:
            continue
        for row in rows:
            grouped[row.get('linked_job_url') or row.get('job_url') or ''][table].append(row)
    completions = {_dict(row['details_json']).get('action_id'): _dict(row['details_json']) | {
        'completed_at': row['occurred_at'], 'completed_by': row['actor']}
        for row in data.get('feedback_reviews', [])}
    automatic = {row['message_key']: row for row in data.get('referral_auto_messages', [])}
    return grouped, completions, automatic


def _action(job, kind, identity, title, reason, *, at=None, deadline=None, materials=None,
            actor='human', priority=50, target_url=None, completion=None):
    explicit, timezone_known = _deadline(deadline)
    received = _date(at)
    scope = '' if kind in {'employer_response', 'relationship_response', 'message_review', 'unlinked_message'} else job.get('url', '')
    action_id = kind + ':' + digest(dump([scope, identity]))[:32]
    done = (completion or {}).get(action_id, {})
    return {'id': action_id, 'kind': kind, 'job_url': job.get('url', ''),
            'company': job.get('company') or '', 'role': job.get('title') or '',
            'title': title, 'actor': actor, 'reason': reason,
            'deadline': explicit, 'deadline_basis': ('employer_requested' if timezone_known else
                'employer_requested_timezone_unspecified') if explicit else 'not_specified',
            'deadline_label': ('Employer deadline' if timezone_known else
                'Employer date (timezone not stated)') if explicit else 'No stated deadline',
            'materials': materials or [], 'state': 'complete' if done else 'pending',
            'priority': priority, 'created_at': received, 'target_url': target_url or _href(job.get('url')),
            'completion': done or None,
            'overdue': bool(timezone_known and not done and utc(explicit) < utc()),
            'can_complete': actor == 'human' and kind not in {'practice', 'employer_requirement'}}


def _sort_key(action):
    return (action['state'] != 'pending', action['actor'] != 'human', action['priority'],
            action['deadline'] or '9999', action['created_at'] or '', action['id'])


def _mail_link(message):
    if _dict(message.get('context_json')).get('channel', 'email') != 'email':
        return None
    thread = message.get('thread_id') or message.get('message_id')
    return 'https://mail.google.com/mail/u/0/#all/' + quote(str(thread), safe='') if thread else None


def _response_material(message, *, fallback=None):
    payload = _dict(message.get('payload_json'))
    context = _dict(message.get('context_json'))
    return {'kind': 'message', 'id': message.get('message_key') or str(message.get('id', '')),
            'sender': message.get('sender') or payload.get('sender') or message.get('contact_name') or '',
            'subject': message.get('subject') or payload.get('subject') or '',
            'text': context.get('full_message') or message.get('excerpt') or payload.get('body')
                    or payload.get('snippet') or fallback or '',
            'received_at': message.get('received_at') or message.get('occurred_at'),
            'url': _mail_link(message)}


def _sent_packages(records):
    deliveries = {r['attempt_id']: r for r in records['application_email_deliveries']}
    packages = []
    for attempt in records['application_attempts']:
        if attempt['status'] != 'submitted':
            continue
        delivery = deliveries.get(attempt['attempt_id'], {})
        snapshot = _dict(attempt.get('snapshot_json'))
        packages.append({'id': attempt['attempt_id'], 'channel': attempt['channel'], 'target': attempt['target'],
            'sent_at': attempt.get('submitted_at'), 'state': 'submitted',
            'documents': _dict(attempt.get('artifact_hashes_json')),
            'proof_ids': obj(attempt.get('selected_proof_ids_json'), []),
            'contract': _dict(snapshot.get('application_contract_json')),
            'payload': _dict(delivery.get('payload_json')) or None,
            'verification': {'kind': attempt.get('proof_kind'), 'proof': attempt.get('proof')},
            'record_scope': 'saved email payload' if delivery.get('payload_json') else
                            'reserved documents and submission receipt; complete browser form text was not captured'})
    known = {item['id'] for item in packages}
    for submission in records['feedback_submissions']:
        if submission['submission_id'] not in known:
            packages.append({'id': submission['submission_id'], 'sent_at': submission['submitted_at'],
                'state': 'submitted', 'features': _dict(submission['features_json']),
                'record_scope': 'submission snapshot; exact outgoing package unavailable'})
    return sorted(packages, key=lambda item: item.get('sent_at') or '', reverse=True)


def _project(job, records, completions, automatic, practice, now):
    from applypilot.apply.application_contract import contract_for, contract_revision, missing_requirements

    contract = contract_for(job)
    submissions = sorted(records['feedback_submissions'], key=lambda r: (r['submitted_at'], r['submission_id']))
    events = [r for r in records['application_outcomes'] if not r.get('voided_at')]
    episode = [r for r in events if not submissions or r.get('submission_identifier') == submissions[-1]['submission_id']]
    status = current_state(episode, submitted_at=submissions[-1]['submitted_at'], now=now) if submissions else {}
    messages = {r['message_key']: r for r in records['feedback_messages']}
    outbox = {r['id']: r for r in records['referral_outbox']}
    actions, responses, represented = [], [], set()
    for event in events:
        key = event.get('message_key')
        result = _dict(event.get('payload_json'))
        material = _response_material(messages.get(key) or event,
                                      fallback=result.get('evidence_quote') or event.get('notes'))
        responses.append({'kind': event['event_type'], 'trust': event.get('trust'),
                          'material': material, 'occurred_at': event['occurred_at']})
        if event not in episode or event.get('trust') == 'legacy-unreviewed' or status.get('terminal'):
            continue
        auto = automatic.get(key, {})
        owner_input = auto.get('reason') in OWNER_INPUT
        delivery = outbox.get(auto.get('outbox_id'), {})
        for request in result.get('action_items', []):
            if not isinstance(request, dict) or not request.get('action') or not request.get('quote'):
                continue
            routine = request.get('requires_candidate') is False and not owner_input
            delegated = routine and auto.get('status') in {'waiting', 'drafted', 'sent'}
            identity = [key or event['id'], {field: request.get(field) for field in ('action', 'quote', 'date_iso')}]
            action = _action(job, 'employer_response', identity, request['action'],
                auto['reason'] if owner_input else 'The employer explicitly requested this next step.',
                actor='machine' if delegated else 'human', at=event['occurred_at'], deadline=request.get('date_iso'),
                materials=[material], priority=10, completion=completions)
            if delegated and auto.get('reason') in ANSWERED_INTENTS and delivery.get('state') == 'sent':
                action.update(state='complete', overdue=False, completion={'completed_at': delivery['sent_at'],
                    'basis': 'confirmed response to a routine request', 'outbox_id': delivery['id']})
            actions.append(action)
            if key:
                represented.add(key)
    for message in records['referral_messages']:
        key = message['message_key']
        material = _response_material(message)
        responses.append({'kind': message['suggested_kind'], 'trust': 'observed', 'material': material,
                          'occurred_at': message['received_at']})
        auto = automatic.get(key, {})
        if key in represented or (auto.get('reason') not in OWNER_INPUT and auto.get('status') != 'review'):
            continue
        actions.append(_action(job, 'relationship_response', [key, auto.get('reason')],
            'Supply the missing information' if auto.get('reason') in OWNER_INPUT else 'Resolve the conversation exception',
            auto['reason'], at=message['received_at'], materials=[material], priority=15, completion=completions))
        represented.add(key)
    for message in records['feedback_messages']:
        auto = automatic.get(message['message_key'], {})
        if message['state'] == 'review' and message['message_key'] not in represented and not auto:
            actions.append(_action(job, 'message_review', [message['message_key'], message.get('link_reason')],
                'Resolve the employer message', message.get('link_reason') or 'The incoming message could not be linked to an application.',
                at=message.get('received_at') or message.get('created_at'), materials=[_response_material(message)],
                priority=20, completion=completions))
    if not status.get('terminal'):
        for lane in practice:
            source = lane['next_action']
            material = source['supporting_material']
            session = lane.get('session')
            if not session or material.get('job_url') != job['url'] or source['completion_state'] != 'pending':
                continue
            action = _action(job, 'practice', [session['id'], session.get('revision')], 'Practice the next interview answer',
                source['reason'], deadline=source['deadline'], priority=70,
                materials=[{'kind': 'practice_session', 'id': session['id'], 'question': material['question'],
                            'completed_answers': len(session['turns']), 'focus': material.get('focus')}],
                target_url='/ui/career?' + urlencode({'job_url': job['url']}))
            action.update(deadline_basis=source['deadline_basis'], deadline_label='Practice target')
            actions.append(action)
        if not submissions and not job.get('applied_at') and job.get('state') not in {'archived', 'rejected'}:
            for requirement in missing_requirements(job, now=now):
                actions.append(_action(job, 'employer_requirement', [contract.get('source_hash'), requirement],
                    requirement.get('prompt') or requirement['reason'], requirement['reason'],
                    actor=requirement['actor'], deadline=contract.get('deadline'),
                    materials=[{**requirement, 'requirement_kind': requirement['kind'], 'kind': 'requirement'}], priority=40))
    decision = {'state': status.get('terminal') or status.get('stage') or job.get('state') or 'preparing',
                'reason': job.get('apply_error') or job.get('score_reasoning') or '',
                'evidence': [{'kind': 'outcome', 'id': r['id'], 'event_type': r['event_type']} for r in episode]}
    route = next(iter(records['application_routes']), None)
    attempts = [r for r in records['application_attempts'] if r['status'] in
                {'prepared', 'side_effect_intent', 'verification_required', 'verification_quarantined'}]
    if attempts:
        actions.append(_action(job, 'reconcile_submission', attempts[0]['attempt_id'], 'Resolve the existing application attempt',
            'The existing application workflow owns this attempt and its submission receipt.', actor='machine', priority=50))
    elif submissions and not status.get('terminal'):
        actions.append(_action(job, 'observe', submissions[-1]['submission_id'], 'Observe employer responses',
            'Continue observing the submitted application. Silence is not a rejection.', actor='machine', priority=90))
    actions.sort(key=_sort_key)
    return {'job': job, 'decision': decision, 'route': route, 'contract': contract,
            'contract_revision': contract_revision(job), 'evidence': _dict(job.get('selected_evidence')),
            'sent_packages': _sent_packages(records),
            'employer_responses': sorted(responses, key=lambda r: r.get('occurred_at') or '', reverse=True),
            'outreach': [{**r, 'payload': _dict(r.get('payload_json'))} for r in records['referral_outbox']],
            'relationship_events': [r for r in records['referral_events'] if not r.get('voided_at')],
            'career_reports': [{'id': r['id'], 'mode': r['mode'], 'state': r['state']} for r in records['career_ops_reports']],
            'actions': actions, 'next_action': next((a for a in actions if a['state'] == 'pending'), None), 'outcome': status}


def opportunity_view(conn, job_url, *, now=None):
    data = _snapshot(conn, job_url)
    if not data.get('jobs'):
        raise ValueError('Opportunity not found')
    grouped, completions, automatic = _group(data)
    return _project(data['jobs'][0], grouped[job_url], completions, automatic, data['practice'], utc(now))


def _actions(conn):
    data = _snapshot(conn, human_only=True)
    grouped, completions, automatic = _group(data)
    moment = utc()
    actions = [a for job in data.get('jobs', [])
               for a in _project(job, grouped[job['url']], completions, automatic, data['practice'], moment)['actions']]
    unlinked = grouped['']
    unknown = {'url': '', 'company': '', 'title': 'Unlinked employer engagement'}
    for table in ('feedback_messages', 'referral_messages'):
        for message in unlinked[table]:
            auto = automatic.get(message['message_key'], {})
            owner_input = auto.get('reason') in OWNER_INPUT or auto.get('status') == 'review'
            if not owner_input and (message.get('state') != 'review' or auto or
                                    (table == 'referral_messages' and message.get('case_id'))):
                continue
            actions.append(_action(unknown, 'unlinked_message', message['message_key'],
                'Identify the opportunity and respond', 'An incoming message needs linkage before automated handling can continue.',
                at=message.get('received_at') or message.get('created_at'), materials=[_response_material(message)],
                priority=20, completion=completions))
    return list({action['id']: action for action in actions}.values())


def next_actions(conn, *, limit=20):
    unique = {}
    for action in sorted(_actions(conn), key=_sort_key):
        if action['actor'] == 'human' and action['state'] == 'pending':
            unique.setdefault(action['job_url'] or action['id'], action)
    selected = list(unique.values())
    return selected if limit is None else selected[:max(0, int(limit))]


def complete_action(conn, action_id, *, note, effort_minutes=None):
    """Record explicit owner follow-through, without claiming a sent reply or outcome."""
    if not str(note or '').strip() or len(str(note)) > 4000:
        raise ValueError('Describe the completed action in 1–4000 characters')
    try:
        effort = None if effort_minutes in (None, '') else float(effort_minutes)
    except (TypeError, ValueError) as exc:
        raise ValueError('Effort must be minutes spent on this action') from exc
    if effort is not None and (not math.isfinite(effort) or not 0 <= effort <= 10080):
        raise ValueError('Effort must be between 0 and 10080 minutes')
    from applypilot.tracking.feedback_store import transaction
    with transaction(conn):
        action = next((a for a in _actions(conn) if a['id'] == action_id), None)
        if action is None:
            raise ValueError('Action no longer exists; reload the opportunity')
        if action['state'] == 'complete':
            return action
        if not action['can_complete']:
            raise ValueError('Complete this action in its existing application or practice workflow')
        stamp = iso()
        details = {'action_id': action_id, 'job_url': action['job_url'], 'kind': action['kind'],
                   'note': str(note).strip(), 'effort_minutes': effort, 'action_created_at': action['created_at'],
                   'deadline': action['deadline'], 'completion_basis': 'owner_reported'}
        message = next((m.get('id') for m in action['materials'] if m.get('kind') == 'message'), None)
        conn.execute('INSERT INTO feedback_reviews(message_key,action,actor,details_json,occurred_at) VALUES(?,?,?,?,?)',
                     (message, COMPLETION, 'owner', dump(details), stamp))
    return {**action, 'state': 'complete', 'completion': details | {'completed_at': stamp, 'completed_by': 'owner'}}

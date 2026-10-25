"""Observed pursuit strategies and bottlenecks over the existing outcome history."""
from __future__ import annotations

from collections import defaultdict
import math
from statistics import median

from .feedback_store import obj, utc


def _sent_evidence(conn, attempt):
    """Credit only content observed in the submitted package, not selected inventory."""
    if not attempt:
        return {'evidence_ids': [], 'documents': [], 'responses': [], 'usage': 'not_recorded'}
    manifest = obj(attempt.get('artifact_hashes_json'))
    responses = {}
    document_kinds, response_ids, payload_hash = set(), set(), None
    if attempt.get('channel') == 'email':
        row = conn.execute("SELECT payload_json,payload_hash FROM application_email_deliveries "
                           "WHERE attempt_id=? AND state='sent'", (attempt['attempt_id'],)).fetchone()
        if row:
            payload = obj(row['payload_json'])
            payload_hash = row['payload_hash']
            contract = payload.get('application_contract') or {}
            responses = contract.get('responses', {})
            document_kinds = {item['kind'] for item in payload.get('attachments', []) if item.get('kind')}
            response_ids = {identifier for identifier, response in responses.items()
                            if response.get('text') and response['text'] in payload.get('body', '')}
    else:
        document_kinds = {kind for kind, receipt in obj(attempt.get('browser_uploads_json')).get('documents', {}).items()
                          if receipt.get('status') == 'verified'}
    evidence_ids, documents, answered = set(), [], []
    for kind in sorted(document_kinds):
        artifact = manifest.get(kind)
        if not artifact:
            continue
        usage = artifact.get('document_context') or {}
        supplied = set(usage.get('supplied_source_ids') or [])
        used = [key for key in usage.get('used_source_ids', []) if key in supplied]
        evidence_ids.update(used)
        documents.append({'kind': kind, 'sha256': artifact.get('files', {}).get(artifact.get('path')),
                          'evidence_ids': used, 'usage': 'model_reported' if used else 'not_recorded'})
    for identifier in sorted(response_ids):
        response = responses.get(identifier, {})
        ids = [value for value in response.get('evidence_ids', []) if isinstance(value, str)]
        evidence_ids.update(ids)
        answered.append({'requirement_id': identifier, 'evidence_ids': ids,
                         'proof_links': response.get('proof_links', []),
                         'text': response.get('text', ''), 'actor': response.get('actor')})
    return {'evidence_ids': sorted(evidence_ids), 'documents': documents, 'responses': answered,
            'payload_hash': payload_hash, 'usage': 'observed_package_with_declared_citations'}


def submission_strategy(conn, features, *, job_url, submitted_at, attempt=None):
    networking = features.get('networking_at_submission') or {}
    strategies = ['direct_application']
    if set(networking.get('assistance', [])) & {'recruiter_intro', 'hiring_manager_intro', 'ats_referral',
                                               'employee_endorsement', 'resume_forwarded', 'application_flagged'}:
        strategies.append('confirmed_assistance')
    if set(networking.get('assistance', [])) & {'recruiter_intro', 'hiring_manager_intro'}:
        strategies.append('genuine_introduction')
    if networking.get('exposures'):
        strategies.append('direct_outreach')
    if any('follow' in exposure.get('kind', '') for exposure in networking.get('exposures', [])):
        strategies.append('follow_up')
    if any(exposure.get('kind', '').startswith('reply:') for exposure in networking.get('exposures', [])):
        strategies.append('candidate_response')
    package = _sent_evidence(conn, attempt)
    if package['evidence_ids'] or any(response['proof_links'] for response in package['responses']):
        strategies.append('evidence_in_application')
    turns = []
    for row in conn.execute('SELECT id,turns_json FROM career_ops_sessions WHERE job_url=?', (job_url,)):
        for turn in obj(row['turns_json'], []):
            if turn.get('answer') and turn.get('answered_at') and utc(turn['answered_at']) <= utc(submitted_at):
                turns.append({'session_id': row['id'], 'report_id': turn.get('report_id'),
                              'occurred_at': turn['answered_at'], 'effort_minutes': turn.get('effort_minutes')})
    if turns:
        strategies.append('completed_interview_practice')
    effort = []
    for row in conn.execute("""SELECT details_json FROM feedback_reviews
        WHERE action='opportunity_action_completed' AND occurred_at<=?""", (submitted_at,)):
        detail = obj(row['details_json'])
        if detail.get('job_url') == job_url and _known_effort(detail.get('effort_minutes')):
            effort.append(detail['effort_minutes'])
    effort.extend(turn['effort_minutes'] for turn in turns if _known_effort(turn.get('effort_minutes')))
    return {'version': 1, 'strategies': strategies, 'evidence_ids': package['evidence_ids'],
            'selected_evidence_ids': features.get('proof_ids') or [], 'sent_package': package,
            'practice_answers': turns, 'human_effort_minutes': sum(effort) if effort else None,
            'effort_observations': len(effort),
            'assistance_event_ids': networking.get('event_ids', []),
            'captured_at': submitted_at, 'basis': 'recorded_before_submission'}


def _known_effort(value):
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _action_history(conn):
    history = defaultdict(list)
    for row in conn.execute("""SELECT r.*,l.job_url FROM referral_events r
            JOIN referral_job_links l ON l.case_id=r.case_id WHERE r.voided_at IS NULL
            ORDER BY r.occurred_at,r.id"""):
        item = dict(row)
        details = obj(item['details_json'])
        if item['kind'] in {'outreach_sent', 'manual_outreach'} and details.get('outbox_id'):
            continue
        history[item['job_url']].append({'kind': item['kind'], 'at': item['occurred_at'],
            'record_id': item['id'], 'trust': item['trust'], 'contact_id': item['contact_id'],
            'evidence_ids': details.get('proof_ids') or details.get('evidence_ids') or [],
            'outbox_id': details.get('outbox_id'), 'human_effort_minutes': details.get('effort_minutes')})
    for row in conn.execute("""SELECT o.*,l.job_url FROM referral_outbox o
            JOIN referral_job_links l ON l.case_id=o.case_id WHERE o.sent_at IS NOT NULL
            ORDER BY o.sent_at,o.id"""):
        payload = obj(row['payload_json'])
        kind = ('follow_up' if 'follow' in row['kind'] else
                'candidate_response' if row['kind'].startswith('reply:') else 'direct_outreach')
        history[row['job_url']].append({'kind': kind,
            'at': row['sent_at'], 'record_id': row['id'], 'contact_id': row['contact_id'],
            'recipient': payload.get('to'), 'subject': payload.get('subject'),
            'evidence_ids': payload.get('evidence_ids') or payload.get('proof_ids') or
                            [p['source_id'] for p in payload.get('proofs', []) if p.get('source_id')],
            'proof': payload.get('proofs') or [],
            'trust': 'sent', 'human_effort_minutes': None})
    for row in conn.execute("""SELECT * FROM feedback_reviews
            WHERE action='opportunity_action_completed' ORDER BY occurred_at,id"""):
        details = obj(row['details_json'])
        if details.get('job_url'):
            history[details['job_url']].append({'kind': details.get('kind') or 'candidate_action',
                'at': row['occurred_at'], 'record_id': str(row['id']), 'trust': 'owner_recorded',
                'human_effort_minutes': details.get('effort_minutes'), 'note': details.get('note'),
                'action_id': details.get('action_id')})
    for row in conn.execute('SELECT id,job_url,turns_json FROM career_ops_sessions'):
        for turn in obj(row['turns_json'], []):
            if turn.get('answer') and turn.get('answered_at'):
                history[row['job_url']].append({'kind': 'completed_interview_practice',
                    'at': turn['answered_at'], 'record_id': turn.get('report_id'),
                    'session_id': row['id'], 'trust': 'candidate_answer',
                    'human_effort_minutes': turn.get('effort_minutes')})
    return history


def report(conn, *, data=None, cfg=None, now=None):
    from .feedback_learning import dataset, wilson
    from .feedback_store import policy
    cfg = cfg or policy(conn)
    now = utc(now)
    if data is None:
        data, _ = dataset(conn, cfg=cfg, now=now)
    history = _action_history(conn)
    cohorts = defaultdict(list)
    sequences = []
    missing_strategy = 0
    repeated_submissions = 0
    counted_jobs = set()
    ordered = sorted(data, key=lambda row: (utc(row['submitted_at']), row['submission_id']))
    next_submission = {}
    later = {}
    for row in reversed(ordered):
        next_submission[row['submission_id']] = later.get(row['job_url'])
        later[row['job_url']] = utc(row['submitted_at'])
    for row in ordered:
        features = row['features']
        strategy = features.get('pursuit_at_submission') or {}
        labels = strategy.get('strategies') or []
        if not labels:
            missing_strategy += 1
        cohort_included = row['included'] and bool(labels) and row['job_url'] not in counted_jobs
        if row['included'] and labels and row['job_url'] in counted_jobs:
            repeated_submissions += 1
        if cohort_included:
            counted_jobs.add(row['job_url'])
            key = tuple(str(value) for value in (
                   features.get('market') or 'unknown', features.get('role_family') or 'unknown',
                   features.get('role_tier') or 'unknown', features.get('fit_score') or 'unknown',
                   features.get('channel') or 'unknown', '|'.join(sorted(set(labels)))))
            cohorts[key].append(row)
        timeline = [{'kind': 'confirmed_submission', 'at': row['submitted_at'],
                     'record_id': row['submission_id'], 'trust': 'confirmed'}]
        boundary = next_submission[row['submission_id']]
        timeline += [dict(action) for action in history[row['job_url']]
                     if utc(action['at']) <= now and (boundary is None or utc(action['at']) < boundary)]
        timeline += [{'kind': e['event_type'], 'at': e['occurred_at'], 'record_id': e['id'],
                      'trust': e['trust']} for e in row['events']]
        for action in timeline:
            action['timing'] = ('before_submission' if utc(action['at']) < utc(row['submitted_at'])
                                else 'at_or_after_submission')
        sequences.append({'job_url': row['job_url'], 'title': features.get('title'),
            'company': features.get('company'), 'submission_id': row['submission_id'],
            'strategies': labels, 'evidence_ids': strategy.get('evidence_ids', []),
            'selected_evidence_ids': features.get('proof_ids', []),
            'sent_package': strategy.get('sent_package'), 'cohort_included': cohort_included,
            'included': row['included'], 'excluded_reason': row['excluded_reason'],
            'outcomes': row['milestones'], 'actions': sorted(timeline, key=lambda a: utc(a['at']))})
    groups = []
    for key, members in sorted(cohorts.items()):
        count, positive = len(members), sum(r['target'] for r in members)
        response_delays = []
        effort = []
        effort_observations = 0
        for row in members:
            replies = [utc(e['occurred_at']) for e in row['within_events']
                       if e['event_type'] in {'recruiter_response', 'screen', 'interview', 'technical_interview'}]
            if replies:
                response_delays.append((min(replies) - utc(row['submitted_at'])).total_seconds() / 3600)
            snapshot_effort = (row['features'].get('pursuit_at_submission') or {}).get('human_effort_minutes')
            if _known_effort(snapshot_effort):
                effort.append(snapshot_effort)
                effort_observations += (row['features'].get('pursuit_at_submission') or {}).get('effort_observations', 0)
        missing_context = [name for name, value in zip(
            ('market', 'role_family', 'role_tier', 'fit_band', 'channel'), key[:5]) if value == 'unknown']
        groups.append(dict(zip(('market', 'role_family', 'role_tier', 'fit_band', 'channel', 'strategy'), key)) | {
            'submissions': count, 'engagement': positive, 'observed_rate': positive / count,
            'interval95': wilson(positive, count), 'sufficient': count >= cfg['min_cell'] and not missing_context,
            'missing_comparison_context': missing_context,
            'median_reply_hours': median(response_delays) if response_delays else None,
            'recorded_effort_minutes': sum(effort) if effort else None,
            'effort_observations': effort_observations, 'submissions_with_effort': len(effort)})
    from applypilot.career_ops.readiness import overview
    return {'cohorts': groups, 'sequences': sequences[-100:][::-1],
            'practice': overview(conn), 'missing_strategy_snapshots': missing_strategy,
            'repeat_submissions_excluded': repeated_submissions,
            'causal_claim': False, 'ranking_changed': False,
            'note': 'Associations within recorded market, role, fit and channel groups; selection differences remain. '
                    'Only mature, observed submissions enter cohorts. Later actions stay on the timeline and are not '
                    'retroactively credited as application strategy. One mature submission per opportunity contributes '
                    'to comparisons. Selected proof is not proof sent. Document citations are model-reported. '
                    'Unknown effort is not zero. No route is disabled by these comparisons.'}

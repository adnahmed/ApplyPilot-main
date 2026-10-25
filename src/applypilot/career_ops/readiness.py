"""Recurring practice projected from pursued jobs and existing interview sessions."""
from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from uuid import uuid4

from applypilot.discovery.repository import transaction
from applypilot.tracking.feedback_store import utc

from . import store

CADENCE_DAYS = 7


def _date(value):
    try:
        return utc(value) if value else None
    except (ValueError, TypeError):
        return None


def _lane(job):
    label = ' '.join(str(job.get('role_family') or job.get('title') or 'Saved role').split())
    return label.casefold(), label


def pursued_jobs(conn, *, job_url=None):
    query = """SELECT j.* FROM jobs j WHERE
        COALESCE(j.state,'') NOT IN ('archived','rejected')
        AND COALESCE(j.tracking_status,'') NOT IN ('rejected','withdrawn')
        AND (COALESCE(j.discovery_source_status,'unknown')<>'closed' OR j.applied_at IS NOT NULL)
        AND COALESCE(NULLIF(TRIM(j.full_description),''),NULLIF(TRIM(j.description),'')) IS NOT NULL
        AND (NULLIF(TRIM(j.tailored_resume_path),'') IS NOT NULL OR j.applied_at IS NOT NULL
            OR EXISTS(SELECT 1 FROM referral_job_links l JOIN referral_cases c ON c.id=l.case_id
                WHERE l.job_url=j.url AND c.state IN ('research','draft','waiting')))"""
    if job_url:
        query += ' AND j.url=?'
    query += ' ORDER BY j.fit_score DESC NULLS LAST,j.discovered_at DESC,j.url'
    rows = conn.execute(query, (job_url,) if job_url else ()).fetchall()
    return [dict(row) for row in rows]


def _sessions(conn):
    result = []
    for row in conn.execute('SELECT * FROM career_ops_sessions ORDER BY updated_at DESC'):
        value = dict(row)
        value['turns'] = json.loads(value.pop('turns_json'))
        value['context'] = json.loads(value.pop('context_json'))
        result.append(value)
    return result


def progress(sessions):
    """Report observed answers and comparable topic feedback, never an ability score."""
    topics = defaultdict(list)
    completed, effort = 0, []
    for session in sessions:
        for turn in session['turns']:
            if not str(turn.get('answer') or '').strip():
                continue
            completed += 1
            if type(turn.get('effort_minutes')) is int:
                effort.append(turn['effort_minutes'])
            per_turn = {}
            for item in turn.get('feedback', []):
                topic = ' '.join(str(item.get('topic') or '').casefold().split())
                dimension = item.get('dimension', 'unknown')
                if not topic or dimension == 'unknown':
                    continue
                per_turn[(dimension, topic)] = {
                    'session_id': session['id'], 'report_id': turn.get('report_id'),
                    'answered_at': turn.get('answered_at', ''), 'rating': item.get('rating', 'unknown'),
                    'answer_quote': item.get('answer_quote', ''), 'gap': item.get('gap', ''),
                    'improvement': item.get('improvement', ''),
                }
            for key, observation in per_turn.items():
                topics[key].append(observation)
    signals = []
    for (dimension, topic), observations in topics.items():
        observations.sort(key=lambda item: item['answered_at'])
        gaps = [item for item in observations if item['rating'] in {'needs_practice', 'developing'}]
        ratings = {'needs_practice': 0, 'developing': 1, 'demonstrated': 2}
        comparable = [item for item in observations if item['rating'] in ratings]
        improved = (len(comparable) >= 2 and
                    ratings[comparable[-1]['rating']] > ratings[comparable[0]['rating']])
        unresolved = bool(gaps and observations[-1]['rating'] in {'needs_practice', 'developing'})
        signals.append({'dimension': dimension, 'topic': topic, 'observations': len(observations),
                        'sessions': len({item['session_id'] for item in observations}),
                        'repeated_gap': len(gaps) >= 2 and unresolved,
                        'observed_improvement': improved, 'latest': observations[-1],
                        'intervention': observations[-1]['improvement'],
                        'confidence': 'provisional',
                        'interpretation': 'Model feedback on completed answers; questions may differ. '
                                          'This is not a definitive measure of ability.'})
    signals.sort(key=lambda item: (not item['repeated_gap'], -item['observations'], item['topic']))
    return {'completed_answers': completed, 'sessions_with_answers': sum(bool(s['turns']) for s in sessions),
            'sessions_considered': len(sessions),
            'reported_effort_minutes': sum(effort) if effort else None,
            'answers_with_reported_effort': len(effort), 'signals': signals}


def overview(conn, job_url=None):
    jobs = pursued_jobs(conn, job_url=job_url)
    grouped = defaultdict(list)
    for job in jobs:
        grouped[_lane(job)[0]].append(job)
    sessions = _sessions(conn)
    lanes = []
    for key, members in grouped.items():
        urls = {job['url'] for job in members}
        relevant = [session for session in sessions if session['job_url'] in urls
                    or (not job_url and session['context'].get('practice_lane') == key)]
        active = next((session for session in relevant if session['job_url'] in urls
                       and session['next_question'].strip()), None)
        metrics = progress(relevant)
        deadline = None
        deadline_basis = 'weekly practice cadence'
        if active:
            start = _date(active['updated_at']) or datetime.now(UTC)
            deadline = (start + timedelta(days=CADENCE_DAYS) if active['turns'] else start).isoformat()
            employer_due = next((_date(job.get('next_action_due')) for job in members
                                 if job['url'] == active['job_url']), None)
            if employer_due and employer_due < _date(deadline):
                deadline, deadline_basis = employer_due.isoformat(), 'saved employer deadline'
        repeated = next((signal for signal in metrics['signals'] if signal['repeated_gap']), None)
        lanes.append({'key': key, 'label': _lane(members[0])[1], 'jobs': [job['url'] for job in members],
                      'representative_job': members[0]['url'], 'session': active, 'progress': metrics,
                      'next_action': {'actor': 'human', 'kind': 'practice',
                          'reason': (repeated['intervention'] if repeated else
                                     'Practice a role you are actively pursuing before the next interview.'),
                          'deadline': deadline, 'deadline_basis': deadline_basis,
                          'supporting_material': {'session_id': active['id'] if active else None,
                              'question': active['next_question'] if active else '',
                              'job_url': active['job_url'] if active else members[0]['url'],
                              'focus': repeated},
                          'completion_state': ('pending' if _date(deadline) <= datetime.now(UTC)
                                               else 'scheduled') if active else 'not_scheduled'}})
    return {'lanes': lanes, 'cadence_days': CADENCE_DAYS,
            'progress': progress([session for session in sessions
                                 if not job_url or session['job_url'] == job_url])}


def seed(conn, limit=100):
    """Queue only opening questions; unanswered sessions suppress document churn."""
    queued = []
    current = datetime.now(UTC)
    with transaction(conn, resource='career-ops'):
        view = overview(conn)
        pending = [dict(row) for row in conn.execute("""SELECT job_url,state,updated_at
            FROM career_ops_reports WHERE mode='interview_practice'
            AND state IN ('pending','running','error','cancelled')""")]
        active_jobs = {url for lane in view['lanes'] for url in lane['jobs']}
        pending = [row for row in pending if row['job_url'] in active_jobs]
        for lane in view['lanes']:
            if len(queued) >= limit:
                break
            if lane['session']:
                continue
            related = [row for row in pending if row['job_url'] in lane['jobs']]
            if any(row['state'] != 'cancelled' or
                   (_date(row['updated_at']) or current) > current-timedelta(days=CADENCE_DAYS)
                   for row in related):
                continue
            window = int(current.timestamp()) // (CADENCE_DAYS*86400)
            trigger = hashlib.sha256(store.dump(
                ['role-practice', lane['key'], lane['representative_job'], window]).encode()).hexdigest()
            identifier = uuid4().hex
            source = {'id': 'role-practice:' + trigger, 'channel': 'pursued role practice',
                      'practice_lane': lane['key'], 'lane_label': lane['label'],
                      'job_urls': lane['jobs']}
            request = {'job_url': lane['representative_job'],
                       'instructions': 'Practice this pursued role before an invitation. Use one grounded '
                           'question and wait for the candidate answer. Separate missing evidence, '
                           'communication difficulties and demonstrated skill gaps.'}
            changed = conn.execute("""INSERT INTO career_ops_reports
                (id,mode,job_url,state,input_json,source_json,trigger_key,attempts,created_at,updated_at)
                VALUES(?,'interview_practice',?,'pending',?,?,?,0,?,?)
                ON CONFLICT(trigger_key) DO NOTHING""",
                (identifier, lane['representative_job'], store.dump(request), store.dump(source),
                 trigger, current.isoformat(), current.isoformat()))
            if changed.rowcount:
                queued.append(identifier)
    return {'queued': queued, 'cadence_days': CADENCE_DAYS}

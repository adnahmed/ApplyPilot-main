"""Results attributed to a retained run by persisted identities."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from applypilot.operations import store as journal
from .data import CONFIRMED_SUBMISSION_CONFIDENCE, now_iso, tables
from .work_export import _job_map, _linked_records, _read_applications, _transitions


def _day(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return str(parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).date())
    except (ValueError, TypeError):
        return None


def summary(root, store, run_id, days):
    with journal.reader(root) as conn:
        conn.execute('BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY')
        saved_run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if saved_run is None:
            raise ValueError('That run is no longer retained. Reload the run list.')
        run = dict(saved_run)
        task_ids = {row[0] for row in conn.execute(
            "SELECT DISTINCT task_id FROM events WHERE run_id=? AND task_id!=''", (run_id,))}
    with store.read() as conn:
        if conn is None:
            return {'database_available': False, 'run_id': run_id, 'run': run}
        conn.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        if 'jobs' not in tables(conn):
            return {'database_available': False, 'run_id': run_id, 'run': run}
        attempts, outcomes, _ = _read_applications(conn, run_id, task_ids)
        transitions = _transitions(conn, run_id)
        outreach, coverage = _linked_records(conn, 'referral_outbox', run_id,
            ('id', 'case_id', 'state', 'attempted_at', 'sent_at', 'created_run_id', 'dispatch_run_id'),
            run_columns=('created_run_id', 'dispatch_run_id'))
        urls = {row['job_url'] for row in [*attempts, *transitions] if row.get('job_url')}
        jobs = _job_map(conn, urls)
    recorded_rows = sorted((row for row in attempts if row['status'] == 'submitted' and row.get('submitted_at')),
                           key=lambda row: _day(row['submitted_at']) or '', reverse=True)
    recorded = {row['job_url']: row for row in recorded_rows}
    submitted = {row['job_url']: row for row in recorded_rows
                 if row.get('verification_confidence') in CONFIRMED_SUBMISSION_CONFIDENCE}
    sent = [row for row in outreach if row.get('dispatch_run_id') == run_id
            and row.get('state') in {'sent', 'manual_sent'} and row.get('sent_at')]
    outcomes = [row for row in outcomes if not row.get('voided_at')]
    by_company = defaultdict(lambda: {'value': 0, 'submitted': 0})
    for url, job in jobs.items():
        name = job.get('company') or 'Unknown company'
        by_company[name]['value'] += 1
        by_company[name]['submitted'] += int(url in submitted)
    recorded_days = [_day(row['submitted_at']) for row in recorded.values()]
    recorded_days.extend(_day(row['sent_at']) for row in sent)
    end = max([datetime.fromtimestamp(run['last_at'], timezone.utc).date(),
               *(date.fromisoformat(day) for day in recorded_days if day)])
    start = max(datetime.fromtimestamp(run['started_at'], timezone.utc).date(), end - timedelta(days=days - 1))
    series = {str(start + timedelta(days=i)): {'date': str(start + timedelta(days=i)),
              'discovered': 0, 'submitted': 0, 'outreach': 0} for i in range((end - start).days + 1)}
    worked = {}
    for row in [*transitions, *attempts]:
        day = _day(row.get('at') or row.get('prepared_at'))
        if day and (row['job_url'] not in worked or day < worked[row['job_url']]):
            worked[row['job_url']] = day
    for measure, times in (('discovered', worked.values()),
                           ('submitted', (row['submitted_at'] for row in submitted.values())),
                           ('outreach', (row['sent_at'] for row in sent))):
        for at in times:
            if _day(at) in series:
                series[_day(at)][measure] += 1
    outcome_counts = Counter(row['event_type'] for row in outcomes)
    outcome_jobs = defaultdict(set)
    for row in outcomes:
        outcome_jobs[row['event_type']].add(row['job_url'])
    groups = {'responses': ('recruiter_response',),
              'interviews': ('screen', 'interview', 'technical_interview', 'interview_completed'),
              'offers': ('offer',)}
    counts = Counter('reported_unverified' if row['status'] == 'submitted'
                     and row.get('verification_confidence') not in CONFIRMED_SUBMISSION_CONFIDENCE
                     else row['status'] for row in attempts)
    return {'at': now_iso(), 'days': days, 'timezone': 'UTC', 'database_available': True,
        'run_id': run_id, 'run': run, 'scope': 'run',
        'state_basis': 'current_committed_outcomes',
        'scope_note': 'Current outcomes for work linked to this run. Later confirmations and replies are included; unlinked history is excluded.',
        'totals': {'jobs': len(urls), 'companies': len(by_company), 'submitted': len(submitted),
                   'reported_unverified': len(recorded.keys() - submitted.keys()),
                   'outreach_sent': len(sent), 'attempts': len(attempts),
                   'verification_holds': counts['verification_required'] + counts['verification_quarantined'],
                   'cancelled': counts['cancelled'] + counts['cancelled_before_submit'],
                   'unfinished': sum(counts[state] for state in ('prepared', 'side_effect_intent'))},
        'outcome_groups': {name: len(set().union(*(outcome_jobs[kind] for kind in kinds)))
                           for name, kinds in groups.items()},
        'outcomes': [{'label': name, 'value': count, 'jobs': len(outcome_jobs[name])}
                     for name, count in outcome_counts.most_common()],
        'stages': [{'label': name, 'value': count} for name, count in counts.most_common()],
        'series': list(series.values()),
        'period': {key: sum(row[key] for row in series.values()) for key in ('discovered', 'submitted', 'outreach')},
        'top_companies': [{'label': name, **values} for name, values in
                         sorted(by_company.items(), key=lambda item: (-item[1]['value'], item[0]))[:8]],
        'coverage': {'unlinked_outreach': coverage.get('unlinked_records', 0)}}

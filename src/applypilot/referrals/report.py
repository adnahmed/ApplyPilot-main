"""Networking diagnostics and observational cohorts, not causal referral multipliers."""
from __future__ import annotations

from collections import Counter, defaultdict
from applypilot.referrals import store as s


def report(conn, *, now=None):
    from applypilot.tracking.feedback_learning import dataset
    data, coverage = dataset(conn, now=now)
    cohorts = defaultdict(lambda: {'submissions': 0, 'mature_observed': 0, 'positive_within_horizon': 0,
                                   'eventual_interview_or_offer': 0})
    strata = defaultdict(lambda: {'submissions': 0, 'mature_observed': 0, 'positive_within_horizon': 0})
    submissions = []
    for item in data:
        pre = s.snapshot(conn, item['job_url'], at=item['submitted_at'])
        later = s.snapshot(conn, item['job_url'], at=now)
        # Freeze initial observations in feedback snapshot; correction-aware as-of
        # view is separately reported, never silently overwrites submitted history.
        frozen = item['features'].get('networking_at_submission', {'status': 'unknown'})
        key = 'assistance_before' if pre.get('assistance') else 'outreach_before' if pre.get('outreach_started_at') else 'no_recorded_networking_before'
        cell = cohorts[key]
        cell['submissions'] += 1
        if item['included']:
            cell['mature_observed'] += 1
            cell['positive_within_horizon'] += item['target']
        exposure = next((x for x in pre.get('exposures', []) if x.get('kind') == 'request'), {})
        dimensions = {'source': str(item['features'].get('source') or 'unknown'),
                      'relationship_at_outreach': exposure.get('relationship', 'not_recorded'),
                      'function_alignment': exposure.get('alignment', 'unknown')}
        for dimension, value in dimensions.items():
            bucket = strata[(dimension, value, key)]
            bucket['submissions'] += 1
            if item['included']:
                bucket['mature_observed'] += 1
                bucket['positive_within_horizon'] += item['target']
        cell['eventual_interview_or_offer'] += int(bool(set(item['milestones']) & {'interview','technical_interview','interview_completed','offer','accepted','hired'}))
        submissions.append({'submission_id': item['submission_id'], 'job_url': item['job_url'],
                            'submitted_at': item['submitted_at'], 'cohort': key,
                            'dimensions': dimensions, 'before_corrected': pre, 'current_assistance': later.get('assistance', []),
                            'frozen_at_submission': frozen,
                            'observation_status': 'mature_observed' if item['included'] else item['excluded_reason']})
    states = dict(Counter(r[0] for r in conn.execute('SELECT state FROM referral_outbox')))
    kinds = dict(Counter(r[0] for r in conn.execute('SELECT kind FROM referral_events WHERE voided_at IS NULL')))
    return {'policy': s.policy(conn), 'outbox': states, 'events': kinds,
            'cases_considered': conn.execute('SELECT COUNT(*) FROM referral_cases').fetchone()[0],
            'cases_without_known_contact': conn.execute("SELECT COUNT(*) FROM referral_cases WHERE ap_json_array_length(plan_json,'$.candidates')=0").fetchone()[0],
            'active_holds': conn.execute('SELECT COUNT(*) FROM referral_cases WHERE ap_day(hold_until)>ap_day(?)', (s.iso(now),)).fetchone()[0],
            'messages_needing_review': conn.execute("SELECT COUNT(*) FROM referral_messages WHERE state='review'").fetchone()[0],
            'cohorts': dict(cohorts), 'strata': [{'dimension': d, 'value': v, 'cohort': c, **value} for (d, v, c), value in sorted(strata.items())], 'coverage': coverage, 'submissions': submissions,
            'interpretation': 'Observational, selected populations. No causal multiplier; offers/interviews come only from the application feedback ledger. Unknown external referrals remain unknown. Late assistance is not assigned to the pre-application cohort.'}

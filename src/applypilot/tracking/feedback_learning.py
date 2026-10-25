"""Local, delayed-outcome learning. No model calls and no eligibility decisions.

We estimate *recorded meaningful engagement within a fixed observation horizon*,
not a hiring probability or a causal effect of a source/document. Young sends are
right-censored; silence is never manufactured into a rejection event. All stages
are reported, but only the declared engagement target drives optional ranking.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import math
from applypilot import postgres

from .feedback_store import (backfill_submissions, digest, dump,
                             iso, job_features, obj, policy, positive_engagement, transaction, utc)

DIMENSIONS = ('source', 'market', 'role_family', 'company', 'document_variant')
RANK_WEIGHTS = {'source': .4, 'market': .35, 'role_family': .25, 'company': .15}


def wilson(successes, n, z=1.96):
    if n == 0:
        return [0.0, 1.0]
    p = successes / n
    base = 1 + z * z / n
    center = (p + z * z / (2 * n)) / base
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / base
    return [max(0.0, center - half), min(1.0, center + half)]


def dataset(conn, *, now=None, cfg=None) -> tuple[list[dict], dict]:
    now = utc(now)
    cfg = cfg or policy(conn)
    horizon = timedelta(days=cfg['horizon_days'])
    cutoff = now - timedelta(days=cfg['lookback_days'])
    rows = conn.execute('SELECT * FROM feedback_submissions ORDER BY submitted_at,submission_id').fetchall()
    event_map = defaultdict(list)
    for row in conn.execute("SELECT * FROM application_outcomes WHERE voided_at IS NULL AND trust IN ('automatic-v1','manual')"):
        event_map[row['submission_identifier']].append(dict(row))
    coverage = [(utc(r[0]), utc(r[1])) for r in conn.execute(
        'SELECT coverage_start,coverage_end FROM feedback_mailboxes WHERE coverage_start IS NOT NULL AND coverage_end IS NOT NULL')]
    manual_coverage = {r[0]:utc(r[1]) for r in conn.execute('SELECT submission_id,observed_through FROM feedback_observations')}
    queued = conn.execute("SELECT COUNT(*) FROM feedback_messages WHERE state IN ('pending','retry')").fetchone()[0]
    ambiguous = set()
    reviews = 0
    for row in conn.execute("SELECT submission_id,candidates_json FROM feedback_messages WHERE state='review'"):
        reviews += 1
        if row[0]:
            ambiguous.add(row[0])
        for candidate in obj(row[1], []):
            if isinstance(candidate, dict) and candidate.get('submission_id'):
                ambiguous.add(candidate['submission_id'])
    summary = {'total_submissions': len(rows), 'mature_observed': 0, 'young': 0,
               'unobserved': 0, 'ambiguous': 0, 'outside_window': 0, 'unknown_submission_time': 0, 'missing_submission_snapshot': 0,
               'pending_messages': queued, 'review_messages': reviews, 'late_events': 0,
               'eventual_positive': 0, 'fixed_horizon_positive': 0,
               'legacy_unreviewed_events': conn.execute("SELECT COUNT(*) FROM application_outcomes WHERE trust='legacy-unreviewed' AND voided_at IS NULL").fetchone()[0]}
    data = []
    for row in rows:
        r = dict(row)
        submitted = utc(r['submitted_at'])
        horizon_end = submitted + horizon
        events = [e for e in event_map[r['submission_id']] if submitted <= utc(e['occurred_at']) <= now]
        within = [e for e in events if utc(e['occurred_at']) <= horizon_end]
        reached = {e['event_type'] for e in events}
        outcome = positive_engagement(within)
        summary['late_events'] += sum(utc(e['occurred_at']) > horizon_end for e in events)
        summary['eventual_positive'] += int(positive_engagement(events))
        reason = None
        if r['snapshot_origin'] == 'prior-submission-time-unknown':
            reason = 'unknown_submission_time'
        elif r['snapshot_origin']=='historical-current-fields':
            reason='missing_submission_snapshot'
        elif submitted < cutoff:
            reason = 'outside_window'
        elif horizon_end > now:
            reason = 'young'
        elif r['submission_id'] in ambiguous:
            reason = 'ambiguous'
        elif manual_coverage.get(r['submission_id'],submitted) < horizon_end and (queued or not any(start <= submitted and end >= horizon_end for start, end in coverage)):
            reason = 'unobserved'
        if reason:
            summary[reason] += 1
        else:
            summary['mature_observed'] += 1
            summary['fixed_horizon_positive'] += int(outcome)
        features = obj(r['features_json'])
        data.append({'submission_id': r['submission_id'], 'job_url': r['job_url'],
                     'submitted_at': r['submitted_at'], 'features': features,
                     'target': int(outcome), 'included': reason is None, 'excluded_reason': reason,
                     'events': events, 'within_events': within,
                     'milestones': sorted(reached), 'horizon_end': iso(horizon_end)})
    return data, summary


def build_model(conn, *, now=None) -> dict:
    with transaction(conn):
        return _build_model(conn, now=now)


def _build_model(conn, *, now=None) -> dict:
    cfg = policy(conn)
    now = utc(now)
    backfill_submissions(conn)
    data, summary = dataset(conn, now=now, cfg=cfg)
    mature = [r for r in data if r['included']]
    n = len(mature)
    s = sum(r['target'] for r in mature)
    # Smoothed global prior avoids 0/1 priors on small histories.
    global_rate = (s + 1) / (n + 2)
    groups = defaultdict(lambda: [0, 0])
    for r in mature:
        for dimension in DIMENSIONS:
            val = str(r['features'].get(dimension) or 'unknown')
            if val == 'unknown':
                continue
            cell = groups[dimension + '\0' + val]
            cell[0] += r['target']
            cell[1] += 1
    cells = {}
    for key, (success, total) in groups.items():
        cells[key] = {
            'n': total, 'success': success, 'observed_rate': success / total,
            'interval95': wilson(success, total),
            'shrunk_rate': (success + cfg['prior_strength'] * global_rate) / (total + cfg['prior_strength']),
            'usable': total >= cfg['min_cell'],
        }
    summary |= {'n': n, 'success': s, 'global_rate': global_rate,
                'ready': n >= cfg['min_mature'], 'target': 'recorded_positive_engagement_within_horizon',
                'horizon_days': cfg['horizon_days'], 'lookback_days': cfg['lookback_days'],
                'interval95': wilson(s, n),
                'causal_claim': False, 'selection_propensities_available': False}
    inputs=[(row['submission_id'],row['target'],row['features']) for row in mature]
    model_id = digest(dump([cfg, summary, cells, inputs]))
    with transaction(conn):
        conn.execute('UPDATE feedback_models SET active=0')
        conn.execute('''INSERT INTO feedback_models(model_id,created_at,checked_at,config_json,summary_json,cells_json,active)
                        VALUES(?,?,?,?,?,?,1) ON CONFLICT(model_id) DO UPDATE SET checked_at=excluded.checked_at,active=1''',
                     (model_id, iso(now), iso(now), dump(cfg), dump(summary), dump(cells)))
    model = {'model_id': model_id, 'created_at': iso(now), 'config': cfg, 'summary': summary, 'cells': cells}
    refresh_priorities(conn, model, cfg)
    return model


def load_model(conn, *, now=None) -> dict | None:
    try:
        row = conn.execute('SELECT * FROM feedback_models WHERE active=1 ORDER BY created_at DESC LIMIT 1').fetchone()
    except postgres.OperationalError as exc:
        if 'no such table' in str(exc):
            return None
        raise
    if row is None:
        return None
    cfg = policy(conn)
    if utc(now) - utc(row['checked_at'] or row['created_at']) > timedelta(hours=cfg['max_model_age_hours']):
        return None
    return {'model_id': row['model_id'], 'created_at': row['created_at'],
            'config': obj(row['config_json']), 'summary': obj(row['summary_json']), 'cells': obj(row['cells_json'])}


def adjustment(features, model, cfg) -> tuple[float, list[dict]]:
    if not model or not model['summary']['ready']:
        return 0.0, []
    global_rate = model['summary']['global_rate']
    total = weight = 0.0
    reasons = []
    for dimension, w in RANK_WEIGHTS.items():
        val = str(features.get(dimension) or 'unknown')
        cell = model['cells'].get(dimension + '\0' + val)
        if not cell or not cell['usable']:
            continue
        difference = cell['shrunk_rate'] - global_rate
        total += w * difference
        weight += w
        reasons.append({'dimension': dimension, 'value': val, **cell})
    if not weight:
        return 0.0, reasons
    boost = cfg['max_boost'] * max(-1.0, min(1.0, total / weight / .25))
    return round(boost, 6), reasons


def refresh_priorities(conn, model, cfg, *, limit=5000):
    """Materialize outcome-only ranking in a bounded maintenance pass.

    Missing/stale projections contribute zero. They never relax source/fit or
    submission guards. No Python callback runs during PostgreSQL selection.
    """
    if not model['summary']['ready']:
        conn.execute("""INSERT INTO feedback_priority(job_url,model_id,input_revision,boost,computed_at)
            SELECT url,?,discovery_assessment_revision,0,? FROM jobs WHERE applied_at IS NULL
            ORDER BY fit_score DESC NULLS LAST,url LIMIT ?
            ON CONFLICT(job_url) DO UPDATE SET model_id=excluded.model_id,
            input_revision=excluded.input_revision,boost=excluded.boost,computed_at=excluded.computed_at""",
            (model['model_id'], iso(), limit))
        return
    jobs = conn.execute("SELECT * FROM jobs WHERE applied_at IS NULL "
                        "ORDER BY fit_score DESC NULLS LAST,url LIMIT ?", (limit,)).fetchall()
    values = []
    for row in jobs:
        job = dict(row)
        values.append((job['url'], model['model_id'], job.get('discovery_assessment_revision'),
                       adjustment(job_features(job, conn=conn), model, cfg)[0], iso()))
    conn.executemany("""INSERT INTO feedback_priority(job_url,model_id,input_revision,boost,computed_at)
        VALUES(?,?,?,?,?) ON CONFLICT(job_url) DO UPDATE SET model_id=excluded.model_id,
        input_revision=excluded.input_revision,boost=excluded.boost,computed_at=excluded.computed_at""", values)


def register_priority(conn) -> str | None:
    cfg, model = policy(conn), load_model(conn)
    if cfg['mode'] != 'active' or not model or not model['summary']['ready']:
        return None
    # IDs are internal SHA-256 hex, not employer/model-supplied SQL.
    import re
    model_id = model['model_id']
    if not re.fullmatch(r'[a-f0-9]{16,128}', model_id):
        raise ValueError('Invalid feedback model identity')
    return ("(COALESCE(j.fit_score,0)+COALESCE((SELECT fp.boost FROM feedback_priority fp "
            "WHERE fp.job_url=j.url AND fp.model_id='" + model_id + "' "
            "AND fp.input_revision IS NOT DISTINCT FROM j.discovery_assessment_revision),0))")


def decision(conn, job, *, attempt_id=None, stage='apply') -> dict:
    cfg = policy(conn)
    model = load_model(conn)
    features = job_features(job, conn=conn)
    predicted, reasons = adjustment(features, model, cfg)
    return {'mode': cfg['mode'], 'model_id': model['model_id'] if model else None,
            'base_score': job.get('fit_score'),
            'adjustment': predicted if cfg['mode'] == 'active' else 0.0,
            'shadow_adjustment': predicted, 'reasons': reasons, 'stage': stage,
            'eligibility_overridden': False, 'preferences_overridden': False,
            'propensity': None, 'policy_kind': 'deterministic_bounded_reranking',
            'attempt_id': attempt_id}


def log_decision(conn, job, attempt_id) -> dict:
    detail = dict(job.get("_feedback_choice") or decision(conn, job, attempt_id=attempt_id))
    detail["attempt_id"] = attempt_id
    conn.execute('INSERT INTO feedback_decisions\n        (decision_key,job_url,attempt_id,model_id,mode,base_score,adjustment,features_json,occurred_at)\n        VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
        (attempt_id, job['url'], attempt_id, detail['model_id'], detail['mode'],
         detail['base_score'], detail['adjustment'], dump(detail), iso()))
    return detail


def report(conn, *, now=None) -> dict:
    cfg = policy(conn)
    data, summary = dataset(conn, cfg=cfg, now=now)
    cohorts = defaultdict(lambda: {'submissions': 0, 'engagement': 0, 'assessment': 0,
                                  'interview': 0, 'technical_interview': 0, 'offer': 0, 'rejection': 0})
    for r in data:
        if not r['included']:
            continue
        f = r['features']
        key = (f.get('source','unknown'), f.get('market','unknown'), f.get('role_family','unknown'), f.get('document_variant','unknown'))
        c = cohorts[key]
        c['submissions'] += 1
        reached = {e['event_type'] for e in r['within_events']}
        c['engagement'] += r['target']
        c['assessment'] += int('assessment' in reached)
        c['interview'] += int(bool(reached & {'screen','interview','technical_interview','interview_completed'}))
        c['technical_interview'] += int('technical_interview' in reached)
        c['offer'] += int('offer' in reached)
        c['rejection'] += int('rejection' in reached)
    # Eventual observed funnel is separate from the fixed-horizon training cohort.
    eventual=defaultdict(int)
    feedback=[]
    for row in data:
        for milestone in set(row['milestones']):
            eventual[milestone]+=1
        for event in row['events']:
            for item in obj(event.get('payload_json')).get('feedback', []):
                if isinstance(item,dict) and item.get('quote'):
                    feedback.append({'event_id':event['id'],'submission_id':row['submission_id'],
                                     'theme':item.get('theme','unspecified'),'quote':item['quote'],
                                     'employer_statement_not_verified_fact':True})
    model = load_model(conn, now=now)
    return {'policy': cfg, 'summary': summary, 'eventual_observed_funnel':dict(eventual),
            'employer_feedback':feedback[-50:],
            'model': model, 'cohorts': [dict(zip(('source','market','role_family','document_variant'), k)) | v
                                      for k, v in sorted(cohorts.items())]}

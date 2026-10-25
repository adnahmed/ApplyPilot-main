"""Evidence tiers and bounded allocation from recorded company-source outcomes."""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from applypilot.targets import store as targets
from applypilot.tracking.feedback_store import utc


def tier(points):
    return 'watch' if points >= 70 else 'research' if points >= 50 else 'passive' if points >= 30 else 'ignore'


def target_research_allowed(conn, target):
    """Automatic watch polling follows its latest verified tier; owner intent wins."""
    if target.get('origin', 'operator') == 'operator':
        return True
    row = conn.execute("""SELECT score,detail_json FROM company_signals WHERE target_id=?
        ORDER BY observed_at DESC,id DESC LIMIT 1""", (target['id'],)).fetchone()
    if row is None or row['score'] < 70:
        return False
    detail = json.loads(row['detail_json'])
    verified = detail.get('verification') or {}
    return bool(verified and not detail.get('verification_error') and not any(
        detail.get('evidence', {}).get(key) or verified.get('evidence', {}).get(key)
        for key in ('inactive', 'employment_restricted')))


def source_metrics(conn):
    """Count observed yield, never turn job state or an inferred reply into an outcome.

    Multiple signals, targets and events give a source at most one credit per job
    at each stage. A source is observational attribution, not a causal claim.
    """
    rows = [dict(row) for row in conn.execute("""SELECT s.*,
        (SELECT COUNT(DISTINCT c.website) FROM company_signals c WHERE c.source_id=s.id) AS companies,
        (SELECT COUNT(DISTINCT c.website) FROM company_signals c
            WHERE c.source_id=s.id AND c.score>=50) AS relevant
        FROM company_sources s ORDER BY s.name,s.id""")]
    sources_by_job = defaultdict(set)
    jobs = defaultdict(set)
    for row in conn.execute("""SELECT DISTINCT c.source_id,l.job_url,j.discovery_role_status
        FROM company_signals c JOIN company_target_jobs l ON l.target_id=c.target_id
        JOIN jobs j ON j.url=l.job_url"""):
        sources_by_job[row['job_url']].add(row['source_id'])
        if row['discovery_role_status'] == 'relevant':
            jobs[row['source_id']].add(row['job_url'])
    ambiguous = set()
    for row in conn.execute("SELECT submission_id,candidates_json FROM feedback_messages WHERE state='review'"):
        if row['submission_id']:
            ambiguous.add(row['submission_id'])
        for candidate in json.loads(row['candidates_json'] or '[]'):
            if isinstance(candidate, dict) and candidate.get('submission_id'):
                ambiguous.add(candidate['submission_id'])
    submissions = {}
    applications, interviews, offers = (defaultdict(set) for _ in range(3))
    now = datetime.now(UTC)
    for row in conn.execute("""SELECT attempt_id AS submission_id,job_url,submitted_at
        FROM application_attempts WHERE status='submitted' AND NULLIF(submitted_at,'') IS NOT NULL
        UNION SELECT submission_id,job_url,submitted_at FROM feedback_submissions WHERE snapshot_origin='manual'"""):
        if row['job_url'] not in sources_by_job or utc(row['submitted_at']) > now:
            continue
        submissions[row['submission_id']] = dict(row)
        for source_id in sources_by_job[row['job_url']]:
            applications[source_id].add(row['job_url'])
    for row in conn.execute("""SELECT e.submission_identifier,e.job_url,e.event_type,e.occurred_at
        FROM application_outcomes e WHERE e.voided_at IS NULL AND e.trust IN ('manual','automatic-v1')
        AND e.event_type IN ('screen','interview','technical_interview','interview_completed','offer')
        AND (e.trust='manual' OR EXISTS (SELECT 1 FROM feedback_messages m
            WHERE m.message_key=e.message_key AND m.state='processed'
            AND m.submission_id=e.submission_identifier))"""):
        submission = submissions.get(row['submission_identifier'])
        if (not submission or row['submission_identifier'] in ambiguous
                or row['job_url'] != submission['job_url']
                or not utc(submission['submitted_at']) <= utc(row['occurred_at']) <= now):
            continue
        destination = offers if row['event_type'] == 'offer' else interviews
        for source_id in sources_by_job[row['job_url']]:
            destination[source_id].add(row['job_url'])
    for row in rows:
        source_id = row['id']
        row.update(jobs=len(jobs[source_id]), applications=len(applications[source_id]),
                   interviews=len(interviews[source_id]), offers=len(offers[source_id]))
        evidence = json.loads(row['evidence_json'])
        spend = evidence.get('resource_totals', {})
        cost = row['runs'] + spend.get('http', 0) / 10 + spend.get('model', 0) / 5
        reward = row['relevant'] + 3 * row['jobs'] + 8 * row['applications'] + 20 * row['interviews'] + 40 * row['offers']
        row['yield_score'] = round(reward / (cost + 4), 4)
        row['capability'] = evidence.get('capability', 'public_page')
        row['allocation_basis'] = 'Observed companies, relevant jobs, confirmed submissions and trusted interview/offer events'
    return rows


def select_sources(conn, settings):
    """Business ranking only; native Schedules decide when sources run."""
    return sorted((row for row in source_metrics(conn) if row['state'] in {'active', 'candidate'}),
                  key=lambda row: (-row['yield_score'], row['id']))


SEARCH_TEMPLATES = (
    ('portfolio_directory', '{market} venture capital portfolio companies'),
    ('accelerator', '{market} startup accelerator cohort companies {year}'),
    ('government_program', '{market} government startup incubator grant recipients {year}'),
    ('funding_news', '{market} startups seed funding announced {year}'),
    ('vc_job_board', '{market} venture capital portfolio engineering jobs'),
    ('company_registry', '{market} technology startup company directory'),
    ('accelerator', '{market} university entrepreneurship accelerator startups {year}'),
    ('portfolio_directory', '{market} venture fund portfolio {role}'),
)


def source_queries(context):
    markets = list(dict.fromkeys(str(market.location or ' '.join(market.countries) or market.label.value)
                                for market in context.intent.active_markets())) or ['global remote']
    role = next((query.query for query in context.intent.active_queries()), 'technology')[:100]
    year = datetime.now(UTC).year
    return [{'query': template.format(market=market, role=role, year=year)[:1000],
             'market': market, 'kind': kind}
            for kind, template in SEARCH_TEMPLATES for market in markets]

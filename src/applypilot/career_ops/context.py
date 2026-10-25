"""Reuse existing candidate facts, company documents and trusted outcome history."""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from applypilot import config

from . import store


def _source(text, kind, **metadata):
    return {'text': text, 'kind': kind, 'revision': hashlib.sha256(text.encode()).hexdigest(), **metadata}


def profile_pay(profile):
    """Reuse actual pay only; legacy salary expectations are not current pay."""
    from applypilot.compensation import preferences
    comp = preferences(profile.get('compensation') or {})
    periods = {'hourly': 'hour', 'daily': 'day', 'weekly': 'week', 'monthly': 'month', 'annual': 'year'}
    return {key: str(value) for key, value in {
        'current_amount': comp.get('current_pay_amount'),
        'current_currency': comp.get('current_pay_currency'),
        'current_period': periods.get(comp.get('current_pay_period')),
    }.items() if value is not None}


def salary_figures(data, profile=None):
    """Compare supplied amounts and saved actual pay in matching currency/period."""
    saved = profile_pay(profile or {})
    figures = {}
    for name in ('current', 'desired', 'advertised', 'offer'):
        use_profile = name == 'current' and not any(data.get('current_' + key, '').strip()
                                                    for key in ('amount', 'currency', 'period'))
        values = saved if use_profile else data
        raw = values.get(name + '_amount', '').strip()
        if not raw:
            if not use_profile and any(data.get(name + '_' + key, '').strip() for key in ('currency', 'period')):
                raise ValueError(f'{name.title()} compensation needs an amount, currency and period together')
            continue
        try:
            amount = Decimal(raw.replace(',', ''))
        except InvalidOperation as exc:
            raise ValueError(f'{name.title()} compensation must be a numeric amount') from exc
        currency = values.get(name + '_currency', '').strip().upper()
        period = values.get(name + '_period', '').strip().lower()
        if not amount.is_finite() or amount < 0 or (amount == 0 and name != 'current') or amount > Decimal(1000000000000):
            raise ValueError('Compensation must be finite and at most one trillion; only current pay can be zero')
        if not re.fullmatch(r'[A-Z]{3}', currency):
            raise ValueError('State the three-letter currency for every compensation amount')
        if period not in ('hour', 'day', 'week', 'month', 'year'):
            raise ValueError('State hour, day, week, month, or year for every compensation amount')
        figures[name] = {'amount': str(amount), 'currency': currency, 'period': period,
                         'source': 'profile' if use_profile else 'user'}
    gaps = []
    for left, right in (('desired', 'offer'), ('advertised', 'offer'), ('current', 'offer')):
        if left not in figures or right not in figures:
            continue
        a, b = figures[left], figures[right]
        comparable = (a['currency'], a['period']) == (b['currency'], b['period'])
        gaps.append({'baseline': left, 'comparison': right, 'comparable': comparable,
                     'difference': str(Decimal(b['amount']) - Decimal(a['amount'])) if comparable else None,
                     'currency': a['currency'] if comparable else None,
                     'period': a['period'] if comparable else None,
                     'reason': 'Same currency and period' if comparable else 'Currency or period differs; no conversion'})
    return {'figures': figures, 'gaps': gaps, 'conversion': 'none'}


def outcome_context(conn):
    from applypilot.tracking.feedback_learning import dataset, wilson
    from applypilot.tracking.feedback_store import POSITIVE
    data, summary = dataset(conn)
    cells = defaultdict(lambda: {'submissions': 0, 'advanced': 0, 'explicit_rejections': 0, 'excluded': 0})
    sources = {}
    for row in data:
        facts = row['features']
        host = (urlsplit(facts.get('target') or row['job_url']).hostname or '').lower()
        ats = next((name for name, domains in {
            'Greenhouse': ('greenhouse.io',), 'Lever': ('lever.co',), 'Ashby': ('ashbyhq.com',),
            'Workday': ('myworkdayjobs.com', 'myworkdaysite.com'),
            'SmartRecruiters': ('smartrecruiters.com',), 'Workable': ('workable.com',),
            'iCIMS': ('icims.com',), 'BambooHR': ('bamboohr.com',),
        }.items() if any(host == domain or host.endswith('.' + domain) for domain in domains)), 'Unknown')
        key = (ats, str(facts.get('channel') or 'unknown'))
        cell = cells[key]
        if not row['included']:
            cell['excluded'] += 1
        else:
            cell['submissions'] += 1
            cell['advanced'] += int(any(e['event_type'] in POSITIVE for e in row['within_events']))
            cell['explicit_rejections'] += int(any(e['event_type'] == 'rejection' for e in row['within_events']))
        for event in row['events']:
            if event['event_type'] != 'rejection':
                continue
            evidence = {key: event.get(key) for key in ('notes', 'event_type', 'occurred_at', 'trust', 'source')}
            evidence['submission_id'] = row['submission_id']
            evidence['job_url'] = row['job_url']
            sources['outcome:' + str(event['id'])] = _source(store.dump(evidence), 'explicit_rejection')
    cohorts = []
    for (ats, channel), cell in sorted(cells.items()):
        n = cell['submissions']
        cohorts.append({'ats': ats, 'channel': channel, **cell,
                        'recorded_advance_rate': round(cell['advanced'] / n, 4) if n else None,
                        'interval_95': wilson(cell['advanced'], n),
                        'small_sample': n < 8})
    return {'cohorts': cohorts, 'observation_coverage': summary,
            'interpretation': 'Recorded advances in the existing observation horizon; no causal ATS conclusion. '
                              'Young, ambiguous, and unobserved submissions are excluded from rates. '
                              'Silence is never counted as rejection.'}, sources


def build(conn, mode, data, *, invitation=None):
    from applypilot.career.documents import document_context
    from applypilot.impact.ledger import default_repository
    from applypilot.profile import scoped_work_authorization

    job = {}
    job_url = data.get('job_url', '')
    if job_url:
        row = conn.execute('SELECT * FROM jobs WHERE url=?', (job_url,)).fetchone()
        if row is None:
            raise ValueError('Select an existing job to use its saved description and application evidence')
        job = dict(row)
        from applypilot.discovery.repository import read_job_document
        for key in ('discovery_normalized_json', 'discovery_assessment_json'):
            job[key] = read_job_document(job, key, conn)
    elif data.get('job_description'):
        job = {'full_description': data['job_description'], 'company': data.get('company_name', ''),
               'url': '', 'title': 'Pasted role'}
    if mode in ('interview_brief', 'evaluate', 'interview_plan', 'interview_practice') and not job:
        raise ValueError('Select a saved job or paste its job description')
    profile = config.load_profile()
    resume = config.RESUME_PATH.read_text(encoding='utf-8-sig') if config.RESUME_PATH.exists() else ''
    records = default_repository().list_records()
    original = document_context(resume, profile, records, job)
    sources = {}
    for key, value in original['sources'].items():
        text = value.get('text') or store.dump(value.get('facts', {}))
        # Keep all attribution and numeric qualifiers alongside the reusable text.
        if value.get('outcomes') or value.get('metric_binding'):
            text += '\n' + store.dump({k: value[k] for k in ('outcomes', 'metric_binding') if k in value})
        sources[key] = _source(text, 'candidate', attribution=value.get('attribution', ''),
                               source_ref=value.get('source_ref', key))
    description = job.get('full_description') or job.get('description') or ''
    pasted = not description.strip() and bool(data.get('job_description'))
    if pasted:
        description = data['job_description']
    if job and not description.strip() and mode != 'interview_brief':
        raise ValueError('Enrich this job first; career workflows need its saved job description')
    if description:
        sources['job:description'] = _source(description, 'job_description' if job_url and not pasted else 'pasted_job_description',
                                             url=job_url, origin='saved_job' if job_url and not pasted else 'user_paste')
        facts = {key: job.get(key) for key in ('title', 'company', 'location', 'salary', 'salary_currency',
                                               'salary_period', 'posted_at', 'discovered_at', 'application_url')}
        sources['job:metadata'] = _source(store.dump(facts), 'job_metadata')
    work = scoped_work_authorization(profile, job) if job else {}
    sources['candidate:work_authorization'] = _source(store.dump(work), 'work_authorization')
    if invitation:
        sources['invitation:trigger'] = _source(store.dump(invitation),
            'role_practice_trigger' if invitation.get('practice_lane') else 'interview_invitation')
    if job_url:
        sources['interview:logistics'] = _source(store.dump({
            'next_action': job.get('next_action'), 'next_action_due': job.get('next_action_due'),
            'availability': profile.get('availability', {})}), 'interview_logistics')
        for row in conn.execute("""SELECT id,event_type,occurred_at,notes,payload_json FROM application_outcomes
            WHERE job_url=? AND voided_at IS NULL AND trust IN ('manual','automatic-v1')
            ORDER BY occurred_at,id""", (job_url,)):
            sources['outcome:' + str(row['id'])] = _source(store.dump(dict(row)), 'application_outcome')
        for row in conn.execute('SELECT email_id,subject,body_text,received_at,sender,sender_name FROM tracking_emails WHERE job_url=? ORDER BY received_at', (job_url,)):
            sources['correspondence:' + row['email_id']] = _source(
                (row['subject'] or '') + '\n' + (row['body_text'] or ''), 'correspondence',
                received_at=row['received_at'], sender=row['sender'], sender_name=row['sender_name'])
        for row in conn.execute("""SELECT m.message_key,m.excerpt,m.received_at FROM referral_messages m
            JOIN referral_job_links l ON l.case_id=m.case_id WHERE l.job_url=? ORDER BY m.received_at""", (job_url,)):
            sources['correspondence:' + row['message_key']] = _source(row['excerpt'], 'correspondence', received_at=row['received_at'])
    if job.get('company'):
        sites = {row['website'] for row in conn.execute(
            "SELECT website FROM company_targets WHERE lower(company)=lower(?) AND website<>'' UNION "
            "SELECT website FROM company_signals WHERE lower(company)=lower(?) AND website<>''",
            (job['company'], job['company']))}
        from applypilot.company_discovery.predictive_store import documents
        for site in sorted(sites):
            for doc in documents(conn, site):
                sources['company:' + doc['id']] = _source(doc['text'], 'company_research', url=doc['url'],
                    title=doc['title'], observed_at=doc['observed_at'], event_date=doc.get('event_date'))
    for name in ('instructions', 'offer_text', 'notes', 'answer'):
        if data.get(name):
            sources['user:' + name] = _source(data[name], 'user_account' if name != 'offer_text' else 'offer_document')
    comp = salary_figures(data, profile)
    sources['compensation:user'] = _source(store.dump(comp), 'compensation')
    history, outcome_sources = outcome_context(conn) if mode == 'patterns' else ({}, {})
    sources.update(outcome_sources)
    if history:
        sources['outcomes:cohorts'] = _source(store.dump(history), 'computed_cohorts')
    # No generated story is promoted into candidate facts. The original sources
    # are cited again whenever a previous draft is reused.
    drafts = [json.loads(row['story_json']) for row in conn.execute(
        'SELECT story_json FROM career_ops_stories ORDER BY created_at DESC LIMIT 30')]
    practice_progress = {}
    if mode in {'interview_plan', 'interview_practice'} and job_url:
        from .readiness import overview
        practice_progress = overview(conn, job_url=job_url)['progress']
    return {'mode': mode, 'job_url': job_url, 'sources': sources, 'work_authorization': work,
            'compensation': comp, 'history': history, 'story_drafts': drafts,
            'input_revisions': original['input_revisions'], 'request': data,
            'practice_progress': practice_progress,
            'as_of': datetime.now(UTC).isoformat()}

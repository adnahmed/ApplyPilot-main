"""Native job-swarm stages on ApplyPilot's existing company-discovery schedule.

The numerical research is independent of the model audit. Only real discovered
postings enter the job pipeline; memos and artifact plans remain review records.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json

from applypilot import config, operations as ops
from applypilot.execution import ExecutionStopped
from applypilot.integrations.crawl import CrawlService
from applypilot.integrations.search import SearchService
from applypilot.integrations.state import SourceState
from applypilot.integrations.types import SourceError
from applypilot.targets import store as targets
from . import predictive_store as store
from .store import identity


def candidate_profile(context):
    """Use application evidence, excluding login secrets and unrelated personal fields."""
    profile = config.load_profile()
    facts = {key: profile[key] for key in ('experience', 'skills_boundary', 'resume_facts') if profile.get(key)}
    if isinstance(facts.get('experience'), dict):
        facts['experience'] = {key: value for key, value in facts['experience'].items() if key != 'target_role'}
    resume = config.RESUME_PATH.read_text(encoding='utf-8-sig') if config.RESUME_PATH.exists() else ''
    if not resume.strip() and not facts:
        raise ValueError('Predictive research needs resume or candidate experience evidence')
    text = resume + '\n\nCandidate facts:\n' + json.dumps(facts, ensure_ascii=False)
    facets = []
    for value in profile.get('skills_boundary', {}).values():
        if isinstance(value, list):
            facets.extend(item for item in value if isinstance(item, str) and item.strip())
    facets.extend(paragraph.strip() for paragraph in resume.split('\n\n') if len(paragraph.strip()) >= 80)
    fingerprint = identity(text, context.intent.fingerprint(), context.applicant.fingerprint())
    return text, list(dict.fromkeys(facets))[:40], fingerprint


def _signal_documents(conn, company):
    documents = []
    for row in conn.execute("""SELECT website,source_url,event_date,observed_at,detail_json
        FROM company_signals WHERE company=? ORDER BY observed_at DESC LIMIT 40""", (company['company'],)):
        if store.website_key(row['website']) != company['website']:
            continue
        value = json.loads(row['detail_json'])
        documents.append({'url': row['source_url'], 'title': value.get('signal_type', 'Company signal'),
            'text': value.get('quote') or '', 'event_date': row['event_date'],
            'observed_at': row['observed_at'], 'authored': False})
    return documents


def _careers(conn, company):
    company['jobseek_boards'] = []
    for row in conn.execute("SELECT id,website,config_json FROM company_targets WHERE state='active' AND website<>''"):
        if store.website_key(row['website']) == company['website']:
            company['jobseek_boards'] = targets.boards(conn, row['id'])
            return json.loads(row['config_json']).get('careers_url') or ''
    for row in conn.execute('SELECT website,detail_json FROM company_signals WHERE company=? ORDER BY observed_at DESC',
                            (company['company'],)):
        if store.website_key(row['website']) == company['website']:
            value = json.loads(row['detail_json'])
            if value.get('verification') and not value.get('verification_error'):
                company['jobseek_boards'] = value.get('jobseek_boards', [])
                return value.get('careers_url') or ''
    return ''

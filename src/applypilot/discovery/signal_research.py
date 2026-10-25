"""Resolve retained hiring signals through the existing company research worker.

A post is provenance, never a grant to apply or contact its author. Official
vacancies receive independent source identities and the usual admission checks.
"""
from __future__ import annotations

from dataclasses import replace
import json
import re
from urllib.parse import urlsplit

from applypilot import operations as ops
from applypilot.integrations.types import SourceError

PROVIDERS = frozenset({'linkedin_posts', 'twitter', 'hackernews', 'web_search'})
_NON_EMPLOYER_HOSTS = frozenset({'linkedin.com', 'x.com', 'twitter.com', 'facebook.com',
    'google.com', 'wikipedia.org', 'crunchbase.com', 'indeed.com', 'glassdoor.com',
    'jobs.lever.co', 'jobs.eu.lever.co', 'jobs.ashbyhq.com', 'boards.greenhouse.io', 'job-boards.greenhouse.io'})


def stored_signal(conn, job):
    if job.get('site') not in PROVIDERS:
        return None
    from .payloads import resolve_json
    row = conn.execute("""SELECT o.raw_json FROM discovery_postings p
        JOIN discovery_observations o ON o.identity_key=p.identity_key
        WHERE p.job_url=? AND o.inference IS NULL
        ORDER BY o.source_revision DESC,o.last_seen_at DESC LIMIT 1""", (job['url'],)).fetchone()
    if row is None:
        return None
    record = json.loads(resolve_json(conn, row[0]))
    if record.get('record_kind') != 'role_signal' or not record.get('company'):
        return None
    return record


def _stage_author(conn, record, provider):
    from applypilot.referrals.network import stage_people
    post = record.get('raw_record', {}).get('signal', {})
    author = post.get('author')
    if not isinstance(author, dict) or not author.get('name') or not author.get('profile_url'):
        return []
    text = post.get('text', '')
    quotes = [record[key] for key in ('invitation', 'contact')
              if isinstance(record.get(key), str) and record[key] in text]
    result = stage_people(conn, [{'name': author['name'], 'profile_url': author['profile_url'],
        'source_url': post.get('url'), 'quote': '\n'.join(quotes) or text[:2000]}],
        provider=provider, company_hint=record['company'], operation='hiring_post')
    return result['lead_ids']


def _url(value):
    from applypilot.integrations.net import public_url
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        return public_url(value if '://' in value else 'https://' + value, resolve=False)
    except SourceError:
        return None


def _host(url):
    return (urlsplit(url).hostname or '').lower().removeprefix('www.')


def _same_site(url, website):
    host, root = _host(url), _host(website)
    return bool(root) and (host == root or host.endswith('.' + root))


def _official_routes(report, company):
    """Require observed website coordinates before following employer careers."""
    sources = {source['id']: source for source in report.get('sources', [])}
    websites = []
    for fact in report.get('facts', []):
        if fact.get('field') != 'website':
            continue
        source = sources.get(fact.get('source_id'))
        website = _url(fact.get('value'))
        if not source or not website or company.casefold() not in source.get('text', '').casefold():
            continue
        if any(_host(website) == host or _host(website).endswith('.' + host) for host in _NON_EMPLOYER_HOSTS):
            continue
        observed = [source.get('url'), *source.get('links', [])]
        if any(isinstance(url, str) and _same_site(url, website) for url in observed):
            websites.append(website)
    websites = list(dict.fromkeys(websites))
    if not websites:
        return [], []
    official_sources = [source for source in sources.values()
                        if source.get('kind') == 'page' and any(
                            _same_site(source['url'], website) for website in websites)]
    linked = {url for source in official_sources for url in (source['url'], *source.get('links', []))}
    from .company_targets import _career_page
    from .careers import detect_provider
    native = {'greenhouse', 'lever', 'ashby'}
    careers = [url for url in linked if _career_page(url) or detect_provider(url) in native]
    careers.sort(key=lambda url: (0 if detect_provider(url) in native else
                                 1 if any(_same_site(url, website) for website in websites) else 2, url))
    return websites, careers


def _company(value):
    words = re.findall(r'[\w]+', str(value or '').casefold())
    while words and words[-1] in {'inc', 'incorporated', 'llc', 'ltd', 'limited', 'corp', 'corporation', 'co', 'company'}:
        words.pop()
    return ' '.join(words)


def _related_role(title, expected):
    plural = {'engineers', 'developers', 'designers', 'managers', 'analysts', 'scientists',
              'architects', 'researchers', 'specialists', 'consultants', 'interns'}
    def tokens(value):
        return {word[:-1] if word in plural else word for word in re.findall(r'[\w]+', str(value or '').casefold())}
    required, actual = tokens(expected), tokens(title)
    return bool(required) and required <= actual


@ops.observe('company_research', 'signal_vacancies', job_arg='job', stage='company_research')
def resolve_signal(job, report, researcher, *, context=None, traversal=None):
    """Save author leads and publish corroborating vacancies; never send messages."""
    from applypilot import postgres
    from applypilot.config import DB_PATH
    from .context import build_context
    from .repository import _context, _identity, _specific_listing, publish_records, transaction
    from .normalization import normalize_record
    from .company_targets import vacancy_step

    if job.get('site') not in PROVIDERS:
        return None
    conn = postgres.connect(DB_PATH)
    try:
        record = stored_signal(conn, job)
        if record is None:
            return None
        leads = _stage_author(conn, record, job['site'])
    finally:
        conn.close()
    result = {'source_job': job['url'], 'source_signal_id': record['source_signal_id'],
              'source_revision': record['source_revision'], 'lead_ids': leads,
              'resolved_postings': [], 'status': 'deferred', 'sent': 0}
    websites, careers = _official_routes(report, record['company'])
    if not websites:
        result['reason_code'] = 'employer_website_unresolved'
        return result
    context = context or build_context()
    if _context(context)[0] != job.get('discovery_context_key'):
        raise SourceError('Signal research context changed; retry the current revision', 'schema')
    # Connector calls no longer spend a separate research/model quota.
    # Provider request authorization remains at the discovery transport boundary.
    reader_context = context
    target = {'company': record['company'], 'website': websites[0], 'job_url': None}
    traversal = traversal or {'pending': [{'url': url, 'depth': 0} for url in careers or websites], 'visited': []}
    rows, observations, continuation = vacancy_step(traversal, target, reader_context,
                                                   researcher.crawler, publication_provider='ats_registry')
    capped = bool(continuation['pending'])
    result['traversal'] = continuation if capped else None
    result['coverage'] = 'partial' if capped else 'observed_pages'
    result['observations'] = observations
    origin = {'provider': job['site'], 'signal_id': record['source_signal_id'],
              'revision': record['source_revision'], 'url': record.get('url'), 'lead_ids': leads}
    conn = postgres.connect(DB_PATH)
    try:
        with transaction(conn):
            current = conn.execute("""SELECT discovery_content_hash,discovery_source_revision,
                discovery_context_key FROM jobs WHERE url=?""", (job['url'],)).fetchone()
            expected = (job.get('discovery_content_hash'), job.get('discovery_source_revision'),
                        job.get('discovery_context_key'))
            if current is None or tuple(current) != expected:
                raise SourceError('Signal changed while researching vacancies', 'schema')
            for raw in rows:
                row = dict(raw)
                if not _related_role(row.get('title'), record.get('title')):
                    continue
                if _company(row.get('company')) != _company(record['company']):
                    continue
                native = row.get('source_identity') or {}
                provider = native.get('provider', 'ats_registry')
                tenant = native.get('tenant') or _host(websites[0])
                region = (native.get('api_region') or 'global') if provider not in {'greenhouse', 'ashby'} else None
                if native.get('posting_id') is not None:
                    row['source_posting_id'] = native['posting_id']
                if not row.get('source_posting_id') and not _specific_listing(row.get('url')):
                    continue
                row['raw_record'] = {**row.get('raw_record', {}), 'hiring_signal_origin': origin}
                candidate = normalize_record(row, provider, tenant=tenant, api_region=region)
                identity = _identity(candidate, provider, tenant, region)[0]
                publish_records([row], provider, context=context, conn=conn, tenant=tenant,
                                api_region=region, origins=('signal-research:' + job['url'],))
                saved = conn.execute('SELECT job_url FROM discovery_postings WHERE identity_key=?', (identity,)).fetchone()
                result['resolved_postings'].append({'job_url': saved[0], 'title': row.get('title'),
                    'application_url': row.get('application_url'), 'provider': provider,
                    'association': 'related_observed_vacancy'})
    finally:
        conn.close()
    if result['resolved_postings']:
        result['status'] = 'resolved'
    else:
        result['reason_code'] = 'no_corroborating_vacancy_observed'
    ops.record(component_name='company_research', operation='signal_resolution', stage='company_research',
               outcome='success' if result['status'] == 'resolved' else 'deferred',
               message='Hiring signal vacancy research completed',
               details={'source': job['site'], 'reason_code': result.get('reason_code', 'vacancies_resolved'),
                        'resolved_postings': len(result['resolved_postings']), 'contact_leads': len(leads), 'sent': 0})
    return result

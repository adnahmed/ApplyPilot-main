"""Structured company feeds, with observed identities and explicit access failures.

Contracts: career-ops/company-funded.mjs, Dealroom /for-agents and Premium API,
Crunchbase v4 search/entity documentation, and SEC EDGAR current-filings Atom.
Credentials stay in environment variables and are never included in evidence.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from xml.etree import ElementTree as ET

from bs4 import BeautifulSoup
import httpx

from applypilot.integrations.net import check_status, fetch, public_url
from applypilot.integrations.types import Document, SourceError, bounded_int

DEALROOM_PUBLIC = 'https://dealroom.co/api/'
DEALROOM_PREMIUM = 'https://api.dealroom.co/api/v1/companies'
CRUNCHBASE = 'https://api.crunchbase.com/v4/data/searches/organizations'
SEC = 'https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=D&owner=include&count=100&output=atom'
RSS = (
    'https://techcrunch.com/category/startups/feed/',
    'https://www.prnewswire.com/rss/venture-capital-list.rss',
    'https://www.theguardian.com/technology/rss',
)


def capabilities(root=None):
    from applypilot import config
    from applypilot.discovery.jobseek import runtime_status
    from applypilot.onboarding.resolution import file_values, project_root
    saved = {**file_values(project_root() / '.env'), **file_values((root or config.APP_DIR) / '.env')}
    loaded = getattr(config, '_LOADED_ENV_VALUES', {})

    def present(key):
        value = os.getenv(key)
        return bool(saved.get(key) if key in loaded and value == loaded[key] else value or saved.get(key))
    runtime = runtime_status()
    return [
        {'name': 'Jobseek crawler', 'status': runtime['status'],
         'detail': (f"{runtime.get('monitor_count', 0)} monitor families and {runtime.get('scraper_count', 0)} scrapers available."
                    if runtime['installed'] else runtime.get('error') or 'Install the isolated Jobseek runtime.')},
        {'name': 'Public RSS / Atom', 'status': 'available', 'detail': 'Feed entries and article links; publication dates stay distinct from funding dates.'},
        {'name': 'Dealroom public', 'status': 'available', 'detail': 'Documented live signals and sampled market maps; coverage limits retained.'},
        {'name': 'SEC Form D', 'status': 'configured' if present('APPLYPILOT_SEC_USER_AGENT') else 'needs_configuration',
         'detail': 'Set APPLYPILOT_SEC_USER_AGENT to your application name and contact email.'},
        {'name': 'Dealroom Premium v1', 'status': 'configured' if present('DEALROOM_API_KEY') else 'needs_credentials',
         'detail': 'Requires your licensed DEALROOM_API_KEY. Add the Premium companies endpoint as a source.'},
        {'name': 'Crunchbase v4', 'status': 'configured' if present('CRUNCHBASE_API_KEY') else 'needs_credentials',
         'detail': 'Requires your licensed CRUNCHBASE_API_KEY. Add the organizations endpoint as a source.'},
        {'name': 'MAGNiTT', 'status': 'public_pages',
         'detail': 'Public news uses normal company discovery. Licensed JSON feeds can use an account-provided feed contract.'},
    ]


def _request(url, before_request, *, headers=None, body=None):
    """Credentialed requests never follow redirects or expose provider response text."""
    url = public_url(url)
    if before_request:
        before_request()
    try:
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            with client.stream('POST' if body is not None else 'GET', url,
                               headers={'Accept': 'application/json,application/atom+xml', **(headers or {})},
                               json=body) as response:
                check_status(response.status_code, response.headers)
                if 300 <= response.status_code < 400:
                    raise SourceError('Feed redirects require the final documented endpoint', 'schema')
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 5_000_000:
                        raise SourceError('Company feed exceeds 5 MB', 'schema')
                return bytes(payload)
    except httpx.HTTPError as exc:
        raise SourceError(f'Company feed transport failed ({type(exc).__name__})', 'network') from exc


def _json(url, before_request, **kwargs):
    raw = _request(url, before_request, **kwargs) if kwargs else fetch(
        url, max_bytes=5_000_000, before_request=before_request)[1]
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise SourceError('Company feed did not return valid JSON', 'schema') from exc
    if not isinstance(value, dict):
        raise SourceError('Company feed JSON must be an object', 'schema')
    return value


def _links(value):
    if isinstance(value, str):
        if value.startswith(('https://', 'http://')):
            try:
                yield public_url(value, resolve=False)
            except SourceError:
                pass
    elif isinstance(value, dict):
        for item in value.values():
            yield from _links(item)
    elif isinstance(value, list):
        for item in value:
            yield from _links(item)


def _document(source, rows, kind, *, url=None, notices=()):
    if any(not isinstance(row, dict) for row in rows):
        raise SourceError('Company feed records must be objects', 'schema')
    text = (f"Recurring structured company feed: {source['name']}\n"
            'Records are company evidence, not vacancies. Funding totals, intentions, filings and publication dates '
            'must not be represented as newly completed funding rounds. Preserve unknown event dates.\n')
    text += '\n'.join(notices) + '\n'
    for row in rows:
        text += '\n' + '\n'.join(f'{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}'
                                   for key, value in row.items()) + '\n'
    return Document(url=url or source['url'], title=source['name'], markdown=text,
        links=tuple(dict.fromkeys(link for row in rows for link in _links(row))),
        provider='company_feed:' + kind,
        structured_data=(*rows, {'_feed_metadata': {'kind': kind, 'notices': list(notices),
                                                  'record_count': len(rows)}}))


def _offset(cursor):
    if cursor is None:
        return 0
    if type(cursor) is not int or cursor < 0:
        raise SourceError('Invalid company feed checkpoint', 'schema')
    return cursor


def _slice(rows, cursor, limit):
    offset = _offset(cursor)
    if offset >= len(rows):
        offset = 0
    selected = rows[offset:offset + limit]
    return selected, offset + len(selected) if offset + len(selected) < len(rows) else None


def _xml(source, before_request, cursor, limit):
    is_sec = urlsplit(source['url']).hostname == 'www.sec.gov'
    if is_sec:
        user_agent = os.getenv('APPLYPILOT_SEC_USER_AGENT', '').strip()
        if not user_agent or '@' not in user_agent:
            raise SourceError('SEC requires APPLYPILOT_SEC_USER_AGENT with application name and contact email', 'configuration')
        raw = _request(source['url'], before_request, headers={'User-Agent': user_agent})
    else:
        _, raw, _ = fetch(source['url'], max_bytes=5_000_000, before_request=before_request)
    if re.search(br'<!DOCTYPE|<!ENTITY', raw, re.I):
        raise SourceError('Feed contains unsupported XML declarations', 'schema')
    try:
        tree = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SourceError('Source did not return RSS or Atom', 'schema') from exc
    local = lambda tag: tag.rsplit('}', 1)[-1]
    if local(tree.tag) not in {'rss', 'feed', 'RDF'}:
        raise SourceError('Source is not an RSS or Atom feed', 'schema')
    rows = []
    for entry in tree.iter():
        if local(entry.tag) not in {'item', 'entry'}:
            continue
        row, links = {}, []
        for node in entry:
            key = local(node.tag)
            value = ''.join(node.itertext()).strip()
            if key == 'link':
                link = node.get('href') or value
                if link:
                    links.extend(_links(urljoin(source['url'], link)))
            elif key in {'title', 'summary', 'description', 'content', 'encoded'}:
                soup = BeautifulSoup(value, 'html.parser')
                row['title' if key == 'title' else 'summary'] = soup.get_text(' ', strip=True)
                links.extend(link for anchor in soup.select('a[href]')
                             for link in _links(urljoin(source['url'], anchor['href'])))
            elif key in {'pubDate', 'published', 'updated'} and 'publication_date' not in row:
                try:
                    parsed = parsedate_to_datetime(value) if key == 'pubDate' else datetime.fromisoformat(value.replace('Z', '+00:00'))
                    row['publication_date'] = parsed.isoformat()
                except (ValueError, TypeError):
                    row['publication_date_text'] = value
        row['links'] = list(dict.fromkeys(links))
        if is_sec:
            row['signal_type'] = 'fundraising'
            row['company_name'] = re.sub(r'^D(?:/A)?\s*-\s*', '', row.get('title', ''))
            row['company_name'] = re.sub(r'\s*\(\d{6,}\).*$', '', row['company_name']).strip()
        if row.get('title') and links:
            rows.append(row)
    selected, next_cursor = _slice(rows, cursor, min(limit, 2))
    return _document(source, selected, 'sec_form_d' if is_sec else 'rss_atom', notices=(
        'SEC Form D is a fundraising filing, not evidence that a round closed.' if is_sec else
        'Feed publication dates are not necessarily the dates of company events.',)), next_cursor


def _dealroom(source, before_request, cursor, limit):
    path = urlsplit(source['url']).path
    if path == '/api/marketmaps':
        params = parse_qs(urlsplit(source['url']).query)
        index = _json(DEALROOM_PUBLIC + 'marketmaps?' + urlencode({'q': params.get('q', [''])[0], 'limit': 200}), before_request)
        maps = index.get('results')
        if not isinstance(maps, list) or any(not isinstance(row, dict) for row in maps):
            raise SourceError('Dealroom market-map response changed', 'schema')
        cursor = cursor or {'map': 0, 'offset': 0}
        if not isinstance(cursor, dict) or set(cursor) != {'map', 'offset'}:
            raise SourceError('Invalid Dealroom map checkpoint', 'schema')
        map_index, offset = _offset(cursor['map']), _offset(cursor['offset'])
        if map_index >= len(maps):
            map_index, offset = 0, 0
        if not maps:
            return _document(source, [], 'dealroom_marketmap'), None
        url = maps[map_index].get('companiesUrl')
        if not isinstance(url, str) or not url.startswith(DEALROOM_PUBLIC + 'marketmap?'):
            raise SourceError('Dealroom map has no observed company endpoint', 'schema')
        body = _json(url, before_request)
        rows = body.get('companies')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SourceError('Dealroom company map response changed', 'schema')
        selected, remaining = _slice(rows, offset, limit)
        next_cursor = ({'map': map_index, 'offset': remaining} if remaining is not None else
                       {'map': map_index + 1, 'offset': 0} if map_index + 1 < len(maps) else None)
        return _document(source, selected, 'dealroom_marketmap', url=url,
            notices=(f"Public sample: returned {body.get('returned', len(rows))} of {body.get('total_companies', 'unknown')} companies; capped={body.get('capped', 'unknown')}.",)), next_cursor
    body = _json(source['url'], before_request)
    rows = body.get('signals' if path == '/api/live-signals' else 'companies')
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise SourceError('Dealroom public response changed', 'schema')
    selected, next_cursor = _slice(rows, cursor, min(limit, 2) if path == '/api/live-signals' else limit)
    selected = [dict(row) for row in selected]
    for row in selected:
        if row.get('type') == 'round' and isinstance(row.get('title'), str):
            row['company_name'] = re.split(r'\s+(?:raised|raises|secured|closes)\s+', row['title'], maxsplit=1)[0]
    return _document(source, selected, 'dealroom_public', notices=('Public sample; partial dates stay unknown.',)), next_cursor


def _licensed(source, before_request, cursor, limit):
    if source['url'] == DEALROOM_PREMIUM:
        key = os.getenv('DEALROOM_API_KEY', '')
        if not key:
            raise SourceError('Dealroom Premium requires DEALROOM_API_KEY', 'auth')
        # Premium v1 declares pagination in query parameters, not the JSON body.
        request_url = source['url'] + '?' + urlencode({'limit': limit, 'offset': _offset(cursor)})
        body = _json(request_url, before_request,
            headers={'Authorization': 'Basic ' + base64.b64encode((key + ':').encode()).decode()},
            body={})
        rows = body.get('items')
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SourceError('Dealroom Premium response does not match its v1 items contract', 'schema')
        if len(rows) > limit:
            raise SourceError('Dealroom Premium ignored the requested page limit', 'schema')
        return _document(source, rows, 'dealroom_premium'), _offset(cursor) + len(rows) if len(rows) == limit else None
    key = os.getenv('CRUNCHBASE_API_KEY', '')
    if not key:
        raise SourceError('Crunchbase requires CRUNCHBASE_API_KEY and licensed search access', 'auth')
    if cursor is not None and (not isinstance(cursor, str) or not re.fullmatch(r'[a-fA-F0-9-]{36}', cursor)):
        raise SourceError('Invalid Crunchbase pagination UUID', 'schema')
    payload = {'field_ids': ['identifier', 'short_description', 'website', 'last_funding_at',
                             'last_funding_total', 'last_funding_type', 'location_identifiers'],
        'query': [{'type': 'predicate', 'field_id': 'last_funding_at', 'operator_id': 'gte',
                   'values': [(datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()]}],
        'order': [{'field_id': 'last_funding_at', 'sort': 'desc'}], 'limit': limit}
    if cursor:
        payload['after_id'] = cursor
    body = _json(source['url'], before_request, headers={'X-cb-user-key': key}, body=payload)
    entities = body.get('entities')
    if not isinstance(entities, list) or any(not isinstance(row, dict)
                                            or not isinstance(row.get('properties'), dict) for row in entities):
        raise SourceError('Crunchbase search response changed', 'schema')
    if len(entities) > limit:
        raise SourceError('Crunchbase ignored the requested page limit', 'schema')
    rows = [row['properties'] for row in entities]
    next_cursor = entities[-1].get('uuid') if len(entities) == limit else None
    if len(entities) == limit and (not isinstance(next_cursor, str)
                                  or not re.fullmatch(r'[a-fA-F0-9-]{36}', next_cursor)):
        raise SourceError('Crunchbase omitted a valid pagination UUID', 'schema')
    if next_cursor is not None and next_cursor == cursor:
        raise SourceError('Crunchbase returned the same pagination UUID', 'schema')
    return _document(source, rows, 'crunchbase'), next_cursor


def load(source, *, before_request, limit=8):
    bounded_int(limit, 'feed company limit', 1, 25)
    from .feed_contracts import load as load_contract
    contract = load_contract(source, before_request=before_request, limit=limit)
    if contract is not None:
        return contract
    evidence = json.loads(source.get('evidence_json') or '{}')
    cursor = evidence.get('feed_cursor')
    url = source['url']
    parsed = urlsplit(url)
    if url in {DEALROOM_PREMIUM, CRUNCHBASE, 'https://api.crunchbase.com/api/v4/searches/organizations'}:
        return _licensed(source, before_request, cursor, limit)
    if parsed.hostname == 'dealroom.co' and parsed.path in {'/api/marketmaps', '/api/marketmap', '/api/live-signals'}:
        return _dealroom(source, before_request, cursor, limit)
    if (parsed.hostname == 'www.sec.gov' and parsed.path == '/cgi-bin/browse-edgar'
            and parse_qs(parsed.query).get('output') == ['atom']):
        return _xml(source, before_request, cursor, limit)
    if url in RSS or re.search(r'/(?:feed|rss|atom)(?:/|\.|$)|\.(?:rss|atom|xml)$', parsed.path, re.I):
        return _xml(source, before_request, cursor, limit)
    return None

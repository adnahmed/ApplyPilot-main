"""One referral source observation; Temporal owns ordering and retries."""
from __future__ import annotations

import re

from . import store as s
from .identity import profile_key
from .network import normalize_people, stage_people, list_leads
from .planner import open_case

PROVIDERS = {'web', 'linkedin_rs', 'linkedin_mcp', 'twikit', 'xactions', 'x_use'}


def github_person(username):
    import httpx
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', username):
        raise ValueError('Invalid GitHub handle')
    url = 'https://api.github.com/users/' + username
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        response = client.get(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'ApplyPilot-referral-observation'})
        response.raise_for_status()
        value = response.json()
    return {'source_url': url, 'observed_at': s.iso(), 'unverified_public_profile':
            {key: value.get(key) for key in ('login', 'name', 'company', 'blog', 'html_url', 'email', 'bio')},
            'instructions': 'Verify current employer and professional contact context before import. Public email is not consent. Do not mine commit emails or infer a warm relationship.'}


def discover(conn, case_id, *, provider='web', limit=10, authorized=False, inputs=None, operation=None, run_id=None, search=None, social=None):
    """Known relevant contacts -> existing leads -> one bounded source operation.

    linkedin_rs checks a single page of connections before people search. Use
    start=40,80,... or import Connections.csv for complete network coverage.
    No-results is explicitly partial, never proof no referrer exists.
    """
    if provider not in PROVIDERS or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('select a free provider and a limit from 1 to 20')
    if s.policy(conn)['mode'] == 'off':
        raise ValueError('referrals disabled')
    if provider != 'web' and authorized is not True:
        raise ValueError('explicit authorization for your own session is required')
    case = s.require_case(conn, case_id)
    if case.get('case_type') == 'company':
        from applypilot.targets.outreach import open_company_case
        case = open_company_case(conn, case['target_id'])
    else:
        case = open_case(conn, case['job_url'])
    if s.application_phase(conn, case_id) == 'in_flight':
        return {'status': 'in_flight', 'sent': 0}
    plan = s.obj(case['plan_json'])
    if plan.get('candidates') and plan['candidates'][0].get('hold_eligible'):
        return {'status': 'verified_contact_available', 'candidates': plan['candidates'], 'remote_calls': 0, 'sent': 0}
    company = s.case_subject(conn, case)['company']
    leads = list_leads(conn, company=company, limit=limit)
    if leads:
        return {'status': 'review_existing_leads', 'lead_ids': [r['id'] for r in leads], 'remote_calls': 0, 'sent': 0}
    inputs = dict(inputs or {})
    function = plan.get('job_team', {}).get('name') or plan.get('job_function', {}).get('name') or case['title']
    # Quotes/newlines in a company name must not inject search operators.
    def clean(value):
        return re.sub(r'["\r\n]', ' ', str(value))[:200]
    query = f'"{clean(company)}" "{clean(function)}"'
    allowed = {'connections', 'people'} if provider == 'linkedin_rs' else {'people' if provider in {'web', 'linkedin_mcp'} else 'posts'}
    if operation not in allowed or not run_id:
        raise ValueError('Choose one provider operation with a stable observation identity')
    prior = conn.execute('SELECT result_json FROM referral_search_runs WHERE id=? AND finished_at IS NOT NULL',
                         (run_id,)).fetchone()
    if prior:
        return s.obj(prior['result_json'])
    if provider == 'web':
        if search is None:
            from applypilot.integrations.search import SearchService
            search = SearchService()
        hits = search.search('site:linkedin.com/in/ ' + query, limit=limit)
        rows = []
        for hit in hits:
            item = hit.to_dict() if hasattr(hit, 'to_dict') else hit
            try:
                if not (profile_key(item.get('url')) or '').startswith('linkedin:'):
                    continue
            except ValueError:
                continue
            # Search titles are unverified labels, not asserted legal names.
            rows.append({'name': str(item.get('title') or '')[:250], 'profile_url': item['url'],
                         'quote': item.get('snippet') or item.get('body') or '', 'source_url': item['url']})
    else:
        if social is None:
            from applypilot.integrations.social import SocialService
            social = SocialService(provider, inputs=inputs)
        raw = social.read(operation, query=None if operation == 'connections' else query + (' hiring' if operation == 'posts' else ''),
                          limit=limit, authorized=authorized)
        if operation == 'posts':
            from applypilot.integrations.social import normalized_posts
            posts = normalized_posts('twitter', raw, limit=limit)
            rows = [dict(p['author'], quote=p['text'], source_url=p['url']) for p in posts if p.get('author')]
            if posts and not rows:
                raise ValueError('posts have no explicit author metadata; cannot associate a referrer')
        elif operation == 'connections':
            people = normalize_people(raw, provider=provider, operation=operation, limit=limit)
            # Upstream --json bypasses the CLI keyword filter. Filter locally,
            # but retain as hints: headline substring is NOT verified employment.
            rows = [dict(name=p['name'], profile_url=p['profile_url'], role_function=p['role_hint'])
                    for p in people['leads'] if s.company_key(company) in
                    s.company_key(p['role_hint'] + ' ' + p['company_hint'])]
            if people['rejected'] and not people['leads']:
                raise ValueError('connection payload has no recognized people rows')
        else:
            rows = raw
    staged = stage_people(conn, rows, provider=provider, company_hint=company, operation=operation, limit=limit)
    malformed = bool(staged['rejected_rows']) and not staged['lead_ids']
    result = {**staged, 'status': 'source_error' if malformed else 'review_required' if staged['lead_ids'] else 'no_results',
              'coverage': 'not_established' if malformed else 'bounded_partial', 'operation': operation, 'remote_calls': 1}
    if malformed:
        raise ValueError('Provider returned no usable person identities')
    result = {'case_id': case_id, 'provider': provider, 'results': [result],
              'status': result['status'], 'review_required': True, 'contacts_imported': 0, 'sent': 0}
    with s.transaction(conn):
        conn.execute('INSERT INTO referral_search_runs(id,case_id,provider,started_at,finished_at,status,result_json) '
                     'VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                     (run_id, case_id, provider + ':' + operation, s.iso(), s.iso(), result['status'], s.dump(result)))
    return result

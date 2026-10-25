"""Read-only probes using the same selected source transports as discovery."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import time

import httpx

from .environment import child_env
from .storage import read_json


def verify_sources(root, progress=lambda **kw: None, *, platform=None):
    from applypilot.integrations.local_config import session_file, freehire_credentials
    from applypilot.integrations.worker import call_worker
    from applypilot.integrations.types import SourceError
    from applypilot.operations.runtime import exception_details, sanitize
    from applypilot.integrations.recovery import source_check_view

    env = child_env(root)
    profile = read_json(root / 'profile.json', {}).get('personal') or {}
    accounts = read_json(root / 'unattended.json', {}).get('accounts') or {}

    def sdk(provider, operation, arguments):
        return call_worker(provider, operation, arguments, timeout=60)

    def saved_session(provider):
        path = session_file(provider, root, env)
        if path is not None and not path.is_file():
            raise SourceError('The configured session file is missing; reconnect the account or import its session again', 'configuration')
        return path

    def ddgs():
        result = sdk('ddgs', 'search', {'query': 'software engineer jobs', 'limit': 1,
                     'backend': env.get('APPLYPILOT_DDGS_BACKEND') or 'auto'})
        if not isinstance(result, list) or not result:
            raise SourceError('Search completed without a result; working search coverage was not established', 'empty_results')
        return 'Live web search returned a result.'

    def browser():
        result = sdk('playwright', 'crawl', {'url': 'https://example.com/'})
        if not result.get('html'):
            raise SourceError('Browser loaded no page content', 'empty_results')
        return 'Browser launched and read a public page.'

    def searxng():
        endpoint = (env.get('SEARXNG_URL') or 'http://127.0.0.1:8888').rstrip('/')
        with httpx.Client(timeout=httpx.Timeout(30, connect=5), trust_env=False, follow_redirects=False) as client:
            response = client.get(endpoint + '/search', params={'q': 'software engineer jobs', 'format': 'json'})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise SourceError('The search endpoint did not return JSON results; enable JSON search output in SearXNG', 'schema')
        if not payload['results']:
            failures = payload.get('unresponsive_engines') or []
            reason = '; '.join(': '.join(str(value) for value in row[:2]) for row in failures[:5] if isinstance(row, list))
            raise SourceError('Live search returned no results' + (': ' + reason if reason else ''), 'empty_results')
        return f"Live search returned {len(payload['results'])} results."

    def social(provider):
        path = saved_session(provider)
        if path is None:
            return None
        result = sdk(provider, 'verify', {'session_file': str(path)})
        if provider == 'twikit':
            username = str(result.get('screen_name') or '')
            if not username or not result.get('id'):
                raise SourceError('X did not return the signed-in account identity', 'auth')
            expected = accounts.get('x') or profile.get('x_url')
            if expected:
                from applypilot.referrals.identity import profile_key
                if profile_key(expected) != profile_key('https://x.com/' + username):
                    raise SourceError('The signed-in X account differs from the profile account; connect the intended account', 'identity_mismatch')
            return 'Signed-in X account verified: @' + username + '.'
        if not result.get('authenticated'):
            raise SourceError('LinkedIn did not confirm signed-in access', 'auth')
        return 'Saved LinkedIn session opened the signed-in feed.'

    def linkedin_rs():
        path = saved_session('linkedin_rs')
        if path is None:
            return None
        identifier = profile.get('linkedin_url') or accounts.get('linkedin')
        if not identifier:
            raise SourceError('Save your LinkedIn profile URL to check this reader', 'configuration')
        from applypilot.integrations.social import _li
        result = _li('profile', identifier, 1)
        if not result:
            raise SourceError('LinkedIn reader returned no profile', 'empty_results')
        return 'LinkedIn reader fetched the configured profile using the saved session.'

    def freehire():
        if str(env.get('FREEHIRE_ENABLED', 'true')).casefold() in {'false', '0', 'no'}:
            return None
        credentials = freehire_credentials(env)
        if not credentials['token']:
            return None
        from applypilot.discovery.freehire import _fetch_json_path
        result = _fetch_json_path('agent/jobs/search', {'q': 'software engineer', 'limit': 1},
                                 max_retries=1, timeout=25, auth=True)
        if not isinstance(result, dict):
            raise SourceError('FreeHire returned an invalid search response', 'schema')
        return 'Authenticated job search request succeeded; no credits spent.'

    checks = [('ddgs', ddgs), ('playwright', browser), ('searxng', searxng),
              ('twikit', lambda: social('twikit')), ('linkedin_scraper', lambda: social('linkedin_scraper')),
              ('linkedin_rs', linkedin_rs), ('freehire', freehire)]
    if platform:
        checks = [entry for entry in checks if entry[0] in ({'twikit'} if platform == 'x' else {'linkedin_scraper', 'linkedin_rs'})]

    def probe(name, callback):
        started = time.monotonic()
        try:
            detail = callback()
            row = {'component': name, 'ok': detail is not None, 'live': detail is not None,
                   'skipped': detail is None, 'detail': detail or 'Not configured; optional source skipped.'}
        except Exception as exc:
            chain = exception_details(exc, secrets=env).get('exception_chain', [])
            details = next((entry['diagnostics'] for entry in chain if entry.get('diagnostics')), {})
            details = {'exception_type': type(exc).__name__, **details}
            # Preserve the provider's real bounded failure, not a generic setup verdict.
            row = {'component': name, 'ok': False, 'live': True, 'detail': sanitize(str(exc), 1200, secrets=env),
                   'error_code': getattr(exc, 'kind', type(exc).__name__), 'diagnostics': details}
        row['elapsed_seconds'] = round(time.monotonic() - started, 2)
        return source_check_view(row)

    rows = {}
    progress(message='Checking live search, browser access, and saved account sessions…')
    with ThreadPoolExecutor(max_workers=len(checks), thread_name_prefix='source-check') as pool:
        futures = {pool.submit(probe, name, callback): name for name, callback in checks}
        for future in as_completed(futures):
            row = future.result()
            rows[row['component']] = row
            progress(message=f"Checked {len(rows)} of {len(checks)} sources: {row['component']} — {row['detail']}")
    return [rows[name] for name, _ in checks]

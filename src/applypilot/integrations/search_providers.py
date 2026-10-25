"""One search-provider request, with no scheduler, cache lease or execution budget.

HTTP and SDK I/O live here. Temporal chooses when/where to invoke this adapter.
Provider response page sizes bound payloads; they never cap a complete traversal.
Search results are evidence, not instructions or permission to visit a URL.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import math
import os
from urllib.parse import urlsplit, urlunsplit

import httpx

from .types import SearchHit, SourceError

MAX_RESPONSE_BYTES = 3_000_000


def retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Preserve actual Retry-After, including values exceeding one day.

    Missing/invalid headers leave backoff to Temporal's RetryPolicy, rather than
    inventing a provider restriction or saving a local cooldown timestamp.
    """
    if not value:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            due = parsedate_to_datetime(value)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            seconds = (due - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    try:
        timedelta(seconds=seconds)
    except OverflowError:
        return None  # Malformed value, not representable by the native SDK.
    return max(0.0, seconds)


def result_url(raw) -> str | None:
    """Validate an evidence link without making DNS/network requests.

    A subsequent crawler must independently validate every resolved destination
    and redirect. This function does not assert that DNS resolves publicly.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 8192:
        return None
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in raw):
        return None
    try:
        part = urlsplit(raw)
        host = (part.hostname or '').encode('idna').decode('ascii').lower().rstrip('.')
        if part.scheme not in {'http', 'https'} or not host or part.username or part.password:
            return None
        if part.port not in (None, 80, 443):
            return None
        if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if '.' not in host:
                return None
        authority = '[' + host + ']' if ':' in host else host
        if part.port and part.port != (443 if part.scheme == 'https' else 80):
            authority += ':' + str(part.port)
        return urlunsplit((part.scheme, authority, part.path or '/', part.query, ''))
    except (ValueError, UnicodeError):
        return None


def normalize_hits(rows, provider: str) -> list[SearchHit]:
    if not isinstance(rows, list):
        raise ValueError('Search provider did not return a list')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = result_url(row.get('href') or row.get('url'))
        if not url or url in seen:
            continue
        seen.add(url)
        result.append(SearchHit(url=url, title=str(row.get('title') or ''),
            snippet=str(row.get('body') or row.get('content') or ''),
            provider=provider, rank=len(result) + 1,
            published_at=str(row.get('date') or row.get('publishedDate'))
                if row.get('date') or row.get('publishedDate') else None))
    return result


def search_searxng(request, *, endpoint: str | None = None, transport=None) -> list[SearchHit]:
    request.validate()
    endpoint = endpoint or os.environ.get('SEARXNG_URL', '').strip() or 'http://127.0.0.1:8888'
    try:
        parts = urlsplit(endpoint)
        if (parts.scheme not in {'http', 'https'} or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError
        parts.port  # Force invalid-port validation before issuing a request.
    except ValueError:
        raise SourceError('SEARXNG_URL must be an HTTP(S) origin/base path without credentials or query', 'configuration') from None
    with httpx.Client(timeout=httpx.Timeout(30, connect=5), follow_redirects=False,
                      trust_env=False, transport=transport) as client:
        with client.stream('GET', endpoint.rstrip('/') + '/search', params={
            'q': request.query, 'format': 'json', 'pageno': request.page,
            'categories': request.category, 'safesearch': 1,
        }) as response:
            if not 200 <= response.status_code < 300:
                error = SourceError(f'Search provider returned HTTP {response.status_code}',
                                    'unknown', retry_after(response.headers.get('retry-after')))
                error.http_status = response.status_code
                raise error
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError('Search response exceeded the declared payload size')
    import json
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get('results'), list):
        raise ValueError('SearXNG JSON result schema is invalid')
    if not value['results'] and value.get('unresponsive_engines'):
        from applypilot.operations.runtime import sanitize
        detail = sanitize(json.dumps(value['unresponsive_engines'], ensure_ascii=True), 1600)
        raise RuntimeError('SearXNG reported failed upstream engines: ' + detail)
    return normalize_hits(value['results'], 'searxng')


def search_ddgs(request, *, factory=None) -> list[SearchHit]:
    request.validate()
    if factory is None:
        from ddgs import DDGS
        factory = DDGS
    backend = os.environ.get('APPLYPILOT_DDGS_BACKEND', 'auto').strip() or 'auto'
    engine = factory(timeout=30)
    method = engine.news if request.category == 'news' else engine.text
    rows = list(method(request.query, page=request.page,
                       max_results=request.page_size, backend=backend))
    return normalize_hits(rows, 'ddgs')

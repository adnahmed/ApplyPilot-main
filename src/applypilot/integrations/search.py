"""DDGS and self-hosted SearXNG behind one cached, free-only search interface."""
from __future__ import annotations

from applypilot import operations as ops

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from .net import canonical_url, check_status, public_url
from .state import SourceState
from .types import SearchHit, SourceError, bounded_int, stable_id
from .worker import call_worker


class SearchService:
    def __init__(self, backend=None, *, state=None, before_request=None):
        backend = backend or os.environ.get("APPLYPILOT_SEARCH_BACKEND", "auto")
        if backend not in {"auto", "ddgs", "searxng"}:
            raise SourceError("Only ddgs and self-hosted searxng search backends are supported")
        self.backend = backend
        self.state = state or SourceState()
        self.before_request = before_request
        self.last_errors = []

    @ops.observe('search', 'query')
    def search(self, query, *, limit=10, page=1, category="general", fresh=False, preserve_page=False):
        bounded_int(limit, "limit", 1, 50)
        if type(page) is not int or page < 1:
            raise SourceError("page must be a positive integer")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise SourceError("Search query must contain 1..1000 characters")
        if category not in {"general", "news"}:
            raise SourceError("Search category must be general or news")
        if self.backend == "auto":
            return self._combined(query, limit=limit, page=page, category=category, fresh=fresh, preserve_page=preserve_page)
        endpoint = getattr(self, "endpoint", None) or ((os.environ.get("SEARXNG_URL", "").strip() or "http://127.0.0.1:8888") if self.backend == "searxng" else "")
        engines = (os.environ.get("APPLYPILOT_DDGS_BACKEND", "auto").strip() or "auto") if self.backend == "ddgs" else ""
        key = "search:v2:" + stable_id([self.backend, endpoint, engines, query, limit, page, category, preserve_page])
        cached = None if fresh else self.state.get(key)
        if cached is not None:
            return [SearchHit(**row) for row in cached]
        if self.before_request:
            self.before_request()
        try:
            if self.backend == "ddgs":
                data = call_worker("ddgs", "search", {"query": query, "limit": limit, "page": page,
                                                     "category": category, "backend": engines})
            else:
                if not endpoint:
                    raise SourceError("Set SEARXNG_URL to your own SearXNG instance with JSON output enabled")
                # This configured service is trusted; search result destinations are not.
                endpoint = canonical_url(endpoint).rstrip("/") + "/search"
                if urlsplit(endpoint).query:
                    raise SourceError("SEARXNG_URL must be an origin/base path without a query")
                with httpx.Client(timeout=httpx.Timeout(30, connect=2), follow_redirects=False, trust_env=False) as client:
                    response = client.get(endpoint, params={"q": query, "format": "json", "pageno": page,
                                                           "categories": category, "safesearch": 1})
                    check_status(response.status_code, response.headers)
                    if 300 <= response.status_code < 400:
                        raise SourceError("SearXNG redirects are not followed; configure the final endpoint")
                    if len(response.content) > 3_000_000:
                        raise ValueError("SearXNG response exceeds 3 MB")
                    body = response.json()
                    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                        raise ValueError("SearXNG must enable search.formats: [html, json]")
                    data = body["results"]
                    if not data and body.get("unresponsive_engines"):
                        from applypilot.operations.runtime import sanitize
                        failures = body["unresponsive_engines"]
                        reasons = []
                        for failure in failures[:10] if isinstance(failures, list) else []:
                            if (isinstance(failure, (list, tuple)) and len(failure) >= 2
                                    and isinstance(failure[0], str) and isinstance(failure[1], str)):
                                reasons.append(f"{sanitize(failure[0], 80)}: {sanitize(failure[1], 160)}")
                        reason = "; ".join(reasons)[:1600]
                        error = SourceError("SearXNG engines failed; no search coverage was established"
                                            + (f" ({reason})" if reason else ""),
                                            'unknown')
                        error.details = {"phase": "upstream_engines", "http_status": response.status_code,
                                         "reason": reason}
                        raise error
        except Exception as exc:
            ops.capture(exc, component_name='search', operation="fallback_boundary", severity="warning")
            raise
        if not isinstance(data, list):
            raise ValueError("Search provider did not return a list")
        hits, seen = [], set()
        for row in data:
            if not isinstance(row, dict):
                continue
            try:
                url = public_url(row.get("href") or row.get("url") or "", resolve=False)
            except SourceError:
                continue
            if url in seen:
                continue
            seen.add(url)
            hits.append(SearchHit(url, str(row.get("title") or "")[:1000],
                                  str(row.get("body") or row.get("content") or ""),
                                  self.backend, len(hits) + 1,
                                  str(row.get("date") or row.get("publishedDate")) if row.get("date") or row.get("publishedDate") else None))
            if not preserve_page and len(hits) >= limit:
                break
        self.state.put(key, [h.to_dict() for h in hits], ttl=1800)
        return hits

    def _combined(self, query, **options):
        """Merge complete provider reads; failures propagate to the Activity owner."""
        self.last_errors = []
        # The deployment creates schemas. A search never performs DDL.
        def run(name):
            child = SearchService(name, state=self.state, before_request=self.before_request)
            if name == "searxng":
                child.endpoint = os.environ.get("SEARXNG_URL", "").strip() or "http://127.0.0.1:8888"
            return child.search(query, **options)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="source-search") as pool:
            futures = [(name, ops.submit(pool, run, name)) for name in ("searxng", "ddgs")]
            batches, failures = [], []
            for name, future in futures:
                try:
                    batches.append(future.result())
                except (SourceError, httpx.HTTPError) as exc:
                    from applypilot.operations.runtime import sanitize
                    failures.append(exc)
                    self.last_errors.append({"provider": name, "error": sanitize(str(exc), 1000),
                                             "kind": getattr(exc, "kind", "network")})
            if not any(batches) and failures:
                raise failures[0]
        rows = {}; scores = {}; providers = {}
        for hits in batches:
            for hit in hits:
                rows.setdefault(hit.url, hit)
                scores[hit.url] = scores.get(hit.url, 0) + 1 / (60 + hit.rank)
                providers.setdefault(hit.url, []).append(hit.provider)
        ordered = sorted(rows, key=lambda url: (-scores[url], url))
        if not options.get("preserve_page"):
            ordered = ordered[:options["limit"]]
        return [replace(rows[url], provider="+".join(dict.fromkeys(providers[url])), rank=i+1) for i, url in enumerate(ordered)]

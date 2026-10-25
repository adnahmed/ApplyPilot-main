"""Bounded web documents with optional Playwright and authorized BrowserOS reads."""
from __future__ import annotations

from applypilot import operations as ops

from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import os
from dataclasses import replace
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from markdownify import markdownify

from .net import fetch, public_url
from .state import SourceState
from .types import Document, SourceError, bounded_int, stable_id
from .worker import call_worker


def html_document(url: str, html: str, *, provider="http", markdown=None, extra_links=()):
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    structured = []
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.get_text())
            if isinstance(data, (dict, list)):
                structured.extend(data if isinstance(data, list) else [data])
        except (ValueError, TypeError):
            continue
    links = []
    for value in [a.get("href") for a in soup.select("a[href]")] + list(extra_links):
        try:
            links.append(public_url(urljoin(url, value), resolve=False))
        except (SourceError, TypeError):
            continue
    for node in soup(["script", "style", "noscript", "nav", "footer", "iframe", "form"]):
        node.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = markdown if isinstance(markdown, str) and markdown.strip() else markdownify(str(main), heading_style="ATX")
    return Document(url, title, text, tuple(dict.fromkeys(links)), provider,
                    structured_data=tuple(v for v in structured if isinstance(v, dict)))


class CrawlService:
    def __init__(self, backend=None, *, state=None, before_request=None, allow_browseros=False):
        self.backend = backend or os.environ.get("APPLYPILOT_CRAWLER", "auto")
        if self.backend not in {"auto", "hybrid", "http", "playwright", "browseros", "flaresolverr"}:
            raise SourceError("Choose auto, hybrid, http, playwright, browseros or flaresolverr")
        if self.backend == "browseros" and not allow_browseros:
            raise SourceError("BrowserOS page reads require explicit session authorization", "access_denied")
        self.allow_browseros = allow_browseros
        self.state = state or SourceState()
        self.before_request = before_request
        self.last_errors = []

    @ops.observe('crawl', 'read')
    def read(self, url: str, *, fresh=False) -> Document:
        self.last_errors = []
        if self.backend in {"auto", "hybrid"} or (self.allow_browseros and self.backend != "browseros"):
            return self._complementary(url, fresh=fresh)
        url = public_url(url)
        key = "document:v1:" + stable_id([self.backend, url])
        cached = None if fresh else self.state.get(key)
        if cached is not None:
            return Document.from_dict(cached)
        try:
            if self.backend == "flaresolverr":
                from applypilot.browser.cloudflare import FlareSolverrClient, is_browser_challenge
                if self.before_request:
                    self.before_request()
                solution, html = FlareSolverrClient(
                    proxy=os.environ.get("APPLYPILOT_BROWSER_PROXY_URL")).solve_for_html(url)
                final_url = public_url(solution.final_url)
                if is_browser_challenge(html):
                    error = SourceError("Solver returned a browser challenge", "unknown")
                    error.challenge_url = final_url
                    raise error
                doc = html_document(final_url, html, provider="flaresolverr")
            elif self.backend == "browseros":
                doc = self._browseros_document(url)
            elif self.backend == "playwright":
                from applypilot.execution import browser_operation
                if self.before_request:
                    self.before_request()
                with browser_operation():
                    data = call_worker("playwright", "crawl", {"url": url})
                final_url = public_url(data["url"])
                doc = html_document(final_url, data.get("html", ""), provider="playwright",
                                    markdown=data.get("markdown"), extra_links=data.get("links", ()))
            else:
                final_url, raw, content_type = fetch(url, before_request=self.before_request)
                if content_type and not any(t in content_type.lower() for t in ("html", "text/plain", "application/xhtml")):
                    raise SourceError("Only HTML/text is accepted; no automatic file/PDF downloads", "schema")
                doc = html_document(final_url, raw.decode("utf-8", errors="replace"))
            if not doc.markdown.strip() and not doc.structured_data:
                raise SourceError("Page returned no usable text", "schema")
            self.state.put(key, doc.to_dict(), ttl=21600)
            return doc
        except SourceError as exc:
            ops.capture(exc, component_name='crawl', operation="fallback_boundary", severity="warning")
            raise

    def _browseros_document(self, url):
        from applypilot.execution import ExecutionStopped, current_task
        from applypilot.reasoning.budget import InferenceCancelled, cancellable
        from applypilot.work_context import stage_context
        from applypilot.unattended.browser import BrowserOS

        stop_events = tuple(event for event in (
            current_task().get("stop_event"),
            stage_context().get("stop_event"),
        ) if event is not None)

        async def collect():
            async with BrowserOS() as browser:
                return await browser.read(url)

        async def run():
            return await cancellable(collect(), timeout=100, cancel_events=stop_events)

        if self.before_request:
            self.before_request()
        try:
            # Enrichment can already own a sync-Playwright event loop. Keep the
            # owned MCP session and its cleanup together in a separate loop.
            with ThreadPoolExecutor(max_workers=1) as pool:
                data = ops.submit(pool, lambda: asyncio.run(run())).result()
        except InferenceCancelled as exc:
            raise ExecutionStopped(str(exc)) from exc
        final_url = public_url(data.get("url", ""))
        text = data.get("text")
        if not isinstance(text, str):
            raise SourceError("BrowserOS returned no page text", "schema")
        links = [item.get("url") for item in data.get("links", ()) if isinstance(item, dict)]
        doc = html_document(final_url, "", provider="browseros", markdown=text, extra_links=links)
        structured = []
        for raw in data.get("json_ld", ()):
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            structured.extend(item for item in (value if isinstance(value, list) else [value]) if isinstance(item, dict))
        return replace(doc, title=str(data.get("title") or ""), structured_data=tuple(structured),
                       truncated=bool(data.get("truncated")))

    def _complementary(self, url, *, fresh=False):
        """Compatibility client for native static/rendered evidence traversal."""
        from applypilot.orchestration.company_client import crawl
        if self.before_request:
            self.before_request()
        result = asyncio.run(crawl(public_url(url), backend=self.backend,
                                   allow_browseros=self.allow_browseros, fresh=fresh))
        return Document.from_dict(result['documents'][0])

    def crawl(self, url: str, *, max_pages=5, depth=1, focus=None):
        bounded_int(max_pages, 'max_pages', 1, 20)
        bounded_int(depth, 'depth', 0, 2)
        if focus is not None and (not isinstance(focus, str) or len(focus) > 500):
            raise SourceError('focus must contain at most 500 characters', 'schema')
        from applypilot.orchestration.company_client import crawl
        return asyncio.run(crawl(public_url(url), backend=self.backend, allow_browseros=self.allow_browseros,
                                 max_pages=max_pages, depth=depth, focus=focus))

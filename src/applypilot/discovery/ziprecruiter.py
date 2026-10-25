"""Standalone ZipRecruiter HTML adapter, separate from JobSpy zip_recruiter.

Explicit US/CA native requests use one FlareSolverr HTML operation per page.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import transport
from .builtin import (_check_access, _failure_page, _finish_page, _html_next,
                      _integer, _listing_facts, _page_state, _query, _run_source)
from .models import DiscoveryPage, SearchTask

_BASE = "https://www.ziprecruiter.com"
_MAX_PAGES = -1  # Work volume is uncapped; requests are still one native page.


class _JobCardParser(HTMLParser):
    """Native article parser; nested spans do not prematurely close title capture."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.jobs = []
        self._in_article = False
        self._current = {}
        self._capture_tag = None
        self._capture_data = []
        self._capture_depth = None
        self._depth = 0
        self._is_job = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "article":
            if self._in_article:
                raise ValueError("Nested or unclosed ZipRecruiter article")
            self._in_article = True
            self._depth = 0
            self._current = {"posting_id": attrs.get("data-job-id") or attrs.get("data-jobid"),
                             "raw_record": {"article_attributes": attrs}}
            self._is_job = bool(self._current["posting_id"]) or "job" in attrs.get("class", "").lower()
        if not self._in_article:
            return
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self._depth += 1
        classes = attrs.get("class", "")
        marker = attrs.get("data-testid", "")
        field = None
        if tag == "a" and ("job_link" in classes or marker in {"job-card-title", "job-title"}):
            self._current["url"] = attrs.get("href")
            self._is_job = True
            field = "title"
        elif "company_name" in classes or marker == "job-card-company":
            field = "company"
        elif "location" in classes or marker == "job-card-location":
            field = "location"
        elif "salary" in classes or marker == "job-card-salary":
            field = "salary"
        elif "job_snippet" in classes or marker == "job-card-description":
            field = "description"
        elif "work_mode" in classes or marker == "job-card-workplace":
            field = "work_mode"
        if field and self._capture_tag is None:
            self._capture_tag, self._capture_depth, self._capture_data = field, self._depth, []

    def handle_endtag(self, tag):
        if not self._in_article:
            return
        if self._capture_tag and self._depth == self._capture_depth:
            self._current[self._capture_tag] = " ".join(self._capture_data).strip() or None
            self._capture_tag, self._capture_depth, self._capture_data = None, None, []
        self._depth -= 1
        if tag == "article":
            if self._is_job:
                self.jobs.append(self._current)
            self._in_article = False
            self._current = {}

    def handle_startendtag(self, tag, attrs):
        if tag not in {"br", "img", "input", "hr", "meta", "link", "source", "wbr"}:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self._capture_tag and self._in_article:
            self._capture_data.append(data)


def _posting_id(url):
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    for key in ("jid", "job_id", "jobid", "lk"):
        if params.get(key):
            return params[key][0]
    match = re.search(r"/jobs/[^/]+/(?P<id>[a-z0-9]{16,})/?$", parsed.path, re.I)
    return match["id"] if match else None


def _regex_fallback(html):
    # Keep the native job_link fallback, but do not depend on attribute order.
    soup = BeautifulSoup(html, "html.parser")
    jobs = []
    for link in soup.select("a.job_link[href]"):
        card = link.find_parent("article") or link
        jobs.append({"url": link["href"], "title": link.get_text(" ", strip=True),
                     "raw_record": {"card_html": str(card)}})
    return jobs


def _parse_search_html(html):
    parser = _JobCardParser()
    parser.feed(html)
    parser.close()
    if parser._in_article:
        raise ValueError("Truncated ZipRecruiter article")
    rows = parser.jobs or _regex_fallback(html)
    jobs = []
    for row in rows:
        url = urljoin(_BASE, row["url"]) if row.get("url") else None
        row["raw_record"] = {**row.get("raw_record", {}), **{k: v for k, v in row.items() if k != "raw_record"}}
        row["url"] = url
        row["posting_id"] = row.get("posting_id") or (_posting_id(url) if url else None)
        jobs.append(_listing_facts(row, "ziprecruiter"))
    return jobs


def _build_search_url(query, location=None, radius=50, *, country, page=1):
    if country not in {"US", "CA"}:
        raise ValueError("Standalone ZipRecruiter country must be US or CA")
    _integer(radius, "radius", 0, 500)
    _integer(page, "page", 1, _MAX_PAGES)
    if location is not None and not isinstance(location, str):
        raise ValueError("ZipRecruiter location must be a string")
    # The country is an explicit search target, never a fabricated posting location.
    params = {"search": _query(query), "location": location or {"US": "United States", "CA": "Canada"}[country],
              "radius": radius, "page": page}
    return _BASE + "/jobs-search?" + urlencode(params)


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    try:
        params = transport.task_params(task, ("ziprecruiter",))
        _check_access(context, "ziprecruiter")
        if set(params) - {"query", "country", "location", "radius", "page"}:
            raise ValueError("Unsupported standalone ZipRecruiter request parameters")
        start = _integer(params.get("page", 1), "page", 1, _MAX_PAGES)
        page, previous = _page_state(task, cursor, start, _MAX_PAGES)
        url = _build_search_url(params.get("query"), params.get("location"), params.get("radius", 50),
                                country=params.get("country"), page=page)
        transport.reserve_budget(context, "http", 1)
        fetch = getattr(context, "fetch_html", None)
        result = (fetch(url, timeout=60) if fetch else transport.fetch_with_transport(
            url, timeout=60, force_layer="flaresolverr"))
        if isinstance(result, transport.TransportError):
            raise transport.TaskDeferred(result.error, result.kind, http_status=result.http_status,
                                         retry_after=result.retry_after)
        html = result.html
        rows = _parse_search_html(html)
        records = [row for row in rows if row.get("title") and row.get("url") and
                   urlparse(row["url"]).scheme in {"http", "https"} and
                   (row.get("posting_id") or re.search(r"/(?:jobs/|c/[^/]+/Job/)", urlparse(row["url"]).path, re.I))]
        if len(rows) != len(records):
            return _failure_page(task, context, ValueError("Malformed ZipRecruiter job cards"), cursor,
                records, request_url=url, invalid_records=len(rows) - len(records))
        if not records and not re.search(r"\b(?:no jobs found|no results found|0 jobs|no matching jobs)\b",
                                         BeautifulSoup(html, "html.parser").get_text(" ", strip=True), re.I):
            raise ValueError("Unrecognized ZipRecruiter HTML, not a confirmed empty search")
        next_page = _html_next(html, url, page, "page", records=records)
        return _finish_page(task, context, records, page, next_page, previous, _MAX_PAGES,
            request_url=url, native_country=params["country"], contract="observed standalone ZipRecruiter HTML")
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor)


def run_ziprecruiter_discovery(workers: int = 1, *, context=None) -> dict:
    return _run_source("ziprecruiter", workers, context)

"""IrishJobs Ireland-native HTML listings, with no implicit browser escalation."""
from __future__ import annotations

import re
from urllib.parse import urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import transport
from .builtin import (_check_access, _failure_page, _fetch, _finish_page, _html_next,
                      _integer, _listing_facts, _page_state, _query, _run_source)
from .models import DiscoveryPage, SearchTask

_BASE = "https://www.irishjobs.ie"
_IRISHJOBS_SEARCH_URL = _BASE + "/JobSearch/Results"
_MAX_PAGES = -1  # Work volume is uncapped; requests are still one native page.


def _search_url(query, location="", page=1):
    return _IRISHJOBS_SEARCH_URL + "?" + urlencode({
        "Keywords": _query(query), "Location": location, "Page": page})


def _fetch_irishjobs_html(query, location="", *, context, page=1):
    """One HTTP attempt. Denials/challenges stop here, not in another browser."""
    return _fetch(_search_url(query, location, page), context=context, provider="irishjobs")


def _posting_id(url):
    path = urlparse(url).path
    match = re.search(r"(?:job[-/]|[-/])(\d+)(?:\.aspx)?/?$", path, re.I)
    return match[1] if match else None


def _parse_irishjobs_html(html: str) -> list[dict]:
    """Read the existing job-card/link contract without treating navigation as jobs.

    Malformed recognized cards remain in the result for the executor to report
    partial coverage. Optional company/location spans are scoped to each card.
    """
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.select(
        'article[data-testid="job-item"], [data-testid="job-card"], '
        '.job-result, .job-result-card, .job-card, .job-listing, .job')
    # Restrict the fallback to actual posting paths, not /JobSearch/Results links.
    links = soup.find_all("a", href=re.compile(r"/(?:job/|JobSearch/.*\d+\.aspx)", re.I))
    for link in links:
        if any(parent in cards for parent in link.parents):
            continue
        card = link.find_parent(["article", "div", "li"]) or link
        if card not in cards:
            cards.append(card)
    jobs, seen = [], set()
    for card in cards:
        # Wrapper containers are not additional postings.
        if any(parent in cards for parent in card.parents):
            continue
        title = card.select_one('a[data-testid="job-item-title"], a.job-title, h2 a, h3 a')
        if title is None:
            title = card.find("a", href=re.compile(r"/(?:job/|JobSearch/.*\d+\.aspx)", re.I))
        if title is None and card.name == "a":
            title = card
        url = urljoin(_BASE, title.get("href", "")) if title else None
        if url in seen and url is not None:
            continue
        if url:
            seen.add(url)

        def text(selector):
            node = card.select_one(selector)
            return node.get_text(" ", strip=True) or None if node else None

        posting_id = card.get("data-job-id") or card.get("data-id") or (_posting_id(url) if url else None)
        jobs.append(_listing_facts({
            "url": url, "posting_id": posting_id,
            "title": title.get_text(" ", strip=True) or None if title else None,
            "company": text('[data-testid="job-item-company"], .company, .company-name, [class*="company"]'),
            "location": text('[data-testid="job-item-location"], .location, [class*="location"]'),
            "salary": text('[data-testid="job-item-salary"], .salary, [class*="salary"]'),
            "work_mode": text('.work-mode, .workplace-type, [data-testid="job-item-workplace"]'),
            "description": text('.description, .job-description, .job-snippet, [data-testid="job-item-teaser"]'),
            "raw_record": {"card_html": str(card), "posting_id": posting_id},
        }, "irishjobs"))
    return jobs


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    try:
        params = transport.task_params(task, ("irishjobs",))
        _check_access(context, "irishjobs")
        if set(params) - {"query", "country", "location", "page"}:
            raise ValueError("Unsupported IrishJobs request parameters")
        if params.get("country") != "IE":
            raise ValueError("IrishJobs requires an explicit IE-native request")
        query = _query(params.get("query"))
        location = params.get("location", "")
        if not isinstance(location, str):
            raise ValueError("IrishJobs location must be a string")
        start = _integer(params.get("page", 1), "page", 1, _MAX_PAGES)
        page, previous = _page_state(task, cursor, start, _MAX_PAGES)
        url = _search_url(query, location, page)
        html = _fetch_irishjobs_html(query, location, context=context, page=page)
        rows = _parse_irishjobs_html(html)
        records = [row for row in rows if row.get("title") and row.get("url") and (
            _posting_id(row["url"]) or row.get("posting_id")) and
            urlparse(row["url"]).scheme in {"http", "https"}]
        if len(rows) != len(records):
            return _failure_page(task, context, ValueError("Malformed IrishJobs job cards"), cursor,
                records, request_url=url, invalid_records=len(rows) - len(records))
        if not records and not re.search(r"\b(?:no jobs found|no results found|0 jobs|no matching jobs)\b",
                                         BeautifulSoup(html, "html.parser").get_text(" ", strip=True), re.I):
            raise ValueError("Unrecognized IrishJobs HTML, not a confirmed empty search")
        next_page = _html_next(html, url, page, "Page", records=records)
        return _finish_page(task, context, records, page, next_page, previous, _MAX_PAGES,
            request_url=url, native_country="IE", contract="observed IrishJobs HTML")
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor)


def run_irishjobs_discovery(workers: int = 1, *, context=None) -> dict:
    return _run_source("irishjobs", workers, context)

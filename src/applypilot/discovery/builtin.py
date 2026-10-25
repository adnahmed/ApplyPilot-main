"""Built In's observed HTML contract, executed one planned page at a time.

The boundary helpers are also used by the other native index adapters. They do
not plan searches, publish rows, or infer eligibility from an index country.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from . import transport
from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask
from .normalization import country_code, scalar_text

_BASE = "https://builtin.com"
_HEADERS = {"Accept": "text/html,application/xhtml+xml"}
_MAX_PAGES = -1  # Work volume is uncapped; requests are still one native page.


def _strip_html(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def _check_access(context, provider):
    if not transport.authorized(context, provider):
        raise transport.TaskDeferred(
            f"{provider} requires explicit source authorization", FailureKind.ACCESS_DENIED)


def _integer(value, name, minimum, maximum):
    if type(value) is not int or value < minimum or (maximum != -1 and value > maximum):
        bound = f"at least {minimum}" if maximum == -1 else f"in {minimum}..{maximum}"
        raise ValueError(f"{name} must be an integer {bound}")
    return value


def _query(value):
    if isinstance(value, dict):
        value = value.get("query")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("A nonempty role query is required")
    return value.strip()


def _page_state(task, cursor, start=1, maximum=_MAX_PAGES):
    _integer(start, "initial page/offset", 0, maximum)
    if cursor is None:
        return start, None
    state = json.loads(cursor)
    if not isinstance(state, dict) or state.get("request") != task.target.request_key():
        raise ValueError("Cursor does not belong to this exact request")
    position = _integer(state.get("position"), "cursor position", start, maximum)
    previous = state.get("previous_ids")
    if not isinstance(previous, list) or any(not isinstance(x, str) for x in previous):
        raise ValueError("Cursor requires previous native posting identities")
    seen = state.get("seen_pages", [])
    if not isinstance(seen, list) or any(
            not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in seen):
        raise ValueError("Cursor contains invalid page fingerprints")
    return position, {"ids": previous, "seen_pages": seen}


def _failure_page(task, context, error, cursor=None, records=(), **metadata):
    if isinstance(error, transport.TaskDeferred):
        failure = error.failure
    elif isinstance(error, transport.TransportError):
        failure = FetchFailure(error.kind, error.error, error.http_status, error.retry_after)
    else:
        raise error
    return DiscoveryPage(task.task_id or "", tuple(records), next_cursor=cursor,
        complete=False, partial=bool(records), coverage=Coverage.PARTIAL if records else Coverage.UNKNOWN,
        failures=(failure,), request_metadata=transport.page_metadata(task, context,
            status="deferred" if failure.kind in {FailureKind.AUTH, FailureKind.ACCESS_DENIED} else "failed",
            authoritative_snapshot=False, **metadata))


def _fetch(url, *, context, provider="builtin", headers=None, json_response=False, timeout=20.0):
    """One real attempt, no retries, redirects, detail lookups or browser fallback."""
    _check_access(context, provider)
    transport.reserve_budget(context, "http", 1)
    if json_response:
        fetch = getattr(context, "fetch_json", None) or transport.fetch_json_with_transport
        data, result = fetch(url, headers=headers, timeout=timeout)
    else:
        fetch = getattr(context, "fetch_html", None) or transport.fetch_with_transport
        result = fetch(url, headers=headers or _HEADERS, timeout=timeout)
        data = None
    if isinstance(result, transport.TransportError):
        raise transport.TaskDeferred(result.error, result.kind, http_status=result.http_status,
                                     retry_after=result.retry_after)
    if result is not None:
        if not 200 <= result.status_code < 300 or transport.access_blocked(result.html, result.url):
            error = transport._response_error(result)
            raise transport.TaskDeferred(error.error, error.kind, http_status=error.http_status,
                                         retry_after=error.retry_after)
        if not json_response:
            data = result.html
    if not json_response and (not isinstance(data, str) or not data.strip()):
        raise ValueError("Empty HTML response is not an empty search")
    return data


def _finish_page(task, context, records, position, next_position, previous, maximum, **metadata):
    ids = sorted({str(row.get("posting_id") or row["url"]) for row in records})
    fingerprint = hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()
    previous_ids = previous.get("ids") if isinstance(previous, dict) else previous
    seen = list(previous.get("seen_pages", [])) if isinstance(previous, dict) else []
    repeated = bool(records) and (previous_ids == ids or fingerprint in seen)
    stalled = next_position is not None and (not records or next_position <= position)
    bounded = next_position is not None and maximum != -1 and next_position > maximum
    capped = repeated or stalled or bounded
    exhausted = next_position is None or capped
    if fingerprint not in seen:
        seen.append(fingerprint)
    continuation = None if exhausted else json.dumps({
        "request": task.target.request_key(), "position": next_position,
        "previous_ids": ids, "seen_pages": seen}, sort_keys=True)
    if capped:
        metadata["stop_reason"] = ("repeated_page" if repeated else
                                   "nonprogress" if stalled else "connector_ceiling")
    return DiscoveryPage(task.task_id or "", tuple(records), next_cursor=continuation,
        exhausted=exhausted, complete=not capped, partial=capped, capped=capped,
        coverage=Coverage.CAPPED if capped else Coverage.COMPLETE if records else Coverage.EMPTY,
        request_metadata=transport.page_metadata(task, context, position=position,
            authoritative_snapshot=False, **metadata))


def _html_next(html, url, page, page_key, *, records):
    """Follow only a native next-page link for the same query, never arbitrary URLs."""
    soup = BeautifulSoup(html, "html.parser")
    current = urlparse(url)
    expected = parse_qs(current.query, keep_blank_values=True)
    expected.pop(page_key, None)
    for link in soup.select("a[href]"):
        rel = link.get("rel", [])
        label = " ".join((link.get_text(" ", strip=True), link.get("aria-label", ""),
                          link.get("title", ""))).lower()
        is_next = "next" in rel or bool(re.search(r"\bnext\b", label))
        if link.get("aria-disabled") == "true" or "disabled" in link.get("class", []):
            continue
        parsed = urlparse(urljoin(url, unescape(link["href"])))
        params = parse_qs(parsed.query, keep_blank_values=True)
        value = params.pop(page_key, None)
        if parsed.netloc != current.netloc or parsed.path != current.path:
            continue
        if value and len(value) == 1 and value[0].isdigit() and int(value[0]) == page + 1:
            if params != expected:
                if is_next:
                    raise ValueError("Native continuation changed the search parameters")
                continue
            if not records:
                raise ValueError("Empty search unexpectedly has a continuation")
            return page + 1
        if is_next:
            raise ValueError("Malformed or non-advancing native continuation")
    return None


def _listing_facts(job, provider):
    """Map display facts without making the website's country an applicant rule."""
    job = dict(job)
    job.setdefault("raw_record", dict(job))
    job.update(provider=provider, source=provider)
    job["posting_id"] = scalar_text(job.get("posting_id") or job.get("external_id"))
    job["source_job_id"] = job["posting_id"]
    location = scalar_text(job.get("location"))
    mode_text = scalar_text(job.get("work_mode"))
    mode = {"remote": "remote", "hybrid": "hybrid", "in-office": "onsite",
            "on-site": "onsite", "onsite": "onsite", "on site": "onsite"}.get((mode_text or "").lower())
    # Only a role's explicit remote label carries scope; a separate office does not.
    scoped = re.fullmatch(r"remote\s*(?:\(([^)]+)\)|[-:]+\s*(.+)|\s+(worldwide|nationwide|anywhere))",
                          location or "", re.I)
    if (location or "").lower() == "remote" or scoped:
        mode = mode or "remote"
    job["work_mode"] = mode
    job["office_locations"] = [location] if location and not scoped and location.lower() not in {
        "remote", "worldwide", "anywhere", "nationwide"} else []
    job["office_countries"] = [country_code(location)] if country_code(location) and not scoped else []
    if scoped:
        scope = next(x for x in scoped.groups() if x).strip()
        countries = [country_code(x) for x in re.split(r"\s*(?:,|/|\bor\b)\s*", scope, flags=re.I)]
        option = {"id": "remote", "work_mode": "remote"}
        if scope.lower() in {"worldwide", "anywhere in the world"}:
            option["eligible_scope"] = "worldwide"
        elif countries and all(countries):
            option.update(countries=countries, eligible_scope="restricted")
        else:
            option["residency_requirements"] = [location]
        for key in ("schedule", "engagement"):
            if job.get(key):
                option[key] = job[key]
        job["work_arrangements"] = [option]
    job["identity"] = {"provider": provider, "posting_id": job["posting_id"], "listing_url": job.get("url")}
    return job


def _run_source(provider, workers, context=None):
    from .catalog import request_source
    return request_source(provider, context=context, workers=workers)


def _build_url(category="", city=None, remote_only=False, page=1, *, query=None):
    path = "/jobs" + ("/remote" if remote_only else "")
    if category:
        if not re.fullmatch(r"[a-z0-9-]+", category):
            raise ValueError("Invalid Built In category")
        path += "/" + category
    params = {"search": _query(query)} if query is not None else {}
    if city:
        params["city"] = city
    if page > 1:
        params["page"] = page
    return _BASE + path + ("?" + urlencode(params) if params else "")


def _parse_card(card_html: str, job_id: str) -> dict | None:
    card = BeautifulSoup(card_html, "html.parser")
    title = card.select_one('a[href^="/job/"]')
    if title is None or not title.get_text(" ", strip=True):
        return None
    company = card.select_one('a[href^="/company/"]')
    summary = card.select_one(".text-gray-04.fs-sm")
    fields = list(dict.fromkeys(node.get_text(" ", strip=True) for node in card.select("span.font-barlow")))
    mode = location = salary = seniority = None
    for text in fields:
        if re.search(r"\b(remote|hybrid|in-office|on-site)\b", text, re.I):
            mode = mode or text
            if not re.fullmatch(r"remote|hybrid|in-office|on-site|remote or hybrid", text, re.I):
                location = location or text
        elif re.search(r"\d", text) and re.search(r"annually|hourly|\bK\b|[$\u20ac\u00a3]|\dK", text, re.I):
            salary = salary or text
        elif re.search(r"level|intern|expert", text, re.I):
            seniority = seniority or text
        elif text:
            location = location or text
    company_slug = company["href"].split("/")[-1] if company else None
    return _listing_facts({
        "url": urljoin(_BASE, title["href"]), "external_id": job_id, "posting_id": job_id,
        "title": title.get_text(" ", strip=True),
        "company": company.get_text(" ", strip=True) or None if company else None,
        "company_slug": company_slug, "location": location, "work_mode": mode,
        "salary": salary, "seniority": seniority,
        "description": summary.get_text(" ", strip=True) if summary else None,
        "raw_record": {"job_id": job_id, "card_html": card_html, "display_fields": fields,
                       "work_mode": mode, "location": location, "salary": salary, "seniority": seniority},
    }, "builtin")


def _parse_listing_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    jobs = {}
    for card in soup.find_all(id=re.compile(r"^job-card-\d+$")):
        job_id = card["id"].removeprefix("job-card-")
        job = _parse_card(str(card), job_id)
        if job:
            jobs[job_id] = job
    return list(jobs.values())


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    try:
        params = transport.task_params(task, ("builtin",))
        _check_access(context, "builtin")
        if set(params) - {"query", "country", "page", "page_size", "city", "category", "remote_only"}:
            raise ValueError("Unsupported Built In request parameters")
        if params.get("country") != "US":
            raise ValueError("Built In requires an explicit US-native request")
        if "page_size" in params:
            _integer(params["page_size"], "page_size", 1, 100)
        if "remote_only" in params and type(params["remote_only"]) is not bool:
            raise ValueError("remote_only must be a boolean")
        for name in ("city", "category"):
            if name in params and not isinstance(params[name], str):
                raise ValueError(f"{name} must be a string")
        start = _integer(params.get("page", 1), "page", 1, _MAX_PAGES)
        page, previous = _page_state(task, cursor, start)
        url = _build_url(params.get("category", ""), params.get("city"),
                         params.get("remote_only", False), page, query=_query(params.get("query")))
        html = _fetch(url, context=context)
        records = _parse_listing_page(html)
        soup = BeautifulSoup(html, "html.parser")
        cards = {node["id"] for node in soup.find_all(id=re.compile(r"^job-card-\d+$"))}
        if len(records) != len(cards):
            return _failure_page(task, context, ValueError("Malformed Built In job cards"), cursor,
                                 records, request_url=url, invalid_records=len(cards) - len(records))
        if not records and not re.search(r"\b(?:no jobs found|no results found|0 jobs|no matching jobs)\b",
                                         soup.get_text(" ", strip=True), re.I):
            raise ValueError("Unrecognized Built In listing HTML, not a confirmed empty search")
        next_page = _html_next(html, url, page, "page", records=records)
        return _finish_page(task, context, records, page, next_page, previous, _MAX_PAGES,
            request_url=url, native_country="US", contract="observed Built In HTML",
            page_size_policy="provider-controlled; no invented native page-size filter")
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor)


def _insert_jobs(conn, jobs, *, context=None):
    from .context import build_context
    from .repository import publish_records
    context = context if context is not None else build_context(sources=["builtin"])
    result = publish_records([_listing_facts(job, "builtin") for job in jobs], "builtin", context=context, conn=conn)
    return result.new, result.updated + result.strict_duplicates


def run_builtin_discovery(workers: int = 1, *, context=None) -> dict:
    return _run_source("builtin", workers, context)

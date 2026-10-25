"""Amazon's observed website search contract, not a documented public API.

Task execution fetches one page only. Search geography is never eligibility.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask

SEARCH_URL = "https://www.amazon.jobs/en/search.json"
_HEADERS = {"User-Agent": "ApplyPilot/1.0 (job-discovery)", "Accept": "application/json"}


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def _amazon_query(query: str) -> str:
    """Keep Amazon vocabulary inside the adapter, including equivalent SDE probes."""
    query = re.sub(r"\bsoftware engineer\b", "software development engineer", query, flags=re.I)
    return re.sub(r"\bsde\b", "software development engineer", query, flags=re.I).strip().lower()


def _fetch_page(params: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params, doseq=True)}", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _failure(task, exc) -> DiscoveryPage:
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    if status:
        kind = {401: FailureKind.AUTH, 403: FailureKind.ACCESS_DENIED, 429: FailureKind.RATE_LIMIT}.get(
            status, FailureKind.NETWORK if status >= 500 else FailureKind.CONFIGURATION)
    elif isinstance(exc, PermissionError):
        kind = FailureKind.ACCESS_DENIED
    elif isinstance(exc, TimeoutError):
        kind = FailureKind.TIMEOUT
    elif isinstance(exc, (urllib.error.URLError, OSError)):
        kind = FailureKind.NETWORK
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        kind = FailureKind.SCHEMA
    else:
        kind = FailureKind.UNKNOWN
    return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                         failures=(FetchFailure(kind, str(exc), http_status=status),))


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """Params: query/search_term, location, country (ISO), category, radius_km,
    page_size/result_limit. Cursor is a request-bound JSON offset, never a row count
    after assessment. No category or city is silently imposed.
    """
    try:
        p = dict(task.target.request_params)
        if context and context.runtime.get("authorized_sources", {}).get("amazon") is False:
            raise PermissionError("Amazon access denied by runtime policy")
        query = p.get("query", p.get("search_term", p.get("base_query", "")))
        if isinstance(query, dict):
            query = query.get("query", "")
        if not isinstance(query, str) or not query.strip():
            return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                                 failures=(FetchFailure(FailureKind.CONFIGURATION, "Amazon requires a role probe"),))
        limit = int(p.get("page_size", p.get("result_limit", 100)))
        if not 1 <= limit <= 100:
            raise ValueError("Amazon page_size must be 1..100")
        key = task.target.request_key()
        state = json.loads(cursor) if cursor is not None else {"offset": 0, "request": key}
        offset = int(state["offset"])
        if state["request"] != key or offset < 0:
            raise ValueError("Stale or invalid Amazon cursor")
        params = {"base_query": _amazon_query(query), "result_limit": limit, "offset": offset}
        if p.get("location"):
            params["loc_query"] = p["location"]
            if "radius_km" in p:
                params["radius"] = f"{int(p['radius_km'])}km"
        if p.get("country"):
            from .countries import COUNTRY_CODES
            country = str(p["country"]).upper()
            if country not in COUNTRY_CODES:
                raise ValueError("Amazon country must be ISO alpha-2")
            params["country[]"] = country
        if p.get("category"):
            params["category[]"] = p["category"]
        data = _fetch_page(params)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("Amazon response must contain jobs[] and hits")
        total = data.get("hits")
        if type(total) is not int or total < 0:
            raise ValueError("Amazon hits is not a nonnegative integer")
        rows = data["jobs"]
        if not rows and offset < total:
            raise ValueError("Amazon returned an empty page before hits was exhausted")
        records = []
        for job in rows:
            if not isinstance(job, dict) or not job.get("title") or not job.get("job_path"):
                raise ValueError("Amazon job missing title/job_path")
            url = urllib.parse.urljoin("https://www.amazon.jobs", job["job_path"])
            locations = [job.get(k) for k in ("city", "state", "country") if job.get(k)]
            description = "\n\n".join(_strip_html(job[k]) for k in
                ("description", "basic_qualifications", "preferred_qualifications") if job.get(k))
            records.append({
                "title": job["title"], "url": url, "application_url": url, "company": "Amazon",
                "location": job.get("location") or ", ".join(locations) or None,
                "description": description or None, "full_description": description or None,
                "posting_id": str(job["id"]) if job.get("id") is not None else None,
                "office_locations": [job.get("location") or ", ".join(locations)] if locations or job.get("location") else [],
                "office_countries": [job["country"]] if job.get("country") else [],
                "work_mode": job.get("workplaceType") or job.get("remote_type"),
                "employment_type": job.get("job_type"), "posted_at": job.get("posted_date"),
                "identity": {"provider": "amazon", "tenant": task.target.scope,
                             "posting_id": str(job["id"]) if job.get("id") is not None else None,
                             "listing_url": url}, "raw_record": dict(job),
            })
        exhausted = offset + len(rows) >= total
        return DiscoveryPage(task_id=task.task_id or "", records=tuple(records), exhausted=exhausted,
            next_cursor=None if exhausted else json.dumps({"request": key, "offset": offset + len(rows)}),
            coverage=Coverage.EMPTY if not rows else Coverage.COMPLETE,
            request_metadata={"params": params, "total": total, "contract": "observed amazon.jobs search.json",
                              "cycle_id": getattr(context, "cycle_id", None)})
    except Exception as exc:
        return _failure(task, exc)

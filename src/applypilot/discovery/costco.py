"""Bounded Costco native probes using the observed careers /api/jobs contract.

This website interface is not a verified stable public API. Office geography is
output evidence, not permission for an applicant to work there or remotely.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask

SEARCH_URL = "https://careers.costco.com/api/jobs"
_HEADERS = {"User-Agent": "ApplyPilot/1.0 (job-discovery)", "Accept": "application/json"}


def _strip_html(html: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def _fetch_page(params: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(f"{SEARCH_URL}?{urllib.parse.urlencode(params, doseq=True)}", headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """One probe/page. Params: query/search_term, explicit US location (optional),
    country=US, page_size/limit <=100. The planner rotates probes, not markets.
    """
    try:
        p = dict(task.target.request_params)
        if context and context.runtime.get("authorized_sources", {}).get("costco") is False:
            raise PermissionError("Costco access denied by runtime policy")
        if str(p.get("country", "US")).upper() != "US":
            return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                failures=(FetchFailure(FailureKind.CONFIGURATION, "Costco requires a US-native probe; no target substitution"),))
        query = p.get("query", p.get("search_term", p.get("keyword", "")))
        if isinstance(query, dict):
            query = query.get("query", "")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Costco requires a nonempty bounded role probe")
        limit = int(p.get("page_size", p.get("limit", 25)))
        if not 1 <= limit <= 100:
            raise ValueError("Costco limit must be 1..100")
        key = task.target.request_key()
        state = json.loads(cursor) if cursor is not None else {"request": key, "offset": 0}
        offset = int(state["offset"])
        if state["request"] != key or offset < 0:
            raise ValueError("Stale or invalid Costco cursor")
        params = {"keyword": query, "limit": limit, "offset": offset}
        if p.get("location"):
            params["location"] = p["location"]
        data = _fetch_page(params)
        if not isinstance(data, dict):
            raise ValueError("Costco response is not an object")
        rows = data.get("jobs", data.get("results"))
        total = data.get("totalCount", data.get("total"))
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise ValueError("Costco response requires jobs/results[] and a nonnegative total")
        if not rows and offset < total:
            raise ValueError("Costco empty page before reported total")
        records = []
        for wrapper in rows:
            job = wrapper.get("data", wrapper) if isinstance(wrapper, dict) else None
            if not isinstance(job, dict):
                raise ValueError("Costco result is not an object")
            title = job.get("title") or job.get("job_title")
            req_id = job.get("req_id") or job.get("requisition_id")
            url = job.get("url") or job.get("apply_url") or job.get("applyUrl")
            if not url and req_id:
                url = f"https://careers.costco.com/job/{urllib.parse.quote(str(req_id), safe='')}"
            if not title or not url:
                raise ValueError("Costco result missing title or posting URL/req_id")
            url = urllib.parse.urljoin("https://careers.costco.com", url)
            location = job.get("full_location") or ", ".join(
                str(job[k]) for k in ("city", "state", "country") if job.get(k)) or None
            description = _strip_html(job.get("description") or job.get("job_description") or "")
            records.append({
                "title": title, "url": url, "application_url": urllib.parse.urljoin(url,
                    job.get("apply_url") or job.get("applyUrl") or url), "company": "Costco",
                "location": location, "office_locations": [location] if location else [],
                "office_countries": [job["country"]] if job.get("country") else [],
                "description": description or None, "full_description": description or None,
                "posting_id": str(job["id"]) if job.get("id") is not None else None,
                "requisition_id": str(req_id) if req_id is not None else None,
                "work_mode": job.get("workplaceType") or job.get("remote_type"),
                "salary": job.get("salary"), "employment_type": job.get("job_type"),
                "identity": {"provider": "costco", "tenant": task.target.scope,
                    "posting_id": str(job["id"]) if job.get("id") is not None else None,
                    "requisition_id": str(req_id) if req_id is not None else None, "listing_url": url},
                "raw_record": dict(wrapper),
            })
        exhausted = offset + len(rows) >= total
        return DiscoveryPage(task_id=task.task_id or "", records=tuple(records), exhausted=exhausted,
            next_cursor=None if exhausted else json.dumps({"request": key, "offset": offset + len(rows)}),
            coverage=Coverage.EMPTY if not rows else Coverage.COMPLETE,
            request_metadata={"params": params, "total": total, "contract": "observed careers.costco.com/api/jobs",
                              "cycle_id": getattr(context, "cycle_id", None)})
    except Exception as exc:
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
        else:
            kind = FailureKind.SCHEMA
        return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                             failures=(FetchFailure(kind, str(exc), http_status=status),))

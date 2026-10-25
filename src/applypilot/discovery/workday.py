"""Workday CXS employer-site discovery (observed, undocumented contract).

Enumeration requires an explicit verified tenant contract. Otherwise each task
is a native role probe. Search pages never trigger unbudgeted detail requests.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask

def strip_html(html: str) -> str:
    from bs4 import BeautifulSoup
    return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)


def workday_search(employer: dict, search_text: str, limit: int = 20, offset: int = 0,
                   applied_facets: dict | None = None) -> dict:
    url = f"{employer['base_url'].rstrip('/')}/wday/cxs/{employer['tenant']}/{employer['site_id']}/jobs"
    payload = {"appliedFacets": applied_facets or {}, "limit": limit, "offset": offset, "searchText": search_text}
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "User-Agent": "ApplyPilot/1.0 (job-discovery)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """Params accept employer mapping or base_url/url, tenant, site_id, name;
    query/search_term/searchText, appliedFacets, limit <=20. target.scope is the
    employer/tenant identity. Empty query needs enumeration_verified=True.
    Location/country text is not sent as a fabricated CXS facet ID.
    """
    phase = FailureKind.CONFIGURATION
    try:
        p = dict(task.target.request_params)
        if context and context.runtime.get("authorized_sources", {}).get("workday") is False:
            raise PermissionError("Workday access denied by runtime policy")
        employer = dict(p.get("employer") or {})
        for name in ("base_url", "tenant", "site_id", "name", "enumeration_verified"):
            if name in p:
                employer[name] = p[name]
        if not employer.get("base_url") and p.get("url"):
            parts = urllib.parse.urlsplit(p["url"])
            employer["base_url"] = f"{parts.scheme}://{parts.netloc}"
            path = parts.path.strip("/").split("/")
            if path and path[0] in ("en-US", "en-GB"):
                path = path[1:]
            if path and path[0]:
                employer.setdefault("site_id", path[0])
        employer.setdefault("tenant", task.target.scope)
        if not all(isinstance(employer.get(k), str) and employer[k] for k in ("base_url", "tenant", "site_id")):
            raise ValueError("Workday requires base_url, tenant and site_id employer configuration")
        host = urllib.parse.urlsplit(employer["base_url"])
        if host.scheme != "https" or not host.hostname or not host.hostname.endswith(".myworkdayjobs.com"):
            raise ValueError("Workday base_url must be a verified HTTPS myworkdayjobs.com tenant")
        if any("/" in employer[k] or "?" in employer[k] for k in ("tenant", "site_id")):
            raise ValueError("Invalid Workday tenant/site_id")
        query = p.get("query", p.get("search_term", p.get("searchText", "")))
        if isinstance(query, dict):
            query = query.get("query", "")
        if not isinstance(query, str):
            raise ValueError("Workday query must be text")
        if not query.strip() and employer.get("enumeration_verified") is not True:
            raise ValueError("Empty-query enumeration not verified for this tenant; plan a nonempty rotating probe")
        facets = p.get("appliedFacets", p.get("applied_facets", {}))
        if not isinstance(facets, dict) or any(not isinstance(v, (list, tuple)) for v in facets.values()):
            raise ValueError("Workday appliedFacets must map observed facet names to ID lists")
        if (p.get("location") or p.get("country")) and not facets:
            raise ValueError("Workday location/country needs observed native appliedFacets, not free-text targeting")
        limit = int(p.get("limit", p.get("page_size", 20)))
        if not 1 <= limit <= 20:
            raise ValueError("Workday limit must be 1..20")
        key = task.target.request_key()
        state = json.loads(cursor) if cursor is not None else {"request": key, "offset": 0}
        offset = int(state["offset"])
        if state["request"] != key or offset < 0:
            raise ValueError("Stale or invalid Workday cursor")
        phase = FailureKind.SCHEMA
        data = workday_search(employer, query, limit=limit, offset=offset, applied_facets=facets)
        if not isinstance(data, dict) or not isinstance(data.get("jobPostings"), list):
            raise ValueError("CXS response missing jobPostings[]")
        total = data.get("total")
        if type(total) is not int or total < 0:
            raise ValueError("CXS response missing nonnegative total")
        if "total" in state and state["total"] != total:
            raise ValueError("Workday inventory changed during pagination; restart this request")
        rows = data["jobPostings"]
        if not rows and offset < total:
            raise ValueError("CXS empty page before reported total")
        records = []
        for job in rows:
            if not isinstance(job, dict) or not job.get("title") or not job.get("externalPath"):
                raise ValueError("CXS posting missing title/externalPath")
            path = job["externalPath"]
            if not isinstance(path, str) or not path.startswith("/job/"):
                raise ValueError("CXS externalPath is not a job path")
            url = f"{employer['base_url'].rstrip('/')}/{employer['site_id']}{path}"
            location = job.get("locationsText")
            description = strip_html(job.get("jobDescription") or "") or None
            records.append({
                "title": job["title"], "url": url, "application_url": job.get("externalUrl") or url,
                "company": employer.get("name"), "location": location, "description": description,
                "office_locations": [location] if location else [],
                "work_mode": job.get("remoteType"), "schedule": job.get("timeType"),
                "requisition_id": job.get("jobReqId"), "posting_id": path,
                "posted_at": job.get("startDate"), "posted_text": job.get("postedOn"),
                "external_path": path, "employer_key": task.target.scope,
                "employer_name": employer.get("name"),
                "identity": {"provider": "workday", "tenant": task.target.scope,
                    "api_region": host.hostname, "posting_id": path, "requisition_id": job.get("jobReqId"),
                    "listing_url": url}, "raw_record": dict(job),
            })
        exhausted = offset + len(rows) >= total
        return DiscoveryPage(task_id=task.task_id or "", records=tuple(records), exhausted=exhausted,
            next_cursor=None if exhausted else json.dumps({"request": key, "offset": offset + len(rows), "total": total}),
            coverage=Coverage.EMPTY if not rows else Coverage.COMPLETE,
            request_metadata={"total": total, "offset": offset, "limit": limit, "searchText": query,
                "appliedFacets": facets, "contract": "observed Workday CXS",
                "enumeration_verified": employer.get("enumeration_verified") is True,
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
            kind = phase
        return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                             failures=(FetchFailure(kind, str(exc), http_status=status),))

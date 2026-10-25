"""Deterministic, zero-credit FreeHire discovery and route resolution."""

from __future__ import annotations

from contextlib import closing, nullcontext
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta

from applypilot.apply.routes import (
    normalize_public_url,
)
from applypilot.discovery.greenhouse import _strip_html
from applypilot.discovery.url_normalize import canonicalize_application_url

log = logging.getLogger(__name__)

_DEFAULT_API_BASE = "https://freehire.me/api/v1"
_ALLOWED_GET_PATHS = {
    "agent/jobs/search",
    "jobs/facets",
    "insights/salary",
    "insights/skills",
    "me/screening-answers",
    "me/autofill-profile",
}
_ALLOWED_GET_SUFFIXES = {"", "similar", "copies", "apply-form", "match"}
_ALLOWED_GET_TOP_LEVEL = {"companies"}  # top-level resources (not jobs/*)
_HEADERS = {
    "User-Agent": "ApplyPilot/0.2 (zero-credit discovery)",
    "Accept": "application/json",
}


def _api_base() -> str:
    from applypilot.integrations.local_config import freehire_credentials
    configured = freehire_credentials()["api_url"].strip().rstrip("/")
    if not configured:
        return _DEFAULT_API_BASE
    for suffix in ("/agent/jobs/search", "/jobs"):
        if configured.endswith(suffix):
            configured = configured[: -len(suffix)]
    return configured if configured.endswith("/api/v1") else f"{configured}/api/v1"


def _credential_token() -> str:
    from applypilot.integrations.local_config import freehire_credentials
    return freehire_credentials()["token"].strip()


def _headers(auth: bool = False) -> dict[str, str]:
    headers = dict(_HEADERS)
    if auth and (token := _credential_token()):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _path_allowed(path: str) -> bool:
    clean = path.strip("/")
    if clean in _ALLOWED_GET_PATHS:
        return True
    parts = clean.split("/")
    # Top-level resources like companies and companies/{slug}.
    if parts[0] in _ALLOWED_GET_TOP_LEVEL and len(parts) in {1, 2}:
        return True
    return len(parts) in {2, 3} and parts[0] == "jobs" and (
        len(parts) == 2 or parts[2] in _ALLOWED_GET_SUFFIXES
    )


def _fetch_json_path(
    path: str,
    params: dict | None = None,
    *,
    max_retries: int = 3,
    timeout: float = 30.0,
    auth: bool = False,
) -> dict:
    """Perform one allowlisted zero-credit request; Temporal owns retry timing."""
    clean = path.strip('/')
    if not _path_allowed(clean):
        raise ValueError(f'FreeHire endpoint is not zero-credit allowlisted: {clean}')
    query = urllib.parse.urlencode(params or {}, doseq=True)
    url = f'{_api_base()}/{clean}' + (f'?{query}' if query else '')
    request = urllib.request.Request(url, headers=_headers(auth=auth))
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode('utf-8'))


def _fetch_json(params: dict, max_retries: int = 3, timeout: float = 30.0) -> dict:
    """Backward-compatible search wrapper."""
    return _fetch_json_path(
        "agent/jobs/search",
        params,
        max_retries=max_retries,
        timeout=timeout,
    )


def _direct_url(value: object) -> str | None:
    url = normalize_public_url(value)
    if not url:
        return None
    parsed = urllib.parse.urlsplit(url)
    query = [
        (key, val)
        for key, val in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "utm_source"
    ]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _company_name(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        name = value.get("name") or value.get("company_name")
        return str(name).strip() if name else None
    return None


def _salary(raw: dict) -> str | None:
    if raw.get("salary"):
        return str(raw["salary"])
    enrichment = raw.get("enrichment") or {}
    low, high = enrichment.get("salary_min"), enrichment.get("salary_max")
    if low is None and high is None:
        return None
    currency = enrichment.get("salary_currency") or ""
    period = enrichment.get("salary_period") or ""
    amount = f"{low}-{high}" if low is not None and high is not None else str(low or high)
    return " ".join(part for part in (currency, amount, f"per {period}" if period else "") if part)


def _detect_ats(url: str, source: object) -> str | None:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    domains = {
        "myworkdayjobs.com": "workday",
        "greenhouse.io": "greenhouse",
        "lever.co": "lever",
        "ashbyhq.com": "ashby",
        "icims.com": "icims",
        "oraclecloud.com": "oracle",
        "smartrecruiters.com": "smartrecruiters",
    }
    for domain, name in domains.items():
        if domain in host:
            return name
    source_name = str(source).strip().lower() if source else ""
    return source_name if source_name in set(domains.values()) | {
        "bamboohr", "dayforce", "jobvite", "taleo",
    } else None


def normalize_freehire_job(raw: dict) -> dict | None:
    title = str(raw.get("title") or raw.get("job_title") or "").strip()
    apply_url = _direct_url(
        raw.get("application_url") or raw.get("apply_url") or raw.get("url") or raw.get("job_url")
    )
    company = _company_name(
        raw.get("company") or raw.get("company_name") or raw.get("employer_name")
    )
    if not title or not apply_url or not company:
        return None
    apply_url = canonicalize_application_url(apply_url)
    description = _strip_html(str(
        raw.get("description") or raw.get("description_text") or raw.get("full_description") or ""
    ))
    source = raw.get("source")
    freehire_id = raw.get("public_slug") or raw.get("slug") or raw.get("id") or raw.get("external_id")
    metadata = {
        "source": "freehire",
        "freehire_id": freehire_id,
        "ats": _detect_ats(apply_url, source),
        "original_source": raw.get("url"),
        "lane": raw.get("_discovery_lane"),
        "reality": raw.get("reality"),
    }
    return {
        "url": apply_url,
        "application_url": apply_url,
        "title": title,
        "description": description or None,
        "full_description": description or None,
        "location": raw.get("location") or raw.get("location_name") or None,
        "salary": _salary(raw),
        "employer_name": company,
        "posted_at": raw.get("posted_at") or raw.get("published_at") or None,
        "site": "freehire",
        "strategy": "freehire",
        "metadata": metadata,
    }


def execute_task(task, context, cursor=None):
    """Fetch exactly one planned public page; no sync, route lookup or DB writes."""
    from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure
    from .transport import diagnostic_url, page_metadata, task_params

    if task.target and task.target.kind == "company_inventory":
        from .freehire_registry import execute_inventory_task
        return execute_inventory_task(task, context, cursor)

    metadata = page_metadata(task,context,cursor=cursor,method='GET',response_type='json',
                             recovery_strategy='scheduled_http_retry')
    try:
        params = task_params(task, ("freehire",))
        if "query" in params:
            if "q" in params:
                raise ValueError("FreeHire request cannot contain both query and q")
            params["q"] = params.pop("query")
        allowed = {"q", "limit", "countries", "regions", "cities", "work_mode",
                   "employment_type", "salary_min", "salary_currency", "salary_period",
                   "posted_within_days", "visa_sponsorship", "requires_clearance"}
        if set(params) - allowed:
            raise ValueError(f"Unsupported FreeHire planned filters: {sorted(set(params) - allowed)}")
        limit = params.get("limit", 100)
        if type(limit) is not int or not 0 <= limit <= 100:
            raise ValueError("FreeHire page limit must be an integer in [0,100]")
        if limit == 0:
            return DiscoveryPage(task.task_id or "", exhausted=True, request_metadata=metadata)
        checkpoint = json.loads(cursor) if cursor is not None else {}
        request_key = task.target.request_key()
        if checkpoint and checkpoint.get("request_key") != request_key:
            raise ValueError("FreeHire cursor belongs to another request")
        offset = checkpoint.get("offset", 0)
        if type(offset) is not int or offset < 0 or offset >= 10000:
            raise ValueError("FreeHire offset must be in [0,10000)")
        params["limit"] = min(limit, 10000 - offset)
        params["offset"] = offset
        for key in ("countries", "regions", "cities"):
            if isinstance(params.get(key), (tuple, list)):
                params[key] = ",".join(params[key])
        metadata.update(url=diagnostic_url(f'{_api_base()}/agent/jobs/search?'+urllib.parse.urlencode(params,doseq=True)),
                        offset=offset,limit=params['limit'])
        payload = _fetch_json(params, max_retries=1)
        if not isinstance(payload, dict):
            raise ValueError("FreeHire response must be a mapping")
        meta = payload.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError("FreeHire metadata must be a mapping")
        ignored = set(meta.get("ignored_params") or ()) & set(params)
        if ignored:
            raise ValueError(f"FreeHire ignored planned filters: {sorted(ignored)}")
        rows = next((payload[key] for key in ("data", "results", "jobs") if key in payload), None)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("FreeHire response must contain a job-object list")
        records = []
        for raw in rows:
            record = normalize_freehire_job(raw) or {
                "title": raw.get("title"), "description": raw.get("description"),
            }
            # Posting identity is separate from a shared application endpoint.
            record["url"] = raw.get("listing_url") or raw.get("job_url") or raw.get("url")
            record.update(raw_record=raw,
                          source_posting_id=raw.get("public_slug") or raw.get("id") or raw.get("slug") or raw.get("external_id"))
            records.append(record)
        next_offset = offset + len(rows)
        total = meta.get("total", payload.get("total"))
        exhausted = len(rows) < params["limit"] or (type(total) is int and next_offset >= total)
        capped = next_offset >= 10000 and not exhausted
        next_cursor = None if exhausted else json.dumps({
            "request_key": request_key, "offset": next_offset,
        }, sort_keys=True)
        return DiscoveryPage(
            task.task_id or "", records=tuple(records), next_cursor=next_cursor,
            exhausted=exhausted, complete=not capped, capped=capped,
            coverage=Coverage.CAPPED if capped else Coverage.COMPLETE,
            request_metadata={**metadata, "request_key": request_key, "offset": offset,
                              "limit": params["limit"], "total": total},
        )
    except Exception as exc:
        from applypilot.operations.runtime import sanitize
        status = getattr(exc, "code", None) or getattr(exc.__cause__, "code", None)
        cause = exc.__cause__ or (exc.reason if isinstance(getattr(exc,'reason',None),Exception) else exc)
        kind = (FailureKind.AUTH if status == 401 else
                FailureKind.ACCESS_DENIED if status == 403 else
                FailureKind.RATE_LIMIT if status == 429 else
                FailureKind.TIMEOUT if isinstance(cause, TimeoutError) else
                FailureKind.NETWORK if isinstance(exc, OSError) else FailureKind.CONFIGURATION)
        retry_after = max(1,int(context.runtime['retry_base_seconds']))
        metadata.update(exception_type=type(cause).__name__)
        return DiscoveryPage(task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                             request_metadata=metadata,
                             failures=(FetchFailure(kind, sanitize(str(exc)), http_status=status,retry_after=retry_after),))


def fetch_company_context(company_slug: str) -> dict | None:
    """Fetch company context from FreeHire for cover letter enrichment.

    Returns a dict with tagline, company_size, industries, year_founded, etc.
    Returns None on failure or if no data found. Fail-open — never blocks the pipeline.
    """
    if not company_slug:
        return None
    try:
        resp = _fetch_json_path(f"companies/{company_slug}", auth=False)
        if resp.get("status") == "ok" and resp.get("data"):
            data = resp["data"]
            return {
                "tagline": data.get("tagline", ""),
                "company_size": data.get("company_size", ""),
                "industries": data.get("industries") or [],
                "year_founded": data.get("year_founded"),
                "hq_country": data.get("hq_country", ""),
                "maturity": data.get("maturity", ""),
            }
    except Exception as e:
        log.debug("FreeHire company context fetch failed for %s: %s", company_slug, e)
    return None




# ── Job Detail Enrichment ─────────────────────────────────────────────────


def fetch_job_detail(slug: str) -> dict | None:
    """Fetch full structured job detail from FreeHire API.

    Returns a dict with keys: full_description, salary, location, company,
    posted_at, skills, applicant_count, etc. Returns None on failure.
    Fail-open — never blocks the pipeline.
    """
    if not slug:
        return None
    try:
        resp = _fetch_json_path(f"jobs/{urllib.parse.quote(str(slug), safe='')}", auth=False)
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, dict) and resp.get("status", "ok") in {"ok", "success"}:
            description = _strip_html(str(
                data.get("description") or data.get("description_text") or data.get("full_description") or ""
            ))
            return {
                "full_description": description or None,
                "application_url": _direct_url(data.get("application_url") or data.get("apply_url")),
                "salary": _salary(data),
                "location": data.get("location") or data.get("location_name"),
                "company": _company_name(data.get("company") or data.get("company_name")),
                "posted_at": data.get("posted_at") or data.get("published_at"),
                "skills": data.get("skills") or [],
                "applicant_count": data.get("applicant_count"),
                "seniority": data.get("seniority"),
                "remote": data.get("remote") or data.get("work_mode"),
            }
    except Exception as e:
        log.debug("FreeHire job detail fetch failed for %s: %s", slug, e)
    return None


def enrich_freehire_jobs(conn, *, jobs=None, budget=None, context=None) -> dict:
    """Enrich FreeHire-discovered jobs via API instead of Playwright scraping.

    For jobs with site='freehire' and a freehire_id in metadata, fetch full
    detail from the API. This is faster and more reliable than scraping.

    Returns {"enriched": N, "skipped": M, "errors": [...]}.
    """
    if budget is not None and (type(budget) is not int or budget < 0):
        raise ValueError("budget must be a nonnegative integer")
    if budget == 0:
        return {"enriched": 0, "skipped": 0, "errors": []}

    def database():
        from applypilot import postgres
        from applypilot.config import DB_PATH
        return nullcontext(conn) if conn is not None else closing(postgres.connect(DB_PATH))

    if jobs is None:
        from applypilot.database import get_jobs_by_stage
        with database() as connection:
            jobs = get_jobs_by_stage(connection, "pending_detail", limit=200 if budget is None else budget)
    selected = [job for job in jobs if job.get("site") == "freehire"]
    if budget is not None:
        selected = selected[:budget]
    rows = [(job["url"], job.get("title") or "") for job in selected]

    if not rows:
        return {"enriched": 0, "skipped": 0, "errors": []}

    enriched = 0
    skipped = 0
    errors = []
    now = datetime.now(UTC).isoformat()

    for url, title in rows:
        # Extract freehire_id from metadata stored in state transitions
        with database() as connection:
            meta_row = connection.execute(
                "SELECT metadata FROM job_state_transitions "
                "WHERE job_url = ? AND metadata IS NOT NULL "
                "AND metadata LIKE '%freehire_id%' ORDER BY at DESC LIMIT 1",
                (url,),
            ).fetchone()
            freehire_id = None
            if meta_row:
                try:
                    meta = json.loads(meta_row[0])
                    freehire_id = meta.get("freehire_id")
                except (json.JSONDecodeError, TypeError):
                    pass

            if not freehire_id:
                # Current discovery stores source metadata in lossless observations;
                # later lifecycle transitions need not repeat the provider job ID.
                observation = connection.execute(
                    "SELECT o.raw_json FROM discovery_observations o "
                    "JOIN discovery_postings p ON p.identity_key=o.identity_key "
                    "WHERE p.job_url=? AND p.provider='freehire' AND o.inference IS NULL "
                    "ORDER BY o.source_revision DESC,o.last_seen_at DESC LIMIT 1", (url,)).fetchone()
                if observation:
                    from applypilot.discovery import payloads
                    raw = json.loads(payloads.resolve_json(connection, observation[0]))
                    metadata = raw.get("metadata") or {}
                    provider = raw.get("raw_record") or raw
                    freehire_id = (metadata.get("freehire_id") or raw.get("source_posting_id")
                                   or provider.get("public_slug") or provider.get("slug")
                                   or provider.get("freehire_id") or provider.get("id")
                                   or provider.get("external_id"))

        if not freehire_id:
            skipped += 1
            continue

        detail = fetch_job_detail(freehire_id)
        if not detail or not detail.get("full_description"):
            skipped += 1
            continue

        try:
            from applypilot.database import write_with_retry
            from applypilot.enrichment.detail import _mark_enrich_result

            with database() as connection:
                def _update():
                    connection.execute("UPDATE jobs SET salary=COALESCE(?,salary), "
                                       "location=COALESCE(?,location) WHERE url=?",
                                       (detail.get("salary"), detail.get("location"), url))
                    _mark_enrich_result(
                        connection, url, status="ok", full_description=detail["full_description"],
                        application_url=detail.get("application_url"), error=None,
                        tier=0, retry_count=0, now=now, context=context)

                write_with_retry(connection, _update)
            enriched += 1
        except Exception as e:
            errors.append(f"{title[:40]}: {e}")

    log.info(
        "FreeHire API enrichment: %d enriched, %d skipped, %d error(s)",
        enriched, skipped, len(errors),
    )
    return {"enriched": enriched, "skipped": skipped, "errors": errors}

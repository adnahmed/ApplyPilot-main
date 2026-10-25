"""Reed Jobseeker API: credentialed, bounded native search pages."""
from __future__ import annotations

import base64
import os
from urllib.parse import urlencode, urlparse

from . import transport
from .builtin import (_check_access, _failure_page, _fetch, _finish_page, _integer,
                      _listing_facts, _page_state, _query, _run_source)
from .models import DiscoveryPage, FailureKind, SearchTask
from .normalization import scalar_bool, scalar_number, scalar_text

_REED_API_BASE = "https://www.reed.co.uk/api/1.0"
_MAX_OFFSET = -1  # Keep the API page size at 100, not an arbitrary total-result cap.


def _get_reed_auth() -> tuple[str, str] | None:
    api_key = os.environ.get("REED_API_KEY", "").strip()
    return (api_key, "") if api_key else None


def _fetch_reed(path, params=None, timeout=20.0, *, context):
    _check_access(context, "reed")
    auth = _get_reed_auth()
    if not auth:
        raise transport.TaskDeferred("Reed requires REED_API_KEY", FailureKind.AUTH)
    # Credentials belong on the request, never in task identity or page metadata.
    credentials = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode("ascii")
    url = f"{_REED_API_BASE}/{path}" + ("?" + urlencode(params) if params else "")
    return _fetch(url, context=context, provider="reed", json_response=True, timeout=timeout,
                  headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"})


def _search_reed(query, location="", results_wanted=100, page=1, *, context, skip=None, filters=None):
    take = _integer(results_wanted, "resultsToTake", 1, 100)
    _integer(page, "page", 1, -1)
    offset = _integer(skip if skip is not None else (page - 1) * take, "resultsToSkip", 0, _MAX_OFFSET)
    params = {"keywords": _query(query), "resultsToTake": take, "resultsToSkip": offset}
    if location:
        params["locationName"] = location
    params.update(filters or {})
    return _fetch_reed("search", params, context=context)


def _get_job_details(job_id, *, context):
    """Explicit separately budgeted lookup; never called by discovery's page loop."""
    _integer(job_id, "job_id", 1, 2**63 - 1)
    data = _fetch_reed(f"jobs/{job_id}", context=context)
    if not isinstance(data, dict) or scalar_text(data.get("jobId")) != str(job_id):
        raise ValueError("Reed details response is not the requested posting")
    return data


def _reed_job_type_map(job_type: str) -> str | None:
    # Temporary is not part-time; permanent is not a full-time schedule.
    return {"permanent": "permanent", "contract": "contract", "temporary": "temp",
            "apprenticeship": "internship"}.get((scalar_text(job_type) or "").lower())


def _format_salary(item: dict) -> str | None:
    low, high = scalar_number(item.get("minimumSalary")), scalar_number(item.get("maximumSalary"))
    currency = scalar_text(item.get("currency"))
    prefix = currency + " " if currency else ""
    if low is not None and high is not None:
        return f"{prefix}{low:g}-{high:g}"
    if low is not None:
        return f"{prefix}{low:g}+"
    if high is not None:
        return f"{prefix}up to {high:g}"
    return None


def _parse_reed_job(item):
    if not isinstance(item, dict):
        raise ValueError("Reed result must be an object")
    posting_id = scalar_text(item.get("jobId"))
    url, title = scalar_text(item.get("jobUrl")), scalar_text(item.get("jobTitle"))
    if not posting_id or not title or not url or urlparse(url).scheme not in {"http", "https"}:
        raise ValueError("Reed result missing posting ID, title or HTTP(S) URL")
    for key in ("minimumSalary", "maximumSalary"):
        value = item.get(key)
        if value is not None and value != "" and scalar_number(value) is None:
            raise ValueError(f"Reed {key} must be numeric or null")
    low, high = scalar_number(item.get("minimumSalary")), scalar_number(item.get("maximumSalary"))
    if (low is not None and low < 0) or (high is not None and high < 0) or (
            low is not None and high is not None and low > high):
        raise ValueError("Reed salary bounds are invalid")
    full, part = scalar_bool(item.get("fullTime")), scalar_bool(item.get("partTime"))
    schedule = "full_time" if full is True and part is not True else "part_time" if part is True and full is not True else None
    job_type = _reed_job_type_map(item.get("jobType"))
    return _listing_facts({
        "url": url, "posting_id": posting_id, "title": title,
        "company": scalar_text(item.get("employerName")), "location": scalar_text(item.get("locationName")),
        "description": scalar_text(item.get("jobDescription")), "salary": _format_salary(item),
        "salary_min": low, "salary_max": high, "salary_currency": scalar_text(item.get("currency")),
        "salary_period": scalar_text(item.get("salaryType")), "schedule": schedule,
        "contract_type": job_type, "engagement": "contractor" if job_type == "contract" else None,
        "work_mode": scalar_text(item.get("workplaceType")),
        "posted_at": scalar_text(item.get("date")), "raw_record": dict(item),
    }, "reed")


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    try:
        params = transport.task_params(task, ("reed",))
        _check_access(context, "reed")
        boolean_filters = {"permanent", "contract", "temp", "partTime", "fullTime"}
        numeric_filters = {"minimumSalary", "maximumSalary", "distanceFromLocation"}
        if set(params) - {"keywords", "locationName", "resultsToTake", "resultsToSkip"} - boolean_filters - numeric_filters:
            raise ValueError("Unsupported Reed request parameters")
        query = _query(params.get("keywords"))
        location = params.get("locationName", "")
        if not isinstance(location, str):
            raise ValueError("Reed locationName must be a string")
        take = _integer(params.get("resultsToTake", 100), "resultsToTake", 1, 100)
        start = _integer(params.get("resultsToSkip", 0), "resultsToSkip", 0, _MAX_OFFSET)
        offset, previous = _page_state(task, cursor, start, _MAX_OFFSET)
        if (offset - start) % take:
            raise ValueError("Reed cursor is not aligned to the native page size")
        filters = {}
        for key in boolean_filters.intersection(params):
            if type(params[key]) is not bool:
                raise ValueError(f"Reed {key} must be a boolean")
            filters[key] = str(params[key]).lower()
        for key in numeric_filters.intersection(params):
            value = scalar_number(params[key])
            if value is None or value < 0:
                raise ValueError(f"Reed {key} must be a nonnegative number")
            filters[key] = value
        if filters.get("minimumSalary", 0) > filters.get("maximumSalary", float("inf")):
            raise ValueError("Reed minimumSalary exceeds maximumSalary")
        data = _search_reed(query, location, take, context=context, skip=offset, filters=filters)
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("Reed response requires results[]")
        rows = data["results"]
        total = data.get("totalResults")
        if total is not None:
            _integer(total, "totalResults", 0, 2**63 - 1)
        if len(rows) > take or (total is not None and not rows and offset < total):
            raise ValueError("Reed results contradict native pagination")
        records, invalid = [], 0
        for item in rows:
            try:
                records.append(_parse_reed_job(item))
            except (ValueError, TypeError):
                invalid += 1
        if invalid:
            return _failure_page(task, context, ValueError(f"{invalid} malformed Reed results"), cursor,
                                 records, invalid_records=invalid)
        exhausted = offset + len(rows) >= total if total is not None else len(rows) < take
        if not exhausted and len(rows) != take:
            raise ValueError("Short Reed page before declared total")
        return _finish_page(task, context, records, offset, None if exhausted else offset + take,
            previous, _MAX_OFFSET, total=total, native_country="GB", contract="Reed Jobseeker API")
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor)


def run_reed_discovery(workers: int = 1, *, context=None) -> dict:
    return _run_source("reed", workers, context)

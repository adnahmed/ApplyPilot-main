"""SEEK AU/NZ observed website search, not the approved integration API."""
from __future__ import annotations

from urllib.parse import urlencode

from . import transport
from .builtin import (_check_access, _failure_page, _fetch, _finish_page, _integer,
                      _listing_facts, _page_state, _query, _run_source)
from .models import DiscoveryPage, SearchTask
from .normalization import country_code, scalar_number, scalar_text

_SEEK_CONFIG = {
    "AU": {"site_key": "AU-Main", "locale": "en-AU", "base_url": "https://www.seek.com.au"},
    "NZ": {"site_key": "NZ-Main", "locale": "en-NZ", "base_url": "https://www.seek.co.nz"},
}
_MAX_PAGES = -1  # Work volume is uncapped; requests are still one native page.


def _detect_country(location: str) -> str | None:
    country = country_code(location)
    return country if country in _SEEK_CONFIG else None


def _fetch_seek(query, country, location="", page=1, *, context, page_size=20):
    if country not in _SEEK_CONFIG:
        raise ValueError("SEEK requires an explicit AU or NZ country, not an inferred default")
    config = _SEEK_CONFIG[country]
    params = {"siteKey": config["site_key"], "sourcesystem": "houston", "where": location,
              "keywords": _query(query), "page": page, "pageSize": page_size,
              "seekSelectAllPages": "false", "locale": config["locale"]}
    url = f"{config['base_url']}/api/jobsearch/v5/search?{urlencode(params)}"
    return _fetch(url, context=context, provider="seek", json_response=True,
                  headers={"Accept": "application/json", "Referer": config["base_url"] + "/"})


def _parse_seek_job(item: dict, country: str) -> dict:
    if not isinstance(item, dict):
        raise ValueError("SEEK job must be an object")
    posting_id, title = scalar_text(item.get("id")), scalar_text(item.get("title"))
    if not posting_id or not posting_id.isdigit() or not title:
        raise ValueError("SEEK job requires a numeric ID and title")
    advertiser = item.get("advertiser") or {}
    locations = item.get("locations") or []
    salary_range = item.get("salaryRange") or {}
    if not isinstance(advertiser, dict) or not isinstance(salary_range, dict) or not isinstance(locations, list):
        raise ValueError("Malformed SEEK advertiser, salary range or locations")
    if any(not isinstance(x, dict) for x in locations):
        raise ValueError("Malformed SEEK location")
    labels = [scalar_text(x.get("label")) for x in locations]
    labels = [x for x in labels if x]
    salary = scalar_text(item.get("salary"))
    low, high = scalar_number(salary_range.get("from")), scalar_number(salary_range.get("to"))
    job = _listing_facts({
        "url": f"{_SEEK_CONFIG[country]['base_url']}/job/{posting_id}", "posting_id": posting_id, "title": title,
        "company": scalar_text(advertiser.get("description")) or scalar_text(item.get("owner")),
        "location": labels[0] if labels else scalar_text(item.get("location")),
        "salary": salary, "salary_min": low, "salary_max": high,
        "salary_currency": scalar_text(salary_range.get("currency")),
        "salary_period": scalar_text(salary_range.get("period")),
        "description": scalar_text(item.get("teaser")) or scalar_text(item.get("snippet")),
        "schedule": {"full time": "full_time", "part time": "part_time"}.get(
            (scalar_text(item.get("workType")) or scalar_text(item.get("jobType")) or "").lower()),
        "work_mode": scalar_text(item.get("workplaceType")) or scalar_text(item.get("workArrangement")),
        "employment_type_text": scalar_text(item.get("jobType")),
        "posted_at": scalar_text(item.get("listingDate")), "raw_record": dict(item),
    }, "seek")
    if len(labels) > 1:
        job["office_locations"] = labels
    return job


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    try:
        params = transport.task_params(task, ("seek",))
        _check_access(context, "seek")
        if set(params) - {"query", "country", "location", "page", "page_size"}:
            raise ValueError("Unsupported SEEK request parameters")
        country = params.get("country")
        if country not in _SEEK_CONFIG:
            raise ValueError("SEEK country must be AU or NZ")
        location = params.get("location", "")
        if not isinstance(location, str):
            raise ValueError("SEEK location must be a string")
        start = _integer(params.get("page", 1), "page", 1, _MAX_PAGES)
        size = _integer(params.get("page_size", 20), "page_size", 1, 100)
        page, previous = _page_state(task, cursor, start, _MAX_PAGES)
        data = _fetch_seek(_query(params.get("query")), country, location, page, context=context, page_size=size)
        if not isinstance(data, dict):
            raise ValueError("SEEK response must be an object")
        # The existing parser used jobs; the website also exposes the data envelope.
        rows = data.get("data") if "data" in data else data.get("jobs")
        if not isinstance(rows, list):
            raise ValueError("SEEK response requires data[] or jobs[]")
        total = data.get("totalCount")
        if total is not None:
            _integer(total, "totalCount", 0, 2**63 - 1)
        offset = (page - 1) * size
        if len(rows) > size or (not rows and total is not None and offset < total):
            raise ValueError("SEEK rows contradict native pagination")
        records, invalid = [], 0
        for item in rows:
            try:
                records.append(_parse_seek_job(item, country))
            except (ValueError, TypeError, KeyError):
                invalid += 1
        if invalid:
            return _failure_page(task, context, ValueError(f"{invalid} malformed SEEK results"), cursor,
                                 records, invalid_records=invalid)
        exhausted = offset + len(rows) >= total if total is not None else len(rows) < size
        if not exhausted and len(rows) != size:
            raise ValueError("Short SEEK page before declared total")
        return _finish_page(task, context, records, page, None if exhausted else page + 1,
            previous, _MAX_PAGES, native_country=country, total=total,
            contract="observed SEEK website API; not approved integration API")
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor)


def run_seek_discovery(workers: int = 1, *, context=None) -> dict:
    return _run_source("seek", workers, context)

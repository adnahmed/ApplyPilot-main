"""Bounded python-jobspy 1.1.82 snapshots; publication belongs to the repository.

All entrypoints use lower_request. A library result is not an authoritative board
refresh: JobSpy paginates internally, flattens locations, and can log/absorb errors.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime
from enum import Enum
from importlib.metadata import version

from applypilot.limits import work_limit, page_size, page_indices
from jobspy.model import Country

from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask, SourceTarget
from .normalization import is_missing, scalar_bool, scalar_number, scalar_text

log = logging.getLogger(__name__)
_VERSION = "1.1.82"
_RESULT_CAP = 100
_BOARDS = {"indeed", "linkedin", "glassdoor", "google", "zip_recruiter", "naukri", "bayt", "bdjobs"}
_DEFAULT_BOARDS = ["indeed", "linkedin", "google", "naukri", "bayt", "bdjobs"]


def parse_proxy(proxy_str: str) -> dict:
    """Preserve the proxy utility used by the enrichment entrypoint."""
    parts = proxy_str.split(":")
    if len(parts) not in (2, 4):
        raise ValueError("Expected proxy host:port or host:port:user:pass")
    host, port = parts[:2]
    user, password = parts[2:] if len(parts) == 4 else (None, None)
    playwright = {"server": f"http://{host}:{port}"}
    if user is not None:
        playwright.update(username=user, password=password)
    return {"host": host, "port": port, "user": user, "pass": password,
            "jobspy": f"{user}:{password}@{host}:{port}" if user is not None else f"{host}:{port}",
            "playwright": playwright}


def _clean(value):
    """Lossless JSON-shaped raw values, except explicit missing/nonfinite scalars."""
    if isinstance(value, Mapping):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_clean(item) for item in value]
    if isinstance(value, Country):
        return value.value[0].split(",")[0]
    if isinstance(value, Enum):
        return _clean(value.value)
    if is_missing(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return _clean(value.model_dump())
    if hasattr(value, "dict"):
        return _clean(value.dict())
    if isinstance(value, str):
        return value  # full original description/whitespace survives
    if isinstance(value, bool) or type(value).__name__ == "bool_":
        return scalar_bool(value)
    number = scalar_number(value)
    if number is not None:
        return int(number) if isinstance(value, int) else number
    return scalar_text(value)


def _country(value: object) -> Country:
    text = scalar_text(value)
    if text:
        text = {"uae": "ae"}.get(text.lower(), text.lower())
        for country in Country:
            aliases = country.value[0].split(",") + [country.value[1].split(":")[-1]]
            if text in aliases and country not in (Country.WORLDWIDE, Country.US_CANADA):
                return country
    raise ValueError(f"Unsupported JobSpy index country: {value!r}; defer, do not substitute")


def _nonnegative(value, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


def _google_time_phrase(hours_old: int) -> str:
    for threshold, phrase in ((24, "since yesterday"), (72, "in the last 3 days"),
                             (168, "in the last week"), (720, "in the last month")):
        if hours_old <= threshold:
            return phrase
    return ""  # Do not silently narrow an older requested window to one month.


def lower_request(params: Mapping, *, scope: str | None = None, runtime: Mapping | None = None) -> tuple[dict, dict]:
    """Lower one board target to the pinned library's actual keyword contract.

    Aliases: board/site/site_name; query/search_term; remote/is_remote;
    country/country_indeed; limit/results_wanted/results_per_site. Multi-board
    values are invalid. Metadata lists omitted constraints, never eligibility.
    """
    runtime = runtime or {}
    defaults = runtime.get("request_defaults", {})
    board = params.get("board", params.get("site", params.get("site_name", scope)))
    if not isinstance(board, str) or board not in _BOARDS:
        raise ValueError("JobSpy target must resolve to exactly one supported board")
    for name in ("board", "site", "site_name"):
        if name in params and params[name] != board:
            raise ValueError("Conflicting JobSpy board selectors")
    for names in (("query", "search_term"), ("remote", "is_remote"),
                  ("limit", "results_wanted", "results_per_site")):
        supplied = [params[name] for name in names if name in params]
        if supplied and any(value != supplied[0] for value in supplied[1:]):
            raise ValueError(f"Conflicting JobSpy aliases: {names}")
    query = scalar_text(params.get("search_term", params.get("query")))
    if not query and not (board == "google" and scalar_text(params.get("google_search_term"))):
        raise ValueError("JobSpy search requires a nonempty query")
    requested = work_limit(params.get("results_wanted", params.get("limit", params.get(
        "results_per_site", defaults.get("results_per_site", -1)))), "results_wanted")
    limit = page_size(requested, _RESULT_CAP)
    kwargs = {"site_name": [board], "results_wanted": limit,
              "description_format": "markdown", "verbose": 0, "enforce_annual_salary": False}
    omitted = {}
    metadata = {"board": board, "contract_version": _VERSION, "requested_results": requested,
                "result_cap": _RESULT_CAP, "omitted_filters": omitted,
                "authoritative_inventory": False}
    allowed = {"board", "site", "site_name", "query", "search_term", "location", "country",
               "country_indeed", "remote", "is_remote", "results_wanted", "results_per_site", "limit",
               "hours_old", "distance", "job_type", "easy_apply", "google_search_term", "offset",
               "linkedin_fetch_description", "linkedin_company_ids", "company_name", "employer_key",
               "proxies", "ca_cert", "user_agent", "description_format", "verbose"}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError(f"Unsupported JobSpy request parameters: {sorted(unknown)}")
    for key in ("proxies", "ca_cert", "user_agent"):
        if key in params:
            kwargs[key] = list(params[key]) if isinstance(params[key], tuple) else params[key]
    country_value = params.get("country_indeed", params.get("country"))
    if board in {"indeed", "glassdoor", "zip_recruiter", "naukri", "bdjobs"}:
        country = _country(country_value)
        if board == "glassdoor" and len(country.value) != 3:
            raise ValueError(f"Glassdoor does not support {country_value!r}")
        native = {"zip_recruiter": {Country.USA, Country.CANADA},
                  "naukri": {Country.INDIA}, "bdjobs": {Country.BANGLADESH}}
        if board in native and country not in native[board]:
            raise ValueError(f"{board} requires an explicit native country, not {country_value!r}")
        kwargs["country_indeed"] = country.value[0].split(",")[0]
    else:
        # This library argument is required even on boards that do not use it.
        # WORLDWIDE prevents the library's default US-only salary inference.
        kwargs["country_indeed"] = "worldwide"
        if country_value is not None:
            omitted["country"] = country_value
    location = scalar_text(params.get("location"))
    hours = params.get("hours_old", defaults.get("hours_old"))
    if hours is not None:
        hours = _nonnegative(hours, "hours_old")
    remote = scalar_bool(params.get("is_remote", params.get("remote")))
    easy = scalar_bool(params.get("easy_apply"))
    for key, value in (("is_remote", params.get("is_remote", params.get("remote"))),
                       ("easy_apply", params.get("easy_apply"))):
        if value is not None and scalar_bool(value) is None:
            raise ValueError(f"{key} must be an explicit boolean")
    job_type = scalar_text(params.get("job_type"))
    if job_type:
        job_type = {"full_time": "fulltime", "part_time": "parttime", "temp": "temporary"}.get(job_type, job_type)
        if job_type not in {"fulltime", "parttime", "contract", "temporary", "internship", "perdiem",
                            "nights", "other", "summer", "volunteer"}:
            raise ValueError(f"Unsupported JobSpy job_type: {job_type}")
    offset = _nonnegative(params.get("offset", 0), "offset")
    if board in {"bayt", "bdjobs", "zip_recruiter"} and offset:
        raise ValueError(f"Resumable offsets are not established for {board} in {_VERSION}")
    if board == "linkedin" and offset % 10:
        raise ValueError("LinkedIn offsets must be multiples of 10 (library rounds down)")
    if board == "glassdoor" and offset:
        raise ValueError("Glassdoor offset rounding is not a stable continuation contract")
    if board in {"linkedin", "naukri"} and offset + limit > 1000:
        raise ValueError(f"{board} offset + result limit must be <= 1000")
    if board == "naukri" and offset % 20:
        raise ValueError("Naukri offsets must be multiples of 20")
    if offset:
        kwargs["offset"] = offset
    if board in {"bayt", "bdjobs"}:
        kwargs["search_term"] = query
        omitted.update({k: v for k, v in (("location", location), ("hours_old", hours),
                       ("is_remote", remote), ("job_type", job_type), ("easy_apply", easy)) if v is not None})
    elif board == "google":
        term = scalar_text(params.get("google_search_term"))
        if not term:
            target = "remote jobs" if remote is True else "jobs"
            if location:
                target += f" near {location}"
            elif country_value and str(country_value).lower() not in {"global", "worldwide"}:
                target += f" in {country_value}"
            term = " ".join(part for part in (query, target, _google_time_phrase(hours) if hours else "") if part)
        kwargs["google_search_term"] = term
        omitted.update({k: v for k, v in (("job_type", job_type), ("easy_apply", easy)) if v is not None})
        metadata["approximate_filters"] = ["google_search_term"]
    else:
        kwargs["search_term"] = query
        if board == "zip_recruiter" and not location:
            # country_indeed is not used by this board's API; the native search
            # must be expressed in its actual location parameter.
            location = "United States" if country == Country.USA else "Canada"
        if location:
            kwargs["location"] = location
        if board in {"indeed", "linkedin", "glassdoor", "zip_recruiter", "naukri"}:
            if hours is not None:
                kwargs["hours_old"] = hours
            if remote is not None:
                kwargs["is_remote"] = remote
            if job_type and board != "naukri":
                kwargs["job_type"] = job_type
            if easy is not None and board != "naukri":
                kwargs["easy_apply"] = easy
            if board in {"indeed", "linkedin", "zip_recruiter"}:
                kwargs["distance"] = _nonnegative(params.get("distance", defaults.get("distance", 50)), "distance")
            if board == "naukri":
                omitted.update({k: v for k, v in (("job_type", job_type), ("easy_apply", easy)) if v is not None})
                metadata["approximate_filters"] = ["hours_old", "is_remote"]
        else:
            omitted.update({k: v for k, v in (("hours_old", hours), ("is_remote", remote),
                           ("job_type", job_type), ("easy_apply", easy)) if v is not None})
        if board == "indeed":
            if job_type and job_type not in {"fulltime", "parttime", "contract", "internship"}:
                raise ValueError(f"Indeed 1.1.82 does not support job_type={job_type!r}")
            # Preserve the existing remote/date workaround. The native composite
            # remote+job_type group also cannot coexist with easy_apply.
            if remote is True or job_type:
                for key in ("hours_old", "easy_apply"):
                    if key in kwargs:
                        omitted[key] = kwargs.pop(key)
            elif easy is True and "hours_old" in kwargs:
                omitted["hours_old"] = kwargs.pop("hours_old")
        if board == "linkedin":
            details = params.get("linkedin_fetch_description", False)
            if scalar_bool(details) is None:
                raise ValueError("linkedin_fetch_description must be an explicit boolean")
            kwargs["linkedin_fetch_description"] = scalar_bool(details)
            metadata["description_requests"] = "separately_metered" if kwargs["linkedin_fetch_description"] else "disabled"
            if "linkedin_company_ids" in params:
                ids = params["linkedin_company_ids"]
                if not isinstance(ids, (tuple, list)) or any(type(item) is not int or item <= 0 for item in ids):
                    raise ValueError("linkedin_company_ids must contain positive integer IDs")
                kwargs["linkedin_company_ids"] = list(ids)
    for key in ("company_name", "employer_key"):
        if key in params:
            omitted[key] = params[key]  # **kwargs silently ignores these in 1.1.82
    return kwargs, metadata


def _failure(exc: Exception) -> FetchFailure:
    """Carry observed failure facts across the isolated worker boundary."""
    from .transport import TaskDeferred

    if isinstance(exc, TaskDeferred):
        return exc.failure
    from applypilot.operations.runtime import sanitize
    return FetchFailure(FailureKind.UNKNOWN, sanitize(str(exc), 1000),
                        http_status=getattr(exc, "http_status", None),
                        retry_after=getattr(exc, "retry_after", None))


def _jobpost_row(job, board: str, country: str) -> dict:
    """Keep JobPost fields before scrape_jobs flattens and drops them.

    Mirror the public DataFrame columns, without throwing away structured
    location/compensation or fields absent from JobSpy's desired_order list.
    """
    from jobspy.util import desired_order, extract_salary

    raw = _clean(job.model_dump())
    row = dict(raw, site=board, company=raw.get("company_name"))
    row["source_location"] = raw.get("location")
    row["source_compensation"] = raw.get("compensation")
    row["job_type"] = ", ".join(item.value[0] for item in job.job_type) if job.job_type else None
    for key in ("emails", "skills"):
        row[key] = ", ".join(raw[key]) if raw.get(key) else None
    row["location"] = job.location.display_location() if job.location else None
    comp = raw.get("compensation")
    if isinstance(comp, dict):
        row.update({key: comp.get(key) for key in ("interval", "min_amount", "max_amount", "currency")})
        row["salary_source"] = "direct_data"
    elif country in {"usa", "us", "united states"}:
        values = extract_salary(raw.get("description"), enforce_annual_salary=False)
        row.update(zip(("interval", "min_amount", "max_amount", "currency"), values))
        row["salary_source"] = "description" if values[1] is not None or values[2] is not None else None
    for key in desired_order:
        row.setdefault(key, None)
    return row


def _records(df, board: str, origins=()) -> tuple[dict, ...]:
    records = []
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else df
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise ValueError("JobSpy records must be mappings")
        raw = _clean(raw)
        record = dict(raw)
        site = scalar_text(raw.get("site")) or board
        record.update(raw_record=raw, provider="jobspy", site=site, strategy="jobspy", origins=list(origins))
        posting_id = scalar_text(raw.get("id"))
        # These library IDs use Python's process-randomized hash(job_url).
        # The stable listing URL, not that synthetic ID, is source identity.
        if site == "bayt" or site == "bdjobs" and posting_id and posting_id.startswith("bdjobs-"):
            posting_id = None
        record["identity"] = {"provider": "jobspy", "tenant": site,
                              "posting_id": posting_id,
                              "listing_url": scalar_text(raw.get("job_url"))}
        record["url"] = scalar_text(raw.get("job_url"))
        record["application_url"] = scalar_text(raw.get("job_url_direct"))
        record["company"] = scalar_text(raw.get("company"))
        record["is_remote"] = scalar_bool(raw.get("is_remote"))
        record["min_amount"] = scalar_number(raw.get("min_amount"))
        record["max_amount"] = scalar_number(raw.get("max_amount"))
        location = raw.get("source_location", raw.get("location"))
        if isinstance(location, Mapping):
            country_value = location.get("country")
            # Recorded .dict() DataFrames may contain Country's tuple value.
            if isinstance(country_value, list) and country_value:
                country_value = country_value[0].split(",")[0]
            try:
                country_code = _country(country_value).indeed_domain_value[1]
            except ValueError:
                country_code = None
            name = scalar_text(location.get("name")) or ", ".join(
                part for part in (scalar_text(location.get("city")), scalar_text(location.get("state")),
                                  country_code) if part)
            record["location"] = {**location, "name": name or None, "country": country_code}
            record["office_details"] = [record["location"]]
        comp = raw.get("source_compensation", raw.get("compensation"))
        if isinstance(comp, Mapping):
            for key in ("interval", "currency", "min_amount", "max_amount"):
                if record.get(key) is None:
                    record[key] = comp.get(key)
            record["salary_source"] = record.get("salary_source") or "direct_data"
        facts = []
        for key in ("location", "is_remote", "job_type", "interval", "currency", "min_amount", "max_amount",
                    "salary_source", "source_compensation", "source_location", "work_from_home_type", "experience_range"):
            value = record.get(key)
            if value is not None:
                identity = record["identity"]
                seed = f"jobspy|{site}|{identity['posting_id']}|{identity['listing_url']}|{key}"
                facts.append({"id": "jobspy-" + hashlib.sha256(seed.encode()).hexdigest()[:20],
                              "kind": key, "value": value, "authority": "aggregator", "provider_field_path": key})
        record["facts"] = facts
        records.append(record)
    return tuple(records)


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """One finite, checkpointable page; -1 is never passed to the SDK.

    Indeed/Google use the pinned SDK's actual page cursor. LinkedIn/Naukri use
    supported offsets, retaining their SDK ceilings. Other boards remain honest
    bounded snapshots, not an invented resumable/exhaustive API.
    """
    from .jobspy_transport import scrape_snapshot
    from .transport import TaskDeferred, deferred_page, task_params, reserve_budget

    metadata = {"cycle_id": context.cycle_id, "contract_version": _VERSION,
                "provider": "jobspy", "authoritative": False, "http_attempts": 0}
    try:
        target = task.target
        if target is None or target.provider != "jobspy" or target.kind not in {"query", "probe", "employer_probe"}:
            raise ValueError("Expected a JobSpy query/probe/employer_probe target")
        params = task_params(task, ("jobspy",))
        if "country" not in params and "country_indeed" not in params and len(target.native_targets) == 1:
            board = params.get("board", params.get("site", params.get("site_name", target.scope)))
            if board in {"indeed", "glassdoor", "zip_recruiter", "naukri", "bdjobs"}:
                params["country"] = target.native_targets[0]
        metadata.update(origins=list(target.origins), scope=target.scope,
                        request_key=target.request_key(), kind=target.kind)
        runtime = context.runtime
        requested = work_limit(params.get("results_wanted", params.get("limit", params.get("results_per_site",
                    runtime.get("request_defaults", {}).get("results_per_site", -1)))), "results_wanted")
        if (requested == 0
                or "jobspy" not in context.intent.sources_enabled or not context.intent.jobspy_sites):
            return DiscoveryPage(task_id=task.task_id, exhausted=True, coverage=Coverage.EMPTY,
                                 request_metadata={**metadata, "no_work": True})
        state = json.loads(cursor) if cursor is not None else {
            "v": 2, "key": target.request_key(), "consumed": 0, "page": 0,
            "offset": _nonnegative(params.get("offset", 0), "offset"), "token": None,
            "skip": _nonnegative(params.get("offset", 0), "offset"), "seen": [], "tokens": []}
        if (not isinstance(state, dict) or state.get("v") != 2 or state.get("key") != target.request_key()
                or any(type(state.get(k)) is not int or state[k] < 0 for k in ("consumed", "page", "offset", "skip"))
                or any(not isinstance(state.get(k), list) or any(not isinstance(v, str) for v in state[k])
                       for k in ("seen", "tokens"))
                or state.get("token") is not None and not isinstance(state["token"], str)):
            raise ValueError("Invalid JobSpy continuation checkpoint")
        remaining = page_size(requested, _RESULT_CAP, used=state["consumed"])
        if remaining == 0:
            return DiscoveryPage(task_id=task.task_id, exhausted=True, capped=True,
                                 coverage=Coverage.CAPPED, request_metadata={**metadata, "result_limit": requested})
        # Resolve and validate the actual native board before choosing its cursor form.
        page_params = {k: v for k, v in params.items() if k not in {"limit", "results_per_site", "results_wanted"}}
        board_hint = params.get("board", params.get("site", params.get("site_name", target.scope)))
        if board_hint in {"linkedin", "naukri"} and state["offset"] < 1000:
            remaining = min(remaining, 1000 - state["offset"])
        page_params.update(results_wanted=remaining, offset=state["offset"])
        kwargs, lowered = lower_request(page_params, scope=target.scope, runtime=runtime)
        metadata.update(lowered, requested_results=requested, result_limit=requested)
        board = lowered["board"]
        if board not in context.intent.jobspy_sites:
            raise ValueError(f"Board {board} is not enabled in this intent")
        grant = runtime.get("authorized_sources", {}).get(f"jobspy:{board}")
        if grant is False or (board == "linkedin" and grant is not True):
            return DiscoveryPage(task_id=task.task_id, coverage=Coverage.UNKNOWN, complete=False,
                                 request_metadata={**metadata, "deferred": True}, failures=(FetchFailure(
                                     FailureKind.ACCESS_DENIED, f"Explicit access required for jobspy:{board}"),))
        if version("python-jobspy") != _VERSION:
            raise ValueError(f"This adapter requires python-jobspy=={_VERSION}")
        native_page = board in {"indeed", "google"}
        if native_page:
            # Skip explicit initial offsets across pages, never replay all earlier
            # pages for each subsequent window. Preserve full native responses.
            kwargs["offset"] = 0
            kwargs["results_wanted"] = _RESULT_CAP
        metadata["request_params"] = {k: v for k, v in kwargs.items() if k not in {"proxies", "ca_cert", "user_agent"}}
        if getattr(context, "reserve_budget", None) is None:
            reserve_budget(context, "request", 1)
        snapshot = (scrape_snapshot(kwargs, context, native_page=True, continuation=state["token"])
                    if native_page else scrape_snapshot(kwargs, context))
        metadata.update(snapshot.metadata)
        rows = list(snapshot.rows)
        fingerprint = hashlib.sha256(json.dumps(sorted(
            str(row.get("id") or row.get("job_url") or row) for row in rows),
            separators=(",", ":")).encode()).hexdigest()
        repeated = bool(rows) and fingerprint in state["seen"]
        next_token = metadata.pop("next_native_cursor", None)
        if native_page and not snapshot.failures and metadata.get("native_page") is not True:
            raise ValueError("JobSpy worker did not return a native page envelope")
        token_hash = (hashlib.sha256(next_token.encode()).hexdigest() if next_token else None)
        repeated_token = bool(next_token) and (next_token == state["token"] or token_hash in state["tokens"])
        if native_page and state["skip"]:
            skipped = min(state["skip"], len(rows))
            state["skip"] -= skipped
            rows = rows[skipped:]
        if requested != -1:
            rows = rows[:requested - state["consumed"]]
        if repeated:
            rows = []
        records = _records(rows, board, target.origins)
        state["consumed"] += len(rows)
        state["page"] += 1
        full = len(snapshot.rows) >= kwargs["results_wanted"]
        reached = requested != -1 and state["consumed"] >= requested
        offset_supported = board in {"linkedin", "naukri"}
        next_offset = state["offset"] + kwargs["results_wanted"]
        sdk_ceiling = offset_supported and next_offset >= 1000
        continuing = (not snapshot.failures and not reached and not repeated and not repeated_token
                      and (bool(next_token) and bool(snapshot.rows) if native_page
                           else offset_supported and full and not sdk_ceiling))
        connector_cap = (not native_page and full and (not offset_supported or sdk_ceiling))
        capped = reached or repeated or repeated_token or connector_cap
        metadata.update(fetched=len(snapshot.rows), collected=state["consumed"],
            snapshot_exhausted=not snapshot.failures, continuation_supported=native_page or offset_supported,
            stop_reason=("result_target" if reached else "pagination_stalled" if repeated or repeated_token
                         else "connector_ceiling" if connector_cap else "continuing" if continuing
                         else "provider_end" if not snapshot.failures else "provider_error"),
            coverage_note="Non-authoritative search pages; no verified board exhaustion or closure inference")
        snapshot_key = json.dumps([target.request_key(), task.refresh_generation, state["page"]], separators=(",", ":"))
        state["offset"] = next_offset if offset_supported else state["offset"]
        state["token"] = next_token
        state["seen"].append(fingerprint)
        if token_hash:
            state["tokens"].append(token_hash)
        return DiscoveryPage(task_id=task.task_id or "", records=records,
                             next_cursor=json.dumps(state, separators=(",", ":")) if continuing else None,
                             exhausted=not continuing and not snapshot.failures,
                             coverage=Coverage.CAPPED if capped else Coverage.PARTIAL if records else
                                      Coverage.UNKNOWN if snapshot.failures else Coverage.EMPTY,
                             complete=False, partial=True, capped=capped,
                             snapshot_identity="jobspy-" + hashlib.sha256(snapshot_key.encode()).hexdigest(),
                             request_metadata=metadata, failures=snapshot.failures)
    except TaskDeferred as exc:
        return deferred_page(task, context, exc, cursor=cursor, **metadata)


def store_jobspy_results(conn, df, source_label: str, *, context=None) -> tuple[int, int]:
    """Legacy signature; all writes, including supplied connections, are shared."""
    from .context import build_context
    from . import repository

    context = build_context() if context is None else context
    rows = df.to_dict(orient="records") if hasattr(df, "to_dict") else tuple(df)
    records = tuple(_clean(row) if isinstance(row, Mapping) and row.get("provider") == "jobspy" and
                    isinstance(row.get("raw_record"), Mapping) else _records((row,), source_label)[0]
                    for row in rows)
    # Publication origins apply to the entire batch. Group them so an unrelated
    # row cannot inherit another query's exclusions/admission provenance.
    groups = {}
    for record in records:
        key = (record["identity"]["tenant"], tuple(record.get("origins", ())))
        groups.setdefault(key, []).append(record)
    new = existing = 0
    for (board, origins), batch in groups.items():
        result = repository.publish_records(batch, "jobspy", context=context, conn=conn,
                                            tenant=board, origins=origins)
        new += result.new
        existing += result.strict_duplicates + result.updated
    return new, existing


def _publish_task(task, context) -> dict:
    result = {"new": 0, "existing": 0, "errors": 0, "total": 0, "failures": [], "pages": []}
    cursor = None
    for _ in page_indices(context.runtime.get("max_pages_per_task", -1)):
        page = execute_task(task, context, cursor)
        if page.records and not page.failures:
            new, existing = store_jobspy_results(None, page.records, task.target.scope or "jobspy", context=context)
            result["new"] += new
            result["existing"] += existing
        result["total"] += len(page.records)
        result["errors"] += len(page.failures)
        result["failures"].extend(page.failures)
        result["pages"].append(page)
        if page.failures or page.exhausted or page.capped or page.next_cursor is None or page.next_cursor == cursor:
            break
        cursor = page.next_cursor
    return result


def _run_one_search(search: dict, sites: list[str], results_per_site: int, hours_old: int,
                    proxy_config: dict | None, defaults: dict, max_retries: int,
                    accept_locs: list[str], reject_locs: list[str], glassdoor_map: dict,
                    *, context=None) -> dict:
    """Compatibility runner: common assessment replaces private location filters."""
    from .context import build_context

    stats = {"new": 0, "existing": 0, "errors": 0, "filtered": 0, "total": 0,
             "label": str(search.get("query", "")), "failures": [], "pages": []}
    if not sites or results_per_site == 0:
        return stats
    context = build_context() if context is None else context
    for board in dict.fromkeys(sites):
        params = {key: value for key, value in search.items() if key != "origins"}
        params.update(site_name=board, results_wanted=results_per_site)
        params.setdefault("hours_old", hours_old)
        if "country" not in params and "country_indeed" not in params:
            params["country_indeed"] = defaults.get("country_indeed")
        if board == "glassdoor" and params.get("location") in glassdoor_map:
            params["location"] = glassdoor_map[params["location"]]
        if proxy_config:
            params["proxies"] = [proxy_config["jobspy"]]
        origins = tuple(search.get("origins", ()))
        target = SourceTarget("jobspy", scope=board, request_params=tuple(params.items()), origins=origins)
        task = SearchTask(task_id=target.request_key(), target=target)
        result = _publish_task(task, context)
        for key in ("new", "existing", "errors", "total"):
            stats[key] += result[key]
        stats["failures"].extend(result["failures"])
        stats["pages"].extend(result["pages"])
    return stats


def search_jobs(query: str, location: str, sites: list[str] | None = None, remote_only: bool = False,
                results_per_site: int = -1, hours_old: int = 72, proxy: str | None = None,
                country_indeed: str = "usa", *, context=None) -> dict:
    from .context import build_context

    if sites == [] or results_per_site == 0:
        return {"new": 0, "existing": 0, "errors": 0, "total": 0, "disabled": True,
                "failures": [], "pages": []}
    context = build_context() if context is None else context
    sites = list(context.intent.jobspy_sites) if sites is None else sites
    return _run_one_search({"query": query, "location": location, "remote": remote_only,
                            "country_indeed": country_indeed}, sites, results_per_site, hours_old,
                           parse_proxy(proxy) if proxy else None, {}, 0, [], [], {}, context=context)


def _full_crawl(search_cfg: dict, tiers: list[int] | None = None, locations: list[str] | None = None,
                sites: list[str] | None = None, results_per_site: int = -1, hours_old: int = 72,
                proxy: str | None = None, max_retries: int = 2, *, context=None) -> dict:
    from .context import build_context
    from .planner import plan_tasks

    stats = {"new": 0, "existing": 0, "errors": 0, "queries": 0, "total": 0,
             "db_total": None, "failures": [], "pages": []}
    if sites == [] or tiers == [] or locations == [] or results_per_site == 0:
        return {**stats, "disabled": True}
    context = build_context(search_cfg) if context is None else context
    boards = tuple(context.intent.jobspy_sites) if sites is None else tuple(
        board for board in sites if board in context.intent.jobspy_sites)
    selected = replace(context.intent,
        sources_enabled=("jobspy",) if "jobspy" in context.intent.sources_enabled else (),
        jobspy_sites=boards,
        selected_tiers=tuple(tier for tier in context.intent.selected_tiers if tiers is None or tier in tiers),
        active_market_labels=tuple(label for label in context.intent.active_market_labels
                                   if locations is None or label in locations))
    planning_runtime = dict(context.runtime)
    planning_runtime["request_defaults"] = dict(context.runtime["request_defaults"],
                                                results_per_site=results_per_site, hours_old=hours_old)
    planning = replace(context, intent=selected, runtime=planning_runtime)
    # Native exploration/probe selection and Bayt origin dedup are the shared
    # planner's responsibility, not another legacy market Cartesian product.
    # Only planning is narrowed; admission still receives the current context.
    for task in plan_tasks(planning, inventories={}):
        target = task.target
        if target.kind == "deferred":
            failure = FetchFailure(FailureKind.CONFIGURATION, dict(target.request_params).get("reason", "Invalid native target"))
            stats["failures"].append(failure)
            stats["errors"] += 1
            continue
        params = dict(target.request_params)
        params.update(results_wanted=results_per_site, hours_old=hours_old)
        if proxy:
            params["proxies"] = [parse_proxy(proxy)["jobspy"]]
        # Older planner versions express remote mode only in Google terms.
        markets = [market for market in selected.active_markets()
                   if f"market:{market.label.value}" in target.origins]
        if markets and all(len(m.work_modes) == 1 and m.work_modes[0].value == "remote" for m in markets):
            params.setdefault("is_remote", True)
        target = replace(target, request_params=tuple(params.items()))
        task = replace(task, target=target, task_id=target.request_key(), logical_request_key=target.request_key())
        result = _publish_task(task, context)
        stats["queries"] += 1
        for name in ("new", "existing", "errors", "total"):
            stats[name] += result[name]
        stats["failures"].extend(result["failures"])
        stats["pages"].extend(result["pages"])
    return stats


def run_discovery(cfg: dict | None = None, sites_override: list[str] | None = None, *, context=None) -> dict:
    from applypilot import config
    from .context import build_context
    from .intent import compile_source_selection

    if sites_override == []:
        return {"new": 0, "existing": 0, "errors": 0, "queries": 0, "disabled": True}
    cfg = (config.read_discovery_config() if context is None else {}) if cfg is None else cfg
    sites = (list(context.intent.jobspy_sites) if context is not None else list(compile_source_selection(cfg)[1])) if sites_override is None else list(sites_override)
    if not sites or cfg.get("defaults", {}).get("results_per_site") == 0:
        return {"new": 0, "existing": 0, "errors": 0, "queries": 0, "disabled": True}
    context = build_context(cfg) if context is None else context
    defaults = context.runtime.get("request_defaults", {})
    return _full_crawl(cfg, tiers=cfg.get("tiers"), locations=cfg.get("location_labels"), sites=sites,
                       results_per_site=defaults.get("results_per_site", -1),
                       hours_old=defaults.get("hours_old", 72), proxy=cfg.get("proxy"), context=context)


def run_jobspy_discovery(cfg: dict | None = None, sites_override: list[str] | None = None, *, context=None) -> dict:
    """Named source entrypoint; identical context, lowering and publication path."""
    return run_discovery(cfg, sites_override, context=context)

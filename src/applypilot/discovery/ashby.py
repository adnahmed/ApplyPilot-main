"""Ashby posting normalization and one-page discovery adapter."""
from __future__ import annotations

from copy import deepcopy

from applypilot.discovery.greenhouse import _strip_html
from applypilot.discovery.models import DiscoveryPage, SearchTask
from applypilot.discovery.normalization import scalar_bool

def _location_string(posting: dict) -> str:
    """Compose location from primary + secondary + workplaceType + isRemote.

    Ashby splits location across several fields. We collapse to one string
    with ``remote``/``hybrid`` prepended (when applicable) so the
    downstream filter's remote-allowlist fires.
    """
    parts: list[str] = []
    wt = (posting.get("workplaceType") or "").lower()
    if wt == "remote" or scalar_bool(posting.get("isRemote")) is True:
        parts.append("remote")
    elif wt and wt != "on-site":
        parts.append(wt)
    primary = posting.get("location")
    if primary:
        parts.append(str(primary))
    for extra in posting.get("secondaryLocations") or []:
        if isinstance(extra, dict):
            extra = extra.get("location") or ""
        if extra and str(extra) not in parts:
            parts.append(str(extra))
    return ", ".join(parts)


def parse_record(raw: dict, employer: dict | str) -> dict | None:
    if scalar_bool(raw.get("isListed")) is False:
        return None
    emp = employer if isinstance(employer, dict) else {"name": employer}
    description = raw.get("descriptionPlain") or _strip_html(raw.get("descriptionHtml") or "")
    locations = []
    if raw.get("location") or raw.get("address"):
        locations.append({"location": raw.get("location"), "address": deepcopy(raw.get("address"))})
    locations.extend(deepcopy(raw.get("secondaryLocations") or []))
    mode = raw.get("workplaceType")
    if mode is None and scalar_bool(raw.get("isRemote")) is True:
        mode = "remote"
    return {
        "raw_record": deepcopy(raw), "title": raw.get("title"),
        "company": emp.get("name"), "employer_name": emp.get("name"), "employer_slug": emp.get("slug"),
        "url": raw.get("jobUrl"), "application_url": raw.get("applyUrl") or raw.get("jobUrl"),
        "source_posting_id": raw.get("id"), "source_internal_id": raw.get("internalJobId"),
        "source_requisition_id": raw.get("requisitionId"),
        "description": description or None, "full_description": description or None,
        "location": _location_string(raw) or None, "office_locations": locations,
        "work_mode": mode, "employment_type": raw.get("employmentType"),
        "department": raw.get("department"), "team": raw.get("team"),
        "compensation": deepcopy(raw.get("compensation")), "posted_at": raw.get("publishedAt"),
    }


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    from applypilot.discovery.ats_common import execute_board_task
    return execute_board_task(task, context, cursor, provider="ashby",
                              parser=parse_record)

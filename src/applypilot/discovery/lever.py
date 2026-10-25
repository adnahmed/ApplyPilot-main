"""Lever posting normalization and one-page discovery adapter."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone

from applypilot.discovery.greenhouse import _strip_html
from applypilot.discovery.models import DiscoveryPage, SearchTask



def _location_string(posting: dict) -> str:
    """Compose location from categories.location + workplaceType + allLocations.

    Treats ``on-site`` as the default-omit; remote/hybrid surface in the
    string so the downstream filter's remote-allowlist fires.
    """
    cats = posting.get("categories") or {}
    parts: list[str] = []
    wt = (posting.get("workplaceType") or "").lower()
    if wt and wt != "on-site":
        parts.append(wt)
    loc = cats.get("location")
    if loc:
        parts.append(str(loc))
    extra = cats.get("allLocations") or []
    if isinstance(extra, list):
        for e in extra:
            if e and e not in parts:
                parts.append(str(e))
    return ", ".join(parts)


def _description_text(posting: dict) -> str:
    """Plain-text body composed from description + lists + additional."""
    parts: list[str] = []
    if posting.get("descriptionPlain"):
        parts.append(posting["descriptionPlain"])
    elif posting.get("description"):
        parts.append(_strip_html(posting["description"]))
    for section in posting.get("lists") or []:
        body = section.get("content") or ""
        if not body:
            continue
        title = section.get("text") or ""
        if title:
            parts.append(f"\n{title}")
        parts.append(_strip_html(body))
    if posting.get("additionalPlain"):
        parts.append(posting["additionalPlain"])
    elif posting.get("additional"):
        parts.append(_strip_html(posting["additional"]))
    return "\n\n".join(p for p in parts if p)


def parse_record(raw: dict, employer: dict | str) -> dict:
    emp = employer if isinstance(employer, dict) else {"name": employer}
    cats = raw.get("categories") or {}
    locations = list(cats.get("allLocations") or [])
    if cats.get("location") and cats["location"] not in locations:
        locations.insert(0, cats["location"])
    created = raw.get("createdAt")
    posted_at = None
    if isinstance(created, (int, float)) and not isinstance(created, bool) and created > 0:
        try:
            posted_at = datetime.fromtimestamp(created / 1000, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
    description = _description_text(raw)
    return {
        "raw_record": deepcopy(raw), "title": raw.get("text"),
        "company": emp.get("name"), "employer_name": emp.get("name"),
        "employer_slug": emp.get("slug"),
        "url": raw.get("hostedUrl"), "application_url": raw.get("applyUrl") or raw.get("hostedUrl"),
        "description": description or None, "full_description": description or None,
        "source_posting_id": raw.get("id"), "source_internal_id": raw.get("internalId"),
        "source_requisition_id": raw.get("requisitionId"),
        "location": _location_string(raw) or None, "office_locations": deepcopy(locations),
        "office_country": raw.get("country"), "work_mode": raw.get("workplaceType"),
        "employment_type": cats.get("commitment"), "department": cats.get("department"),
        "team": cats.get("team"), "compensation": deepcopy(raw.get("salaryRange")),
        "salary_description": raw.get("salaryDescription"), "posted_at": posted_at,
    }


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    from applypilot.discovery.ats_common import execute_board_task
    return execute_board_task(task, context, cursor, provider="lever",
                              parser=parse_record)


# Kept exposed (some tests + pipeline.py still call this name)

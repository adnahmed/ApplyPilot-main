"""SmartRecruiters public posting API, one offset page per task execution."""
import json
from urllib.parse import quote, urlencode

from applypilot.discovery.ats_common import failure_page
from applypilot.discovery.normalization import scalar_bool
from applypilot.discovery.careers.base import (
    CareerParser, offset_cursor, record, records_page, request, task_settings,
)


def parse_record(raw, tenant, company):
    posting_id = raw.get("id") or raw.get("uuid")
    ad = raw.get("jobAd") or {}
    sections = ad.get("sections") or {}
    description = "\n".join(section["text"] if isinstance(section.get("text"), str) else section.get("body", "")
                            for section in sections.values() if isinstance(section, dict))
    location = raw.get("location") or {}
    mode = raw.get("workplaceType") or raw.get("workMode")
    if isinstance(mode, dict):
        mode = mode.get("label") or mode.get("name") or mode.get("id")
    if not mode and isinstance(location, dict) and scalar_bool(location.get("remote")) is True:
        mode = "remote"
    # API `ref` is a detail API URL, not a career-page path or posting ID.
    url = ad.get("url") or raw.get("postingUrl") or raw.get("url")
    if not url and posting_id:
        url = f"https://jobs.smartrecruiters.com/{quote(tenant, safe='')}/{quote(str(posting_id), safe='')}"
    return record(raw, provider="smartrecruiters", tenant=tenant, company=company, url=url,
                  application_url=raw.get("applyUrl"), posting_id=posting_id, locations=location,
                  description=description or raw.get("description"), work_mode=mode,
                  employment_type=raw.get("typeOfEmployment") or raw.get("employmentType"),
                  salary=raw.get("salary") or raw.get("compensation"))


def execute_task(task, context, cursor=None):
    metadata = {"parser": "smartrecruiters", "cursor": cursor}
    try:
        params, tenant, approved = task_settings(task, context, "smartrecruiters")
        base = f"https://api.smartrecruiters.com/v1/companies/{quote(tenant, safe='')}/postings"
        allowed = {base, base + "/", f"https://careers.smartrecruiters.com/{tenant}",
                   f"https://careers.smartrecruiters.com/{tenant}/", f"https://jobs.smartrecruiters.com/{tenant}",
                   f"https://jobs.smartrecruiters.com/{tenant}/"}
        if any(params[key] not in allowed for key in ("url", "career_url", "endpoint") if params.get(key)):
            raise PermissionError("SmartRecruiters URL does not identify this public company posting endpoint")
        limit = params.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("SmartRecruiters limit must be 1..100")
        state = offset_cursor(task, cursor)
        offset = state["offset"]
        url = base + "?" + urlencode({"offset": offset, "limit": limit})
        metadata.update(url=url, tenant=tenant, api_region="global", offset=offset, limit=limit,
                        contract="public posting API")
        data = request(context, url)
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise ValueError("SmartRecruiters response requires content[]")
        total, rows = data.get("totalFound"), data["content"]
        if type(total) is not int or total < 0 or len(rows) > limit:
            raise ValueError("SmartRecruiters requires a nonnegative totalFound and bounded page")
        if "total" in state and total != state["total"]:
            raise ValueError("SmartRecruiters inventory changed during pagination; restart refresh")
        if offset + len(rows) > total or not rows and offset < total:
            raise ValueError("SmartRecruiters page contradicts totalFound")
        next_cursor = None if offset + len(rows) >= total else json.dumps({
            "request_key": task.target.request_key(), "offset": offset + len(rows), "total": total}, sort_keys=True)
        return records_page(task, context, rows,
                            lambda raw: parse_record(raw, tenant, params.get("name") or params.get("company") or tenant),
                            next_cursor=next_cursor, metadata={**metadata, "total": total})
    except Exception as error:
        return failure_page(task, error, metadata=metadata)


class SmartRecruitersCareerParser(CareerParser):
    provider = "smartrecruiters"

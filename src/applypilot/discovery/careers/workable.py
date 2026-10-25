"""Workable public account published jobs. No employer API token is needed."""
from urllib.parse import quote

from applypilot.discovery.ats_common import failure_page
from applypilot.discovery.normalization import scalar_bool
from applypilot.discovery.careers.base import (
    CareerParser, record, records_page, request, task_settings,
)


def parse_record(raw, tenant, company):
    if raw.get("state") and raw["state"] != "published":
        return None
    locations = raw.get("locations") or raw.get("location")
    mode = raw.get("workplace_type")
    if not mode and scalar_bool(raw.get("remote")) is True:
        mode = "remote"
    return record(raw, provider="workable", tenant=tenant, company=company,
                  url=raw.get("url") or raw.get("shortlink"), application_url=raw.get("application_url"),
                  posting_id=raw.get("shortcode") or raw.get("id"), locations=locations,
                  description="\n".join(raw[key] for key in ("description", "requirements", "benefits")
                                        if isinstance(raw.get(key), str)),
                  work_mode=mode, employment_type=raw.get("employment_type"), salary=raw.get("salary"))


def execute_task(task, context, cursor=None):
    metadata = {"parser": "workable", "cursor": cursor}
    try:
        params, tenant, approved = task_settings(task, context, "workable")
        if cursor is not None:
            raise ValueError("Workable published-jobs snapshot does not accept a cursor")
        endpoint = f"https://www.workable.com/api/accounts/{quote(tenant, safe='')}?details=true"
        allowed = {endpoint, endpoint.removesuffix("?details=true"),
                   f"https://apply.workable.com/{tenant}", f"https://apply.workable.com/{tenant}/",
                   f"https://{tenant}.workable.com", f"https://{tenant}.workable.com/"}
        if any(params[key] not in allowed for key in ("url", "career_url", "endpoint") if params.get(key)):
            raise PermissionError("Workable URL does not identify this public account")
        metadata.update(url=endpoint, tenant=tenant, api_region="global", contract="public account published jobs")
        data = request(context, endpoint)
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("Workable response requires jobs[]")
        return records_page(task, context, data["jobs"],
                            lambda raw: parse_record(raw, tenant, params.get("name") or params.get("company") or tenant),
                            metadata=metadata)
    except Exception as error:
        return failure_page(task, error, metadata=metadata)


class WorkableCareerParser(CareerParser):
    provider = "workable"

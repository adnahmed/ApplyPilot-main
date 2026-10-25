"""Complete Remotive feed. The planner must enforce the six-hour minimum cadence."""
from __future__ import annotations

from .models import Coverage, DiscoveryPage, SearchTask
from .normalization import scalar_text
from .transport import (TaskDeferred, deferred_page, legacy_run, load_export,
                        page_metadata, request_json, task_params)

_REMOTIVE_API = "https://remotive.com/api/remote-jobs"
MIN_REFRESH_SECONDS = 6 * 60 * 60
FEED_DELAY_SECONDS = 24 * 60 * 60


def _fetch_remotive(context):
    return request_json(context, _REMOTIVE_API)


def _parse_remotive_job(item: dict) -> dict:
    salary = scalar_text(item.get("salary"))
    location = scalar_text(item.get("candidate_required_location"))
    job_type = scalar_text(item.get("job_type"))
    return {
        "provider": "remotive", "source": "remotive", "posting_id": scalar_text(item.get("id")),
        "source_job_id": scalar_text(item.get("id")), "url": scalar_text(item.get("url")),
        "listing_url": scalar_text(item.get("url")), "title": scalar_text(item.get("title")),
        "company": scalar_text(item.get("company_name")), "location": location,
        "candidate_required_location": location, "work_mode": "remote", "is_remote": True,
        "job_type": job_type, "employment_type": job_type, "salary": salary, "salary_text": salary,
        "description": scalar_text(item.get("description")),
        "posted_at": scalar_text(item.get("publication_date")),
        "category": scalar_text(item.get("category")), "raw": dict(item),
        "application_url": None,
    }


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """Params: none for API; mode='export', export_path for authorized native JSON."""
    cadence = {"min_refresh_seconds": MIN_REFRESH_SECONDS, "feed_delay_seconds": FEED_DELAY_SECONDS}
    try:
        params = task_params(task, ("remotive",))
        if set(params) - {"mode", "export_path"} or cursor is not None:
            raise TaskDeferred("Remotive is one complete feed; no category/limit/query fan-out")
        if params.get("mode", "api") == "export":
            envelope = load_export(task, context, params, cursor)
            if not envelope["complete"] or envelope.get("next_cursor"):
                raise TaskDeferred("Remotive export must contain a complete feed")
            data = envelope["payload"]
        elif params.get("mode", "api") == "api":
            data = _fetch_remotive(context)
        else:
            raise TaskDeferred("Unsupported Remotive access mode")
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            raise ValueError("Remotive response requires a jobs array")
        attribution = {"name": "Remotive", "url": "https://remotive.com",
                       "feed_metadata": {k: v for k, v in data.items() if k != "jobs"}}
        records, invalid = [], 0
        for item in data["jobs"]:
            if not isinstance(item, dict):
                invalid += 1
                continue
            job = _parse_remotive_job(item)
            if not job["url"] or not job["title"] or not job["posting_id"]:
                invalid += 1
                continue
            job["attribution"] = attribution
            records.append(job)
        count = data.get("job-count", len(data["jobs"]))
        if type(count) is not int or count != len(data["jobs"]):
            invalid += 1
        if invalid:
            return deferred_page(task, context, ValueError(f"{invalid} malformed/incomplete feed entries"),
                                 records=records, attribution=attribution, **cadence)
        return DiscoveryPage(task.task_id or "", tuple(records), exhausted=True,
                             coverage=Coverage.COMPLETE, request_metadata=page_metadata(
                                 task, context, request_url=_REMOTIVE_API, attribution=attribution,
                                 authoritative_snapshot=True, fetched=len(records), **cadence))
    except (TaskDeferred, ValueError, TypeError) as exc:
        return deferred_page(task, context, exc, cursor=cursor, **cadence)


def run_remotive_discovery(workers=1, *, context=None, task=None):
    return legacy_run(execute_task, "remotive", context=context, task=task)

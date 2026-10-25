"""One complete Remote OK feed refresh. Tags are output facts, not fan-out."""
from __future__ import annotations

from .models import Coverage, DiscoveryPage, SearchTask
from .normalization import scalar_number, scalar_text
from .transport import (TaskDeferred, deferred_page, legacy_run, load_export,
                        page_metadata, request_json, task_params)

_REMOTEOK_API = "https://remoteok.com/api"


def _fetch_remoteok(context):
    """Return the entire native feed including legal/attribution entries."""
    return request_json(context, _REMOTEOK_API)


def _parse_remoteok_job(item: dict) -> dict:
    posting_id = scalar_text(item.get("id"))
    url = scalar_text(item.get("url"))
    return {
        "url": url, "listing_url": url, "source": "remoteok", "provider": "remoteok",
        "posting_id": posting_id, "source_job_id": posting_id,
        "title": scalar_text(item.get("position")), "company": scalar_text(item.get("company")),
        "location": scalar_text(item.get("location")), "work_mode": "remote", "is_remote": True,
        "salary_min": scalar_number(item.get("salary_min")),
        "salary_max": scalar_number(item.get("salary_max")),
        "salary_currency": scalar_text(item.get("salary_currency")),
        "salary_text": scalar_text(item.get("salary")),
        "description": scalar_text(item.get("description")),
        "employment_type": scalar_text(item.get("employment_type")),
        "posted_at": scalar_text(item.get("date")), "tags": item.get("tags", []),
        "application_url": scalar_text(item.get("apply_url")), "raw": dict(item),
    }


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """Params: none for API; mode='export', export_path for authorized native JSON."""
    try:
        params = task_params(task, ("remoteok",))
        if set(params) - {"mode", "export_path"} or cursor is not None:
            raise TaskDeferred("Remote OK is one complete feed, not a query/tag/page request")
        mode = params.get("mode", "api")
        if mode == "export":
            envelope = load_export(task, context, params, cursor)
            if not envelope["complete"] or envelope.get("next_cursor"):
                raise TaskDeferred("Remote OK export must contain a complete feed")
            data = envelope["payload"]
        elif mode == "api":
            data = _fetch_remoteok(context)
        else:
            raise TaskDeferred("Unsupported Remote OK access mode")
        if not isinstance(data, list):
            raise ValueError("Remote OK feed must be a JSON array")
        metadata, records, invalid = [], [], 0
        for item in data:
            if not isinstance(item, dict):
                invalid += 1
            elif "legal" in item and not item.get("position"):
                metadata.append(dict(item))
            else:
                job = _parse_remoteok_job(item)
                if not job["url"] or not job["title"] or not job["posting_id"]:
                    invalid += 1
                else:
                    records.append(job)
        attribution = {"name": "Remote OK", "url": "https://remoteok.com", "feed_metadata": metadata}
        for job in records:
            job["attribution"] = attribution
        info = page_metadata(task, context, request_url=_REMOTEOK_API, attribution=attribution,
                             fetched=len(data), invalid_records=invalid, authoritative_snapshot=not invalid)
        if invalid:
            return deferred_page(task, context, ValueError(f"{invalid} malformed feed records"),
                                 records=records, attribution=attribution, authoritative_snapshot=False)
        return DiscoveryPage(task.task_id or "", tuple(records), exhausted=True,
                             coverage=Coverage.COMPLETE, request_metadata=info)
    except (TaskDeferred, ValueError, TypeError) as exc:
        return deferred_page(task, context, exc, cursor=cursor)


def run_remoteok_discovery(workers=1, *, context=None, task=None):
    return legacy_run(execute_task, "remoteok", context=context, task=task)

"""One display feed for saved job history, account tasks and operational errors."""
from __future__ import annotations

from datetime import datetime, timezone

from applypilot import postgres
from applypilot.operations import store
from applypilot.operations.runtime import diagnostic_details, sanitize
from .data import activity_sort_key
from .presentation import humanize_issue


SOURCE_LABELS = {
    "sdk": "Job source", "twikit": "X / Twikit", "linkedin_scraper": "LinkedIn browser reader",
    "linkedin_rs": "LinkedIn API reader", "freehire": "FreeHire", "ddgs": "DuckDuckGo",
    "searxng": "SearXNG", "llm": "AI provider", "gmail": "Gmail", "github": "GitHub",
    "sources": "Job sources", "browseros": "BrowserOS", "pdf": "PDF",
}


def source_label(source):
    return SOURCE_LABELS.get(source, str(source or "Workspace").replace("_", " ").capitalize())


def operation_identity(record):
    """Use recorded integration identity; a generic SDK name is not a source."""
    details = record.get("details") or {}
    diagnostics = [details.get("diagnostics")]
    diagnostics += [item.get("diagnostics") for item in details.get("exception_chain", [])
                    if isinstance(item, dict)]
    diagnostics = [item for item in diagnostics if isinstance(item, dict)]
    source = details.get("integration") or next((item.get("component") for item in diagnostics
                                               if item.get("component")), None) or record.get("component") or "unknown"
    operation = next((item.get("subcommand") for item in diagnostics if item.get("subcommand")), None)
    operation = operation or record.get("operation") or "request"
    action = {"call": "request", "verify": "connection check", "check": "connection check",
              "search_jobs": "job search", "job_search": "job search", "search": "search",
              "sync": "sync", "generate": "generation", "draft": "draft"}.get(operation, operation.replace("_", " "))
    outcome = record.get("outcome") or "recorded"
    ending = {"success": "completed", "partial": "partly completed", "empty": "returned no results",
              "started": "started"}.get(outcome, outcome.replace("_", " "))
    return source, f"{source_label(source)} {action} {ending}"


def severity_for_state(state):
    if state in {"failed", "error", "timed_out"}:
        return "error"
    return "warning" if state in {"cancelled", "interrupted", "deferred"} else "info"


def merge_operations(root, value, query):
    """Merge after setup tasks, then apply the same severity/source filters."""
    kind = query.get("kind") or "all"
    severity = query.get("severity") or ""
    if severity not in {"", "info", "warning", "error", "critical"}:
        raise ValueError("Unknown activity severity")
    if kind in {"all", "operations"}:
        params = {key: query[key] for key in ("q", "severity") if query.get(key)}
        params.update(view="significant", limit=100)
        if severity == "error":
            params["include_critical"] = True
        if query.get("source"):
            params["source"] = query["source"]
        try:
            records = store.events(root, params)["items"]
            from applypilot.operations.bundle import Redactor
            redactor = Redactor(root)
            for record in records:
                details = record.get("details") or {}
                source, title = operation_identity(record)
                evidence = diagnostic_details(details)
                for key in ("exception_chain", "stack", "traceback", "diagnostics", "integration",
                            "validation_errors", "reason_code", "retry_trigger", "retryable", "details_decode_error"):
                    if key in details and details[key] is not None:
                        evidence[key] = redactor.value(details[key])
                value["items"].append({"id": "operation:" + str(record["id"]), "kind": "operations",
                    "at": datetime.fromtimestamp(record["at"], timezone.utc).isoformat(),
                    "event": record.get("operation"), "title": title, "company": details.get('job_company') or '',
                    "source": source, "stage": record.get("stage"),
                    "severity": record.get("severity"), "outcome": record.get("outcome"),
                    "error_kind": record.get("error_kind"), "task_id": record.get("task_id"),
                    "reason": sanitize(record.get("message"), 4000), "details": evidence,
                    "run_id": record.get("run_id"), "span_id": record.get("span_id"),
                    "job_id": record.get("job_id"), "duration": record.get("duration")})
        except (postgres.Error, OSError, ValueError) as exc:
            value.setdefault("warnings", []).append("Operational history is unavailable: " + sanitize(exc, 400))
    entries = value.get("items", [])
    for entry in entries:
        for field in ('reason', 'message', 'summary'):
            if entry.get(field):
                original = entry[field]
                entry[field] = humanize_issue(original)
                if str(original).removeprefix('failed:') == 'application_route_unresolved':
                    entry.setdefault('details', {})['reason_code'] = 'application_route_unresolved'
                    entry['next_action'] = 'Review the job link and retry when the application form is available.'
        entry.setdefault("source", entry.get("component") or entry.get("kind") or "unknown")
        entry["source_label"] = source_label(entry["source"])
        entry.setdefault("severity", severity_for_state(entry.get("task_state") or entry.get("state")))
        if entry.get("task_state") == entry.get("state"):
            entry.pop("task_state", None)
        if entry.get("message") == entry.get("reason"):
            entry.pop("message", None)
        if entry.get("component") == entry.get("source"):
            entry.pop("component", None)
        if entry.get("kind") == "operations" and entry.get("severity") in {"warning", "error", "critical"}:
            source = entry["source"]
            connection = "linkedin" if source in {"linkedin_scraper", "linkedin_rs"} else "x" if source == "twikit" else "sources"
            if source in {"sdk", "twikit", "linkedin_scraper", "linkedin_rs", "ddgs", "searxng", "freehire"}:
                entry["action_href"] = "/ui/settings?section=connections#connection-" + connection
                entry["action_label"] = "Reconnect " + source_label(source) if entry.get("error_kind") == "auth" else "Review " + source_label(source)
    value["sources"] = sorted({str(entry["source"]) for entry in entries if entry.get("source")})
    for key in ("source", "severity"):
        if query.get(key):
            choices = {"error", "critical"} if key == "severity" and query[key] == "error" else {query[key]}
            entries = [entry for entry in entries if entry.get(key) in choices]
    entries.sort(key=activity_sort_key, reverse=True)
    value["items"] = entries[:100]
    return value

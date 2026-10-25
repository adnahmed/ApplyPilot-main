"""Read-only Overview projection: queue != in-flight work != saved output.

No workers are started here. Runtime identity and recorder freshness gate live
claims. Journal aggregates are retained operation outcomes, never lifetime job
conversions or proof of a successful application/send.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from applypilot import postgres
import time

from .data import columns, now_iso, rows, select_columns, tables
from applypilot.operations.store import reader

# One boundary per displayed unit, not nested integration/batch calls.
from applypilot.stage_catalog import STAGES, catalog
UNITS = {key: (stage, component, operation, unit)
         for key, label, stage, component, operation, unit, queue, owner in STAGES}

FRESH_SECONDS = 20


def _stamp(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.timestamp() if result.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _hash(value):
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:24]


def _error(exc):
    if getattr(exc, "sqlstate", None) in postgres.LOCK_RETRY_STATES:
        return "Database busy; this section will retry."
    if getattr(exc, "sqlstate", None) == "57014":
        return "Read exceeded its time budget; this section will retry."
    return "This section is unavailable; no zero or healthy state is inferred."


def _journal(root, runtime, now):
    result = {"available": False, "current": False, "run_id": None,
              "since": runtime.get("started_at"), "stages": {}, "active": [],
              "processes": [], "dropped": None, "write_errors": None,
              "note": "No matching current runtime report; live stage work is unknown."}
    since = _stamp(runtime.get("started_at"))
    if not (runtime.get("running") is True and runtime.get("stale") is False and since is not None):
        return result
    with reader(root) as conn:
        conn.execute("BEGIN")
        # Old bridge compatibility: match the actual reporting PID and a fresh
        # recorder. Never select whichever run happened to be most recent.
        run_id = runtime.get("operations_run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
            matches = rows(conn, "SELECT DISTINCT run_id FROM processes WHERE pid=? AND running=1 "
                           "AND heartbeat BETWEEN ? AND ?", (runtime.get("pid"), now-FRESH_SECONDS, now+5))
            if len(matches) != 1:
                result["note"] = "Runtime is reporting, but its operations recorder could not be matched."
                return result
            run_id = matches[0]["run_id"]
        processes = rows(conn, "SELECT id,role,heartbeat,running,dropped,write_errors FROM processes "
                         "WHERE run_id=? AND heartbeat>=?", (run_id, since-5))
        fresh = {p["id"] for p in processes if p["running"] == 1 and now-FRESH_SECONDS <= p["heartbeat"] <= now+5}
        if not fresh:
            result["note"] = "Runtime is reporting, but no fresh recorder heartbeat is available."
            return result
        # Read once into a bounded in-memory set. Avoid a correlated full events
        # scan for each active span (events has a run index, not a span index).
        active = rows(conn, "WITH ranked AS (SELECT a.*,ROW_NUMBER() OVER "
                      "(PARTITION BY a.stage ORDER BY a.started_at DESC) AS stage_row "
                      "FROM active a JOIN processes p ON p.id=a.process_id "
                      "WHERE a.run_id=? AND p.running=1 AND p.heartbeat BETWEEN ? AND ? "
                      "AND a.started_at BETWEEN ? AND ?) SELECT * FROM ranked "
                      "ORDER BY stage_row,stage LIMIT 201",
                      (run_id, now-FRESH_SECONDS, now+5, since, now+5))
        span_ids = [item["span_id"] for item in active]
        starts = rows(conn, "SELECT span_id,job_id,task_id FROM events WHERE run_id=? "
                      "AND kind='start' AND span_id IN (" + ",".join("?" for _ in span_ids) + ")",
                      (run_id, *span_ids)) if span_ids else []
        by_span = {e["span_id"]: e for e in starts}
        counts = rows(conn, "SELECT stage,component,operation,outcome,COUNT(*) count,MAX(at) last_at "
                      "FROM events WHERE run_id=? AND at BETWEEN ? AND ? AND kind='finish' "
                      "GROUP BY stage,component,operation,outcome", (run_id, since, now+5))
        # Aggregate ALL fresh operations independently of the bounded detail cards.
        # Otherwise 200 busy discovery/enrichment spans hide later stage activity.
        active_totals = rows(conn, "SELECT a.stage,a.component,a.operation,COUNT(*) AS count "
            "FROM active a JOIN processes p ON p.id=a.process_id "
            "WHERE a.run_id=? AND p.running=1 AND p.heartbeat BETWEEN ? AND ? "
            "AND a.started_at BETWEEN ? AND ? GROUP BY a.stage,a.component,a.operation",
            (run_id, now-FRESH_SECONDS, now+5, since, now+5))
        stage_values = {}
        for key, (stage, component, operation, unit) in UNITS.items():
            matched = [a for a in active[:200] if (a["stage"], a["component"], a["operation"]) == (stage, component, operation)]
            totals = {r["outcome"]: r["count"] for r in counts
                      if (r["stage"], r["component"], r["operation"]) == (stage, component, operation)}
            for item in matched:
                item.update({"job_id": by_span.get(item["span_id"], {}).get("job_id", ""),
                             "task_id": by_span.get(item["span_id"], {}).get("task_id", ""), "key": key})
            stage_values[key] = {"active": sum(a["count"] for a in active_totals
                    if (a["stage"], a["component"], a["operation"]) == (stage, component, operation)),
                "outcomes": totals, "unit": unit,
                "stage_open": any(a["stage"] == stage for a in active_totals)}
            result["active"].extend(matched)
        result.update(available=True, current=True, run_id=run_id, stages=stage_values,
                      processes=[{"role": p["role"], "heartbeat": p["heartbeat"],
                                  "state": "stopped" if not p["running"] else "fresh" if p["id"] in fresh else "stale"}
                                 for p in processes],
                      dropped=sum(p["dropped"] for p in processes), write_errors=sum(p["write_errors"] for p in processes),
                      truncated=len(active) > 200,
                      note="Recorded operation outcomes since this runtime started; retries count separately. "
                           "Journal retention or dropped events can make counts incomplete.")
    return result


def _inventory(store, job_ids, now, min_score):
    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    output = {"available": False, "at": now_iso(), "day": today, "timezone": "UTC", "metrics": {},
              "recent_enriched": [], "recent_scored": [], "job_labels": {}}
    with store.read() as conn:
        if "jobs" not in tables(conn):
            return output
        cols = columns(conn, "jobs")
        definitions = {
            "enriched": ("NULLIF(TRIM(full_description),'') IS NOT NULL", "detail_scraped_at", {"full_description"}),
            "scored": ("fit_score IS NOT NULL", "scored_at", {"fit_score"}),
            "qualified": ("fit_score>=?", "scored_at", {"fit_score"}),
            "tailored": ("NULLIF(tailored_resume_path,'') IS NOT NULL", "tailored_at", {"tailored_resume_path"}),
            "cover": ("NULLIF(cover_letter_path,'') IS NOT NULL", "cover_letter_at", {"cover_letter_path"}),
            "brief": ("NULLIF(interview_brief_path,'') IS NOT NULL", "brief_generated_at", {"interview_brief_path"}),
        }
        expressions, params = [], []
        for key, (condition, timestamp, required) in definitions.items():
            if not required <= cols or (key == "qualified" and min_score is None):
                output["metrics"][key] = {"total": None, "today": None}
                continue
            expressions.append(f"COALESCE(SUM(CASE WHEN {condition} THEN 1 ELSE 0 END),0) AS {key}")
            if key == "qualified":
                params.append(min_score)
            if timestamp in cols:
                expressions.append(f"COALESCE(SUM(CASE WHEN {condition} AND ap_date({timestamp})=? THEN 1 ELSE 0 END),0) AS {key}_today")
                if key == "qualified":
                    params.append(min_score)
                params.append(today)
        values = dict(conn.execute("SELECT " + ",".join(expressions) + " FROM jobs", params).fetchone()) if expressions else {}
        for key in definitions:
            output["metrics"][key] = {"total": values.get(key), "today": values.get(key+"_today")}
        safe_columns = ("url", "title", "company", "site", "fit_score", "detail_scraped_at", "scored_at")
        for key, field, condition in (("recent_enriched", "detail_scraped_at", definitions["enriched"][0]),
                                      ("recent_scored", "scored_at", definitions["scored"][0])):
            required = definitions["enriched" if key == "recent_enriched" else "scored"][2]
            if field in cols and required <= cols:
                output[key] = rows(conn, "SELECT " + select_columns(conn, "jobs", safe_columns)
                                   + f" FROM jobs WHERE {condition} AND NULLIF({field},'') IS NOT NULL "
                                   + f"ORDER BY {field} DESC,url LIMIT 5")
        if job_ids and "url" in cols:
            # Same digest as operations.observe. Only requested active IDs are
            # resolved; neither descriptions nor document paths leave this read.
            names = select_columns(conn, "jobs", ("url", "title", "company", "site", "fit_score"))
            records = rows(conn, "SELECT " + names + " FROM jobs WHERE ap_public_id(url) IN ("
                           + ",".join("?" for _ in job_ids) + ")", tuple(job_ids))
            output["job_labels"] = {_hash(row["url"]): row for row in records}
            # Enrichment sometimes observes the resolved navigation URL rather
            # than the saved key. A unique saved application URL is a safe
            # fallback; shared company/application URLs must not guess a job.
            unresolved = job_ids - output["job_labels"].keys()
            if unresolved and "application_url" in cols:
                aliases = rows(conn, "SELECT " + names + ",ap_public_id(application_url) AS digest "
                    "FROM jobs WHERE application_url IS NOT NULL AND ap_public_id(application_url) IN ("
                    + ",".join("?" for _ in unresolved) + ")", tuple(unresolved))
                by_digest = {}
                for item in aliases:
                    by_digest.setdefault(item.pop("digest"), []).append(item)
                output["job_labels"].update({key: items[0] for key, items in by_digest.items() if len(items) == 1})
        try:
            from applypilot.discovery.diagnostics import scoring_breakdown
            output["scoring_admission"] = scoring_breakdown(conn)
        except (postgres.Error, ValueError) as exc:
            output["scoring_admission"] = {"available": False, "error": _error(exc)}
        output["available"] = True
    return output


def _sources(store, now):
    output = {"available": False, "at": now_iso(), "health": [], "tasks": [], "current_tasks": [],
              "context": None, "task_counts_available": False, "truncated": False}
    with store.read() as conn:
        ts = tables(conn)
        if conn is None:
            return output
        if "source_health" in ts:
            needed = {"source_name", "status", "last_run_at", "last_success_at", "jobs_found", "jobs_stored", "consecutive_failures"}
            if needed <= columns(conn, "source_health"):
                output["health"] = rows(conn, "SELECT " + ",".join(sorted(needed)) + " FROM source_health ORDER BY source_name LIMIT 251")
                output["truncated"] = len(output["health"]) > 250
                output["health"] = output["health"][:250]
        if "discovery_active_context" in ts:
            context = conn.execute("SELECT context_key FROM discovery_active_context WHERE singleton_id=1").fetchone()
            output["context"] = context[0] if context else None
        output["available"] = "source_health" in ts
        output["execution_owner"] = "Temporal"
    return output


def _saved_service(root, name, now, since):
    """Saved diagnostics explain admission; they never prove a live task."""
    from applypilot.onboarding.storage import read_json
    from applypilot.operations.runtime import sanitize
    if name == "learning":
        return {"reason": "Learning progress is recorded in Temporal workflow history."}
    path = root / "github_sync/account-state.json"
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("redirected service diagnostics")
        saved = read_json(path, {}, limit=256_000)
        if not isinstance(saved, dict):
            raise ValueError("invalid service diagnostics")
    except (OSError, ValueError, TypeError):
        return {"reason": "Saved service diagnostics are unavailable; controller liveness does not establish service health."}
    if not saved:
        return {}
    progress_at = saved.get("progress_at")
    if (saved.get("status") == "running" and type(progress_at) in (int, float)
            and now - 60 <= progress_at <= now + 5):
        from applypilot.backup.common import operation_busy
        if operation_busy(root):
            return {"state": "running", "active_tasks": 1,
                    "reason": sanitize(saved.get("progress") or "Backup operation is executing.", 400),
                    "phase": saved.get("phase"), "last_result_scope": "saved_workspace",
                    "last_verified_at": saved.get("snapshot_time")}
    at = saved.get("failed_at") if saved.get("status") in {"error", "authentication_required"} else saved.get("checked_at")
    retry = saved.get("retry_at")
    backing_off = type(retry) in (int, float) and retry > now
    failed = saved.get("status") in {"error", "authentication_required"}
    reason = ("Latest saved backup failed: " + str(saved.get("error") or saved.get("error_code") or "Inspect backup details.")
              if failed else "Latest saved backup: " + str(saved.get("status") or "not observed").replace("_", " "))
    if backing_off:
        reason += " Automatic retry is waiting for its saved deadline."
    return {"state": "backoff" if backing_off else "error" if failed else "waiting",
            "reason": sanitize(reason, 400), "last_result_at": at,
            "last_result_scope": "saved_workspace",
            "retry_at": retry, "last_verified_at": saved.get("snapshot_time"),
            "saved_status": saved.get("status")}


def _stage_results(root, runtime):
    from applypilot.operations.runtime import sanitize
    run_id = runtime.get("operations_run_id")
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        return {}
    boundaries = {boundary[:3]: key for key, boundary in UNITS.items()}
    match = " OR ".join("(stage=? AND component=? AND operation=?)" for _ in boundaries)
    parameters = [run_id, *(part for boundary in boundaries for part in boundary)]
    try:
        with reader(root) as conn:
            events = rows(conn, "WITH latest AS (SELECT stage,component,operation,outcome,message,at, "
                          "ROW_NUMBER() OVER (PARTITION BY stage,component,operation ORDER BY at DESC,id DESC) AS position "
                          "FROM events WHERE run_id=? AND kind='finish' AND (" + match + ")) "
                          "SELECT stage,component,operation,outcome,message,at FROM latest WHERE position=1", parameters)
    except (postgres.Error, OSError, ValueError, TypeError):
        return {}
    result = {}
    for event in events:
        key = boundaries[(event["stage"], event["component"], event["operation"])]
        result[key] = {"last_result": event["outcome"], "last_result_at": event["at"],
                       "last_result_scope": "current_run", "last_result_reason": sanitize(event["message"], 400)}
    return result


def _scheduler(root, runtime, now, report=None):
    """All stages are represented even with no spans, no work or no recorder."""
    from applypilot.engine_status import read_engine_heartbeat
    parent_current = runtime.get("running") is True and runtime.get("stale") is False
    runtime_id = runtime.get("runtime_id")
    report = ((report or read_engine_heartbeat(root, runtime_id=runtime_id, now=now))
              if parent_current and isinstance(runtime_id, str) and runtime_id else None)
    retained = {}
    if parent_current and runtime_id and report is None:
        from applypilot.live_status import read_component
        saved = read_component(root, "preparation", runtime_id=runtime_id) or {}
        stamp = saved.get("at")
        if saved.get("runtime_id") == runtime_id and type(stamp) in (int, float) and 0 < stamp <= now + 5:
            retained = saved
    allowed = {"state", "checked_at", "passes", "workers", "pending", "reason", "enabled",
               "batch_started_at", "batch_finished_at", "thread_alive", "last_error_at", "last_result",
               "configured_workers", "effective_workers",
               "queue_checked_at", "active_tasks", "queued_tasks", "completed",
               "oldest_runnable_age", "completion_rate", "policy_source", "enabled_source",
               "companies_source", "legacy_environment_override_ignored", "flow_mode",
               "phase", "last_result_at", "last_result_scope", "last_result_reason", "retry_at",
               "last_verified_at", "saved_status", "completed_tasks", "failed_tasks", "stopped_tasks",
               "last_task_finished_at", "duration_seconds", "oldest_queue_seconds",
               "priority", "dependency_waits", "resource_waits", "thread_waits", "buffer", "output_buffers",
               "submitted_tasks", "scheduler_no_worker", "resource_restricted_tasks",
               "wait_seconds_median", "wait_seconds_p95", "runtime_seconds_median", "runtime_seconds_p95",
               "finished_last_60_seconds", "dask_retries",
               "ready_backlog", "deferred_backlog", "repair_tasks", "advisory_lock_wait_seconds", "provider_wait_seconds", "sample_at"}
    own = (report or retained).get("stages", {})
    external = runtime.get("services", {}) if parent_current else {}
    execution = runtime.get("execution") if parent_current else None
    if not isinstance(execution, dict) or execution.get("runtime_id") != runtime_id:
        execution = {}
    observed = execution.get("stages") or {}
    results = _stage_results(root, runtime) if parent_current else {}
    since = _stamp(runtime.get("started_at"))
    stages = {}
    for row in catalog():
        key = row["id"]
        # Referral planning can be owned by the engine or the resident networking
        # dispatcher. Report the actual configured owner rather than hiding it.
        source = own.get(key) or external.get(key)
        if source is None and key in observed:
            source = {"state": "waiting", "reason": "No task is currently scheduled for this stage.",
                      "checked_at": now}
        if isinstance(source, dict):
            source = dict(source)
            source.update(results.get(key, {}))
            if key in {"learning", "backup"} and source.get("enabled") is not False:
                controller_state = source.get("state")
                source.update(_saved_service(root, key, now, since))
                if controller_state in {"stopped", "unknown"}:
                    source.update(state=controller_state, reason="Controller " + controller_state + ". " + source.get("reason", ""))
                if key == "backup" and source.get("active_tasks"):
                    source.update(state="running", reason="Backup process is executing; saved checkpoints are reported separately.")
            activity = observed.get(key)
            if isinstance(activity, dict):
                source.update(activity)
                source.update(sample_at=execution.get("sampled_at"))
                if activity.get("active_tasks", 0) > 0:
                    source.update(state="running", reason="Task executing" +
                                  (": " + str(source["phase"]).replace("_", " ") if source.get("phase") else "."))
                elif activity.get("queued_tasks", 0) > 0:
                    source.update(state="queued", reason="Waiting for an execution slot or required resource.")
                if (source.get("buffer") or {}).get("paused"):
                    source["reason"] = "New work paused. " + source["buffer"]["reason"]
            elif execution and key in external and key != "backup":
                source.update(active_tasks=0, queued_tasks=0)
            if (source.get("state") in {"waiting", "scheduled", "error"} and source.get("last_result") in {"deferred", "failed", "partial"}
                  and (source.get("buffer") or {}).get("available") is not False):
                source.update(state="error" if source["last_result"] == "failed" else "waiting",
                              reason=source.get("last_result_reason") or "Last pass produced no completed outcome.")
        if key == "engine" and report:
            source = {"state": "running", "thread_alive": True,
                      **{field: report.get(field) for field in
                         ("configured_workers", "effective_workers", "active_tasks", "queued_tasks",
                          "completed", "oldest_runnable_age", "completion_rate")},
                      "reason": "Dependency-driven execution with bounded producer buffers.", "checked_at": report["at"]}
        if not isinstance(source, dict):
            source = {"state": "not_selected" if report and row["owner"] == "engine" else "unknown",
                      "reason": "Not selected in this engine invocation." if report and row["owner"] == "engine"
                                else "No fresh service report; absence of work is not a health check."}
        stages[key] = {k: v for k, v in source.items() if k in allowed}
    if "network_sync" in observed:
        activity = observed["network_sync"]
        stages["network_sync"] = {**activity,
            "state": "running" if activity.get("active_tasks") else "queued" if activity.get("queued_tasks") else "waiting",
            "reason": "Shared mailbox and delivery task; channel results are recorded separately."}
    if parent_current and runtime.get("workers"):
        workers = runtime["workers"]
        active = sum(row.get("status") == "applying" for row in workers)
        from applypilot.web.application_health import application_health
        stages["applications"].update(application_health(runtime, active=active, now=now))
        stages["applications"]["workers"] = len(workers)
    return {"current": bool(report or execution), "parent_current": parent_current,
            "mode": execution.get("scheduler") or (report.get("mode") if report else "unknown"),
            "at": execution.get("sampled_at") if execution else report.get("at") if report else None,
            "stages": stages, "flow_control": report.get("flow_control") if report else None,
            "execution": execution or None}


def engine_snapshot(root, store, runtime):
    from .run_evidence import run_totals, recovery_snapshot
    now = time.time()
    value = {"at": now_iso(), "runtime_id": runtime.get("runtime_id"), "runtime_at": runtime.get("at"),
             "runtime_current": runtime.get("running") is True and runtime.get("stale") is False,
             "warnings": [], "stage_catalog": catalog()}
    value["current_run"] = run_totals(root, store, runtime)
    value["recovery"] = recovery_snapshot(store)
    value["scheduler"] = _scheduler(root, runtime, now)
    try:
        value["journal"] = _journal(root, runtime, now)
    except (postgres.Error, OSError, ValueError, TypeError) as exc:
        value["journal"] = {"available": False, "current": False, "stages": {}, "active": [], "note": _error(exc)}
        value["warnings"].append("Operation telemetry: " + _error(exc))
    if value["runtime_current"]:
        workers = [w for w in runtime.get("workers", []) if isinstance(w, dict)]
        application = value["journal"]["stages"].setdefault("applications", {"outcomes": {}, "unit": "attempts"})
        application["active"] = sum(w.get("status") == "applying" for w in workers)
        application["stage_open"] = bool(workers)
        application["activity_source"] = "runtime_workers"
    job_ids = {a.get("job_id") for a in value["journal"]["active"] if a.get("job_id")}
    min_score = runtime.get("pipeline", {}).get("min_score")
    if type(min_score) not in (int, float) or not 0 <= min_score <= 10:
        min_score = None
    value["min_score"] = min_score
    try:
        value["inventory"] = _inventory(store, job_ids, now, min_score)
    except (postgres.Error, OSError, ValueError, TypeError) as exc:
        value["inventory"] = {"available": False, "metrics": {}, "recent_enriched": [], "recent_scored": [], "job_labels": {}}
        value["warnings"].append("Saved job progress: " + _error(exc))
    labels = value["inventory"].pop("job_labels", {})
    for active in value["journal"]["active"]:
        active["job"] = labels.get(active.get("job_id"))
        active.pop("process_id", None)
    try:
        value["sources"] = _sources(store, now)
    except (postgres.Error, OSError, ValueError, TypeError) as exc:
        value["sources"] = {"available": False, "health": [], "tasks": [], "current_tasks": []}
        value["warnings"].append("Source checkpoints: " + _error(exc))
    task_labels = {t["task_id"]: t for t in value["sources"].get("current_tasks", [])}
    for active in value["journal"]["active"]:
        task = task_labels.get(active.get("task_id"))
        if task:
            active["source"] = {key: task.get(key) for key in ("source_key", "query", "location", "kind")}
    return value

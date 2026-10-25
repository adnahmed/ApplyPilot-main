"""Cheap, authenticated Workers projection. No queries against the jobs DB.

Snapshots are published by actual processes; worker count is never synthesized
from configured pool ceilings. A stale process is unknown, not idle or running.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import time

from applypilot.operations.runtime import sanitize
from .runtime import read_runtime

FRESH_SECONDS = 20
ROW_FIELDS = {"key", "pid", "worker", "thread_name", "kind", "stage", "operation", "span_id",
    "job_id", "job_title", "company", "source", "active", "state", "phase", "started_at", "phase_at",
    "progress_at", "completed_operations", "failed_operations", "partial_operations", "last_outcome", "last_issue",
    "execution_alive", "last_job_title", "last_company", "model_task", "warning_events",
    "model_call_id", "model_call_started_at", "model_call_state", "form_progress_at",
    "model_activity_at", "model_wait_since", "transport_at", "runtime_id"}
APPLICATION_REASONS = {
    "initializing": "Initializing application worker",
    "selecting": "Checking browser preflight and job selection",
    "no_selectable_job": "No job passed this worker's current selection checks. This is not a count of all stored jobs.",
    "browser_unavailable": "Browser preflight failed; no application was opened",
    "linkedin_cooldown": "Waiting for the configured LinkedIn cooldown",
    "queue_empty": "This worker's finite run found no next job",
    "queued": "Awaiting execution; consult Temporal for live workflow state",
    "finished": "Worker finished its run",
}


def selection_snapshot(root, worker, run_id, now):
    """Read a bounded, matching saved diagnostic. Never query PostgreSQL from GET."""
    from applypilot.apply.selection_diagnostics import LABELS
    policy_id = str(worker.get("selection_policy_id", ""))
    if not re.fullmatch(r"[a-f0-9]{24}", policy_id):
        return None
    folder=root/"operations"/"selection-snapshots"
    path=folder/(policy_id+".json")
    try:
        if any(p.is_symlink() for p in (root,root/"operations",folder,path)) or path.stat().st_size>128*1024:
            return None
        payload=json.loads(path.read_text(encoding="utf-8"))
        if payload.get("run_id")!=run_id or payload.get("policy_id")!=policy_id:
            return None
        raw=payload.get("snapshot") or {}
        at=_timestamp(raw.get("captured_at"))
        if at is None or at>now+5:
            return None
        result={"diagnostic_id":payload.get("diagnostic_id"),"policy_id":policy_id,
                "captured_at":at,"stale":now-at>180,"available":raw.get("available") is True,
                "scope":"Read-only snapshot of jobs meeting this worker minimum score, or explicit target; not a reservation",
                "reason":sanitize(raw.get("reason"),80),"elapsed_seconds":raw.get("elapsed_seconds"),
                "samples_truncated":raw.get("samples_truncated") is True}
        if not result["available"]:return result
        for k in ("stored_jobs","cohort_count","passing_common_checks","allocated_preview",
                  "optimistic_assumption_jobs"):
            n=raw.get(k)
            result[k]=n if type(n) is int and 0<=n<=2**53 else None
        result["first_blockers"]=[{"code":k,"label":LABELS[k],"count":n} for k,n in
                                  (raw.get("first_blockers") or {}).items()
                                  if k in LABELS and type(n) is int and 0<=n<=2**53]
        result["samples"]=[]
        for sample in (raw.get("samples") or [])[:12]:
            if not isinstance(sample,dict) or not re.fullmatch(r"[a-f0-9]{24}",str(sample.get("job_id",""))):continue
            code=sample.get("reason")
            if code not in LABELS:continue
            result["samples"].append({"job_id":sample["job_id"],"job_title":sanitize(sample.get("job_title"),160),
                                      "company":sanitize(sample.get("company"),120),"reason":code,"label":LABELS[code]})
        result["optimistic_assumptions"]=[{"code":sanitize(k,80),"count":n} for k,n in
                                          (raw.get("optimistic_assumptions") or {}).items()
                                          if re.fullmatch(r"[a-z][a-z0-9_.:-]{0,79}",str(k))
                                          and type(n) is int and 0<=n<=2**53]
        allocation=raw.get("allocation") or {}
        result["allocation"]={}
        for key in ("active_context","finite_batch","reallocate_unused_to_preferred","fill_idle_capacity"):
            if type(allocation.get(key)) is bool:result["allocation"][key]=allocation[key]
        for key in ("exploration_share","available_credit"):
            v=str(allocation.get(key,""))
            if re.fullmatch(r"-?[0-9]{1,15}(?:/[0-9]{1,15})?",v):result["allocation"][key]=v
        return result
    except (OSError,ValueError,TypeError,AttributeError):
        return None


def _timestamp(value):
    try:
        if type(value) in {int, float}:
            return float(value) if math.isfinite(value) else None
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _nonnegative(value, *, integer=False):
    if type(value) not in ({int} if integer else {int, float}) or not math.isfinite(value) or value < 0:
        return None
    return int(value) if integer else float(value)





def snapshot(root: Path, runtime=None, *, now=None):
    now = time.time() if now is None else now
    runtime = read_runtime(root) if runtime is None else runtime
    if runtime.get('execution_owner') == 'Temporal':
        from .temporal_status import workers
        return workers(root, runtime)
    current = runtime.get("running") is True and runtime.get("stale") is False
    run_id = runtime.get("operations_run_id")
    value = {"format": "applypilot_workers_report", "format_version": 1,
             "at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
             "runtime_id": runtime.get("runtime_id"), "run_id": run_id,
             "runtime_current": current, "runtime_at": runtime.get("at"),
             "runtime_started_at": runtime.get("started_at"),
             "rows": [], "warnings": [], "truncated": False,
             "resources": {"application": {"configured_workers": None, "effective_workers": None,
                                             "active_tasks": None},
                           "preparation": {"available": False}},
             "note": "Actual reported execution slots, not the configured worker ceiling. Heartbeat age is not task progress. "
                     "Completed operations and worker-reported applications are not independent submission verification."}
    from .worker_history import history
    from .pipeline_flow import snapshot as pipeline_snapshot
    value["history"] = history(root)
    # Stopped/stale parent reports never resurrect child work as current.
    if not current:
        value["pipeline"] = pipeline_snapshot(root, runtime, now=now)
        value["warnings"].append("No fresh, matching Autopilot report. Current worker activity is unknown.")
        value["counts"] = {}
        return value
    application_workers = [w for w in runtime.get("workers", [])
                           if isinstance(w, dict) and type(w.get("worker_id")) is int]
    try:
        from applypilot.apply.capacity import snapshot as capacity_snapshot
        capacity = capacity_snapshot()
    except (OSError, ValueError, TypeError):
        capacity = {}
    if capacity.get("runtime_id") != runtime.get("runtime_id"):
        capacity = {}
    value["resources"]["application"] = {
        "configured_workers": _nonnegative(capacity.get("configured_workers"), integer=True),
        "effective_workers": _nonnegative(capacity.get("effective_workers"), integer=True),
        "target_workers": _nonnegative(capacity.get("target_workers"), integer=True),
        "reported_worker_slots": len(application_workers),
        "active_tasks": sum(w.get("execution_active", w.get("status") == "applying") is True
                            for w in application_workers),
        "idle_workers": sum(w.get("status") == "idle" for w in application_workers),
        "external_workers": sum(w.get("role") == "external" for w in application_workers),
        "linkedin_workers": sum(w.get("role") == "linkedin" for w in application_workers),
        "source": "fresh application worker reports",
    }
    for key in ("mode", "reason"):
        value["resources"]["application"][key] = sanitize(capacity.get(key), 200)
    for source, target in (("jobs_applied", "reported_applied"), ("jobs_failed", "reported_failed")):
        counts = [_nonnegative(w.get(source), integer=True) for w in application_workers]
        value["resources"]["application"][target] = (
            sum(counts) if counts and all(n is not None for n in counts) else None
        )
    try:
        from applypilot.engine_status import read_engine_heartbeat
        engine = read_engine_heartbeat(root, runtime_id=runtime.get("runtime_id"), now=now)
    except (OSError, ValueError, TypeError):
        engine = None
    from .engine import _scheduler
    scheduler = _scheduler(root, runtime, now, report=engine)
    execution = scheduler.get("execution")
    if engine or execution:
        prepared = {"available": bool(engine), "source": "fresh preparation worker runtime" if engine else
                    "Preparation inventory is updating; execution counts come from the scheduler."}
        engine = engine or {}
        for key in ("configured_workers", "effective_workers", "active_tasks",
                    "queued_tasks", "completed"):
            prepared[key] = _nonnegative(engine.get(key), integer=True)
        for key in ("oldest_runnable_age", "completion_rate"):
            prepared[key] = _nonnegative(engine.get(key))
        stage_reports = engine.get("stages")
        prepared["stages"] = {
            sanitize(name, 80): {
                "state": sanitize(stage.get("state"), 80),
                "reason": sanitize(stage.get("reason"), 400),
                "active_tasks": _nonnegative(stage.get("active_tasks"), integer=True),
                "pending": _nonnegative(stage.get("pending", stage.get("queued_tasks")), integer=True),
                "completed": _nonnegative(stage.get("completed"), integer=True),
                "thread_alive": stage.get("thread_alive") is True,
            }
            for name, stage in (stage_reports if isinstance(stage_reports, dict) else {}).items()
            if isinstance(stage, dict)
        }
        value["resources"]["preparation"] = prepared
        value["resources"]["stages"] = scheduler["stages"]
        value["resources"]["execution"] = execution if isinstance(execution, dict) else {}
        if isinstance(execution, dict):
            value["warnings"].extend(sanitize(warning, 240) for warning in execution.get("warnings", [])
                                     if isinstance(warning, str))
    else:
        value["warnings"].append("No fresh matching preparation resource report. Preparation capacity and throughput are unknown.")
    selection_cache={}
    for w in application_workers:
        if not isinstance(w, dict) or type(w.get("worker_id")) is not int:
            continue
        status = str(w.get("status", "unknown"))
        state = {"applying": "running", "idle": "idle", "done": "stopped",
                 "applied": "completed", "failed": "failed"}.get(status, "unknown")
        reason = APPLICATION_REASONS.get(w.get("status_reason"), "No detailed wait reason in this worker's report" if state == "idle" else status.replace("_", " "))
        if w.get("status_reason") == "queued":
            state = "waiting"
        policy_id=str(w.get("selection_policy_id", ""))
        if policy_id not in selection_cache:
            selection_cache[policy_id]=selection_snapshot(root,w,run_id,now)
        selection=selection_cache[policy_id]
        if w.get("status_reason")=="no_selectable_job" and selection and not selection.get("stale"):
            if not selection.get("available"):
                reason="Last selection returned no candidate; detailed diagnosis is unavailable (not an empty-inventory count)."
            elif selection.get("reason")=="allocation_wait":
                reason="Last selection returned no candidate; a later snapshot has candidates waiting for allocation allowance."
            elif selection.get("allocated_preview"):
                reason="Last selection returned no candidate; a newer snapshot now finds a candidate. Awaiting the next worker check."
            elif selection.get("first_blockers"):
                top=max(selection["first_blockers"],key=lambda x:x["count"])
                reason=f"No candidate returned. Latest snapshot: {top['label']} ({top['count']} of {selection.get('cohort_count')} in this score/target cohort)."
        busy = status == "applying"
        executing = w.get("execution_active", busy) is True
        waiting_model = busy and (_timestamp(w.get("model_wait_since")) or 0) > 0
        if waiting_model:
            state = "waiting_provider"
            reason = "Awaiting application agent response."
        elif executing and state in {"idle", "waiting"}:
            state = "running"
            reason = "Application task is executing."
        value["rows"].append({"key": "application:" + str(w.get("worker_id")), "kind": "application", "worker_id": w['worker_id'],
            "worker": "Application " + str(w.get("worker_id")), "stage": "applications", "state": state,
            "phase": "waiting_provider" if waiting_model else str(w.get("status_reason") or status),
            "reason": reason, "active": busy or executing,
            "executing": executing,
            "trace_id": w.get("trace_id") if re.fullmatch(r"[a-f0-9]{32}", str(w.get("trace_id", ""))) else None,
            "runtime_id": runtime.get("runtime_id"), "pid": _nonnegative(w.get("pid"), integer=True),
            "admission_reason": sanitize(w.get("admission_reason"), 200),
            "job_title": sanitize(w.get("job_title"), 160) if busy else "", "company": sanitize(w.get("company"), 120) if busy else "",
            "last_job_title": sanitize(w.get("job_title"), 160) if not busy else "",
            "score": w.get("score") if busy else None,
            "started_at": _timestamp(w.get("start_time")) if busy else _timestamp(w.get("state_since")),
            "phase_at": _timestamp(w.get("state_since")), "progress_at": _timestamp(w.get("progress_at")),
            "form_progress_at": _timestamp(w.get("form_progress_at")),
            "model_activity_at": _timestamp(w.get("model_activity_at")),
            "model_wait_since": _timestamp(w.get("model_wait_since")),
            "transport_at": _timestamp(w.get("transport_at")),
            "sample_at": _timestamp(runtime.get("at")), "fresh": True,
            "reported_applied": w.get("jobs_applied"), "reported_failed": w.get("jobs_failed"),
            "actions": w.get("actions"), "last_issue": None, "selection":selection,
            "selection_checked_at":_timestamp(w.get("selection_checked_at")),
            "selection_polls":w.get("selection_polls"),"selection_empty_polls":w.get("selection_empty_polls"),
            "selection_result":w.get("selection_result") if w.get("selection_result") in {"empty","selected","error"} else None,
            "selected_runtime":w.get("selected_runtime") if w.get("selected_runtime") in {"browseros","playwright"} else None})
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        value["warnings"].append("This runtime has no operations-run identity; preparation workers cannot be matched.")
    else:
        stamp = _timestamp(runtime.get("at"))
        fresh = stamp is not None and -5 <= now - stamp <= FRESH_SECONDS
        activity = runtime.get("worker_activity", [])
        for row in activity:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key", ""))
            if (not re.fullmatch(r"[a-f0-9]{32}:[a-f0-9]{12}", key)
                    or key.split(":", 1)[0] != row.get("process_id")
                    or row.get("pid") != runtime.get("pid")):
                continue
            item = {k: v for k, v in row.items() if k in ROW_FIELDS and not isinstance(v, (dict, list))}
            issue = row.get("last_issue")
            if isinstance(issue, dict):
                from applypilot.operations.worker_status import ISSUE_LABELS
                code = issue.get("code") if issue.get("code") in ISSUE_LABELS else "unknown"
                safe_issue = {"code": code, "at": _timestamp(issue.get("at")), "summary": ISSUE_LABELS[code]}
                if issue.get("message"):
                    safe_issue["message"] = sanitize(issue["message"], 1800)
                    safe_issue["summary"] = safe_issue["message"]
                # Never copy arbitrary message/body/summary values from telemetry.
                # Rebuild the summary from code-owned labels and safe identifiers.
                for field in ("component", "operation", "exception_type", "origin", "missing_field"):
                    text = str(issue.get(field, ""))
                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.:-]{0,180}", text):
                        safe_issue[field] = text
                for field in ("span_id", "llm_call_id"):
                    if re.fullmatch(r"[a-f0-9]{32}", str(issue.get(field, ""))):
                        safe_issue[field] = issue[field]
                if issue.get("response_log") == "logs/llm-responses":
                    safe_issue["response_log"] = issue["response_log"]
                if issue.get("severity") in {"warning", "error", "critical"}:
                    safe_issue["severity"] = issue["severity"]
                if issue.get('resolved') is True:
                    safe_issue.update(resolved=True, severity='info')
                if code == "document_latex_missing_field" and safe_issue.get("missing_field"):
                    safe_issue["summary"] += ": " + safe_issue["missing_field"]
                if code in {"document_latex_missing_field", "document_renderer_fallback"} and issue.get("document_completed_after_warning") is True:
                    safe_issue["document_completed_after_warning"] = True
                    safe_issue["summary"] += "; document subsequently completed"
                if code == "unknown" and not safe_issue.get("message"):
                    safe_issue["summary"] = " / ".join(safe_issue.get(k) or sanitize(row.get("stage") if k == "component" else row.get(k), 80) for k in ("component", "operation"))
                    safe_issue["summary"] += (" raised " + safe_issue["exception_type"] if safe_issue.get("exception_type") else
                                              " reported a warning/failure; inspect its run record")
                item["last_issue"] = safe_issue
            else:
                item["last_issue"] = None
            for field in ("started_at", "phase_at", "progress_at", "model_call_started_at",
                          "form_progress_at", "model_activity_at", "model_wait_since", "transport_at"):
                item[field] = _timestamp(item.get(field))
            for field in ("pid", "completed_operations", "failed_operations", "partial_operations", "warning_events"):
                n = item.get(field)
                item[field] = n if type(n) is int and 0 <= n <= 2**53 else None
            if not re.fullmatch(r"[a-f0-9]{32}", str(item.get("model_call_id", ""))):
                item.pop("model_call_id", None)
            if item.get("model_call_state") not in {"requesting", "response_received", "failed", "cancelled", "deferred"}:
                item.pop("model_call_state", None)
            if item.get("kind") not in {"preparation", "service"}:
                item["kind"] = "service"
            for field in ("stage", "phase", "operation", "last_outcome", "model_task"):
                item[field] = sanitize(item.get(field), 80)

            for k in ("job_title", "last_job_title", "company", "last_company", "source", "thread_name", "worker"):
                if k in item:
                    item[k] = sanitize(item[k], 160)
            item.update(fresh=fresh, sample_at=stamp, runtime_id=runtime.get("runtime_id"))
            if row.get("execution_alive") is False:
                item.update(state="stopped", active=False, reason="Execution slot has ended")
            elif not fresh or row.get("execution_alive") is not True:
                item.update(state="unverified", active=False,
                            reason="This process stopped reporting; the last task is not shown as current work.")
            elif item.get("state") == "idle":
                item["reason"] = "Previous operation ended; this live execution slot is waiting for its next task"
            value["rows"].append(item)
        if not activity:
            value["warnings"].append("No per-thread preparation activity has been reported by this runtime yet.")
    value["counts"] = dict(Counter(row["state"] for row in value["rows"]))
    value["pipeline"] = pipeline_snapshot(root, runtime, report=engine, scheduler=scheduler,
                                          workers=value["rows"], now=now)
    value["warnings"] = list(dict.fromkeys(value["warnings"]))
    return value

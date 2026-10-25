"""Bounded, read-only business work and confirmed outcome projections.

Journal completion is an operation result. Only committed domain records establish
submission, delivery, or a secured referral. Historical gaps stay explicit.
"""
from __future__ import annotations

import base64
import json
import math
import re
from applypilot import postgres
import threading
import time
from collections import OrderedDict

from applypilot.operations import store as journal
from applypilot.operations.runtime import sanitize
from applypilot.stage_catalog import STAGES
from .data import columns, integer, now_iso, rows, tables
from .presentation import humanize_issue
from .run_evidence import run_totals as _run_totals, recovery_snapshot
from .work_activity import activity_counts


_EXCLUDED = {"engine", "autopilot", "unattended", "referrals", "applications"}
_BOUNDARIES = [(stage, component, operation) for key, _, stage, component, operation, *_ in STAGES
               if key not in _EXCLUDED]
_BOUNDARIES += [("referrals", "referrals", "prepare_job")]
_ACTIONS = {(component, operation): label for _, label, _, component, operation, *_ in STAGES}
_ACTIONS[("referrals", "prepare_job")] = "Prepare referral"
_FAILURES = {"failed", "partial"}
_GENERIC = {"failed_validation", "failed_judge", "exhausted_retries", "Completed",
            "failed", "partial", "error", "unknown", "Batch contains item errors",
            "Operation completed with errors or incomplete coverage",
            "One or more stages did not complete cleanly; inspect child operations"}
_cache_lock = threading.Lock()
_targets = OrderedDict()
_commits = OrderedDict()


def _cached(cache, key):
    with _cache_lock:
        item = cache.get(key)
        if item and item[0] > time.monotonic():
            cache.move_to_end(key)
            return True, item[1]
        cache.pop(key, None)
    return False, None


def _remember(cache, key, value):
    with _cache_lock:
        cache[key] = (time.monotonic() + (300 if value else 5), value)
        cache.move_to_end(key)
        while len(cache) > 4000:
            cache.popitem(last=False)


def _cursor(value):
    if not value:
        return time.time() + 5, "~"
    try:
        if len(value) > 400:
            raise ValueError()
        at, key = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if not isinstance(at, (int, float)) or not math.isfinite(at) or not isinstance(key, str):
            raise ValueError()
        return at, key[:160]
    except (ValueError, TypeError, json.JSONDecodeError):
        raise ValueError("Invalid work page cursor") from None


def _encode(row):
    return base64.urlsafe_b64encode(json.dumps([row["at"], row["id"]]).encode()).decode().rstrip("=")


def _stamp(value):
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0


def _diagnostic(event):
    details = event.get("details") or {}
    chain = details.get("exception_chain") or []
    if chain:
        return "; caused by ".join(f"{sanitize(e.get('type'), 100)}: {sanitize(e.get('message'))}"
                                   for e in chain[:4] if isinstance(e, dict))
    errors = details.get("validation_errors") or []
    if errors:
        return "; ".join(sanitize(e, 700) for e in errors[:8])
    message = str(event.get("message") or "")
    if message and message.casefold() not in {v.casefold() for v in _GENERIC}:
        return sanitize(humanize_issue(message))
    return "Original error was not recorded."


def _event_row(event):
    details = journal.decoded_details(event.get("details")) if isinstance(event.get("details"), str) else event.get("details") or {}
    event = {**event, "details": details}
    failed = event.get("outcome") in _FAILURES or event.get("severity") in {"error", "critical"}
    return {"id": "event:" + str(event["id"]), "at": event["at"], "source": "operations",
            "action": _ACTIONS.get((event["component"], event["operation"]), f"{event['component']} / {event['operation']}"), "outcome": event["outcome"],
            "message": _diagnostic(event) if failed or details.get("exception_chain") else sanitize(event.get("message") or "Operation completed"),
            "target": details.get("job_title") or details.get("source") or event.get("task_id") or "",
            "company": details.get("job_company") or "", "job_id": event.get("job_id"),
            "stage": event["stage"], "run_id": event["run_id"], "span_id": event["span_id"],
            "task_id": event.get("task_id"),
            "origin": details.get("error_origin") or details.get("origin") or f"{event['component']} / {event['operation']}",
            "model": details.get("model"), "duration": event.get("duration"),
            "next_eligible_at": details.get("next_eligible_at"),
            "saved_outcome": False, "evidence": event}


def _journal_rows(root, before, limit):
    boundary = " OR ".join("(stage=? AND component=? AND operation=?)" for _ in _BOUNDARIES)
    args = [v for entry in _BOUNDARIES for v in entry]
    with journal.reader(root) as conn:
        result = rows(conn, "SELECT e.* FROM events e WHERE (at<? OR (at=? AND 'event:'||id<?)) AND ((kind='finish' AND (" + boundary +
                      ")) OR (span_id='' AND kind='signal' AND severity IN ('error','critical'))) "
                      "AND outcome NOT IN ('skipped','empty') "
                      "AND NOT (stage='backup' AND outcome='deferred' AND message='backoff') "
                      "AND NOT (component='pdf' AND EXISTS(SELECT 1 FROM events p WHERE p.run_id=e.run_id "
                      "AND p.span_id=e.parent_span_id AND p.kind='start' AND p.component IN ('tailor','cover') AND p.operation='job')) "
                      "ORDER BY at DESC,CAST(id AS TEXT) DESC LIMIT ?",
                      [before[0], before[0], before[1], *args, limit + 1])
        # A child warning/request is evidence for its work attempt, not another task.
        for event in result:
            if event["outcome"] not in _FAILURES | {"deferred"} or not event["span_id"]:
                continue
            child = conn.execute("SELECT * FROM events WHERE run_id=? AND (span_id=? OR parent_span_id=?) "
                                 "AND (severity IN ('warning','error','critical') OR outcome='deferred') AND id!=? "
                                 "ORDER BY CASE WHEN details LIKE '%exception_chain%' THEN 0 ELSE 1 END,id DESC LIMIT 1",
                                 (event["run_id"], event["span_id"], event["span_id"], event["id"])).fetchone()
            if child:
                current = journal.decoded_details(event["details"])
                child_details = journal.decoded_details(child["details"])
                if not current.get("exception_chain") and not current.get("validation_errors"):
                    event["details"] = json.dumps({**current, **child_details})
                    if child["message"] not in _GENERIC:
                        event["message"] = child["message"]
        return [_event_row(event) for event in result]


def _domain_rows(store, before, limit, selected=None):
    output, totals = [], {"applications": None, "outreach": None, "referrals": None, "legacy_submissions": None}
    with store.read() as conn:
        ts = tables(conn)
        if "jobs" not in ts:
            return output, totals, None
        saved_ready = conn.execute("SELECT COUNT(*) FROM jobs WHERE state='ready_to_apply'").fetchone()[0]
        totals["legacy_submissions"] = conn.execute("SELECT COUNT(*) FROM jobs WHERE NULLIF(applied_at,'') IS NOT NULL").fetchone()[0]
        specs = []
        if "application_attempts" in ts:
            totals["applications"] = conn.execute("SELECT COUNT(DISTINCT job_url) FROM application_attempts WHERE status='submitted' AND submitted_at IS NOT NULL").fetchone()[0]
            specs.append(("attempt", "application_attempts a JOIN jobs j ON j.url=a.job_url", "a.attempt_id",
                          "a.updated_at", "a.status", "a.verification_reason", "Apply", "a.job_url", "a.status NOT IN ('prepared','side_effect_intent')"))
        if {"referral_outbox", "referral_cases"} <= ts:
            totals["outreach"] = conn.execute("SELECT COUNT(*) FROM referral_outbox WHERE state IN ('sent','manual_sent') AND sent_at IS NOT NULL").fetchone()[0]
            specs.append(("outreach", "referral_outbox a JOIN referral_cases c ON c.id=a.case_id LEFT JOIN jobs j ON j.url=c.job_url", "a.id",
                          "a.updated_at", "a.state", "NULL", "Referral outreach", "c.job_url", "a.attempted_at IS NOT NULL"))
        if {"referral_events", "referral_cases"} <= ts:
            totals["referrals"] = conn.execute("SELECT COUNT(DISTINCT case_id) FROM referral_events WHERE kind='ats_referral' AND trust='operator-confirmed' AND voided_at IS NULL").fetchone()[0]
            specs.append(("referral", "referral_events a JOIN referral_cases c ON c.id=a.case_id JOIN jobs j ON j.url=c.job_url", "a.id",
                          "a.occurred_at", "a.kind", "NULL", "Secured referral", "c.job_url",
                          "a.kind='ats_referral' AND a.trust='operator-confirmed' AND a.voided_at IS NULL"))
        for prefix, source, key, at, outcome, error, action, url, condition in specs:
            if selected and selected[0] != prefix:
                continue
            epoch = f"ap_day({at})*86400"
            params = [before[0], before[0], before[1]]
            where = f"({condition}) AND ({epoch}<? OR ({epoch}=? AND '{prefix}:'||{key}<?))"
            if selected:
                where += f" AND {key}=?"
                params.append(selected[1])
            values = rows(conn, f"SELECT {key} record_id,{epoch} AS at,{outcome} outcome,{error} error,j.title,j.company,{url} job_url "
                          f"FROM {source} WHERE {where} ORDER BY {epoch} DESC,{key} DESC LIMIT ?", [*params, limit + 1])
            for item in values:
                success = item["outcome"] in {"submitted", "sent", "manual_sent", "ats_referral"}
                output.append({"id": prefix + ":" + item["record_id"], "at": item["at"],
                               "source": prefix, "action": action, "target": item["title"], "company": item["company"],
                               "job_url": item["job_url"], "outcome": item["outcome"], "saved_outcome": success,
                               "message": ("Submission confirmed" if prefix == "attempt" else "Delivery recorded" if prefix == "outreach" else "Referral confirmed") if success else
                                          sanitize(humanize_issue(item["error"])) if item["error"] else "Original error was not recorded.",
                               "origin": source.split(" ")[0]})
    return output, totals, saved_ready


def _now(root, runtime, workers, engine):
    if workers.get('execution_owner') == 'Temporal':
        observed = workers['automation']
        return {'current': observed['available'], 'automation': observed,
                'active': [row for row in workers['rows'] if row.get('active')],
                'active_count': observed['in_progress'], 'waiting_provider': None,
                'pipeline': workers['pipeline'], 'activity_counts': {}, 'warnings': observed['warnings']}
    # The common application selector owns this sample. A GET never acquires work.
    from applypilot.live_status import read_component
    capacity = read_component(root, "applications", runtime_id=runtime.get("runtime_id")) or {}
    try:
        age = time.time() - float(capacity.get("checked_at") or capacity.get("reported_at") or 0)
    except (ValueError, TypeError):
        age = float("inf")
    fresh = runtime.get("running") is True and not runtime.get("stale") and -5 <= age <= 30 and capacity.get("runtime_id") == runtime.get("runtime_id")
    application = {key: capacity.get(key) for key in ("selectable_jobs", "selection_available", "checked_at",
                   "target_workers", "current_workers", "busy_workers", "configured_workers",
                   "effective_workers", "reason", "mode", "active_tasks")}
    application["fresh"] = fresh
    if not fresh:
        application.update(selectable_jobs=None, selection_available=False, reason="No fresh matching selector report")
    elif application.get("reason") == "dask_scheduler":
        application["reason"] = ("Eligible applications found in the latest queue check."
                                 if application.get("selectable_jobs") else
                                 "No applications passed the latest readiness check."
                                 if application.get("selection_available") else
                                 "Checking application readiness.")
    worker_rows = workers.get("rows") or []
    blockers, seen = [], set()
    for worker in worker_rows:
        selection = worker.get("selection") or {}
        if selection.get("stale") or not worker.get("fresh"):
            continue
        for blocker in selection.get("first_blockers") or []:
            key = (selection.get("policy_id"), blocker.get("code"))
            if key not in seen:
                blockers.append(blocker)
                seen.add(key)
    from itertools import zip_longest
    active_workers = [w for w in worker_rows if w.get("fresh") and w.get("active")]
    by_stage = {}
    for worker in active_workers:
        by_stage.setdefault(worker.get("stage") or "unknown", []).append(worker)
    # A large application pool must not push every other service out of the
    # bounded live-work view. Keep representatives from each active stage.
    active_cards = [worker for batch in zip_longest(*by_stage.values())
                    for worker in batch if worker is not None][:40]
    return {"current": bool(workers.get("runtime_current")), "at": workers.get("at"), "applications": application,
            "active_count": len(active_workers),
            "pipeline": workers.get("pipeline") or {},
            "activity_counts": activity_counts(active_workers),
            "waiting_provider": sum(w.get("state") == "waiting_provider" for w in active_workers),
            "active": active_cards,
            "blockers": blockers[:20], "scheduler": engine.get("scheduler") or {},
            "runtime_state": runtime.get("control", {}).get("state") or ("running" if runtime.get("running") else "stopped"),
            "warnings": workers.get("warnings") or []}


def snapshot(root, store, q, *, runtime, workers, engine, setup_tasks=()):
    limit = integer(q.get("limit"), 25, 1, 100)
    before = _cursor(q.get("before"))
    value = {"at": now_iso(), "items": [], "warnings": [], "next_before": None,
             "scope": "One recorded work attempt per row; nested requests are evidence. Outcome totals use committed records."}
    try:
        items, value["outcomes"], value["saved_ready_labels"] = _domain_rows(store, before, limit)
    except (postgres.Error, OSError, ValueError) as exc:
        items = []
        value["outcomes"] = {}
        value["warnings"].append("Outcome records unavailable: " + sanitize(exc, 200))
    if q.get('feed') != 'outcomes':
        try:
            items += _journal_rows(root, before, limit)
        except (postgres.Error, OSError, ValueError) as exc:
            value["warnings"].append("Work journal unavailable: " + sanitize(exc, 200))
    for task in (() if q.get('feed') == 'outcomes' else setup_tasks):
        if task.get("state") in {"queued", "running"}:
            continue
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        items.append({"id": "setup:" + str(task.get("id") or ""), "at": _stamp(task.get("finished_at") or task.get("started_at")),
                      "source": "setup", "action": "Setup / " + str(task.get("operation") or "task"), "target": "Workspace",
                      "outcome": task.get("state"), "saved_outcome": False,
                      "message": sanitize(result.get("error") or task.get("message") or "Original result was not recorded."),
                      "origin": "onboarding"})
    items = [r for r in items if (r["at"], r["id"]) < before]
    items.sort(key=lambda r: (r["at"], r["id"]), reverse=True)
    value["items"] = [{k: v for k, v in row.items() if k != "evidence"} for row in items[:limit]]
    try:
        _attach_targets(store, value["items"])
    except (postgres.Error, OSError, ValueError):
        value["warnings"].append("Some historical work targets could not be resolved.")
    try:
        _attach_attempt_evidence(root, value["items"])
    except (postgres.Error, OSError, ValueError):
        pass  # Durable outcomes remain readable when telemetry is unavailable.
    if len(items) > limit:
        value["next_before"] = _encode(items[limit - 1])
    value["now"] = _now(root, runtime, workers, engine)
    value["current_run"] = _run_totals(root, store, runtime)
    value["recovery"] = recovery_snapshot(store)
    return value


def _attach_attempt_evidence(root, items):
    attempts = {row["id"].split(":", 1)[1]: row for row in items if row["source"] == "attempt" and not row["saved_outcome"]}
    if not attempts:
        return
    with journal.reader(root) as conn:
        events = rows(conn, "SELECT * FROM events WHERE task_id IN (" + ",".join("?" for _ in attempts) +
                      ") AND component='applications' AND operation='attempt_result' ORDER BY id DESC LIMIT 100", tuple(attempts))
    for event in events:
        item = attempts.pop(event["task_id"], None)
        if item is not None:
            event["details"] = journal.decoded_details(event["details"])
            item["journal_message"] = _diagnostic(event)
            item["journal_origin"] = event["details"].get("origin") or "applications / attempt_result"
            if not item.get("message") or item["message"] == "Original error was not recorded.":
                item["message"] = item["journal_message"]
            item["event_id"] = event["id"]


def _attach_targets(store, items, *, resolve_links=False):
    from datetime import datetime, timezone
    identity = str(store.path.resolve())
    wanted = {r.get("job_id") for r in items if r.get("job_id") and (resolve_links or not r.get("target"))}
    spans = {r.get("span_id") for r in items if r.get("span_id")}
    tasks = {r.get("task_id") for r in items if r.get("task_id")}
    if not wanted and not spans and not tasks:
        return
    with store.read() as conn:
        if "jobs" not in tables(conn):
            return
        mapped, missing = {}, set()
        for digest in wanted:
            found, cached = _cached(_targets, (identity, digest))
            if found:
                if cached:
                    mapped[digest] = cached
            else:
                missing.add(digest)
        matches = rows(conn, "SELECT url,title,company,ap_public_id(url) digest FROM jobs WHERE ap_public_id(url) IN (" +
                       ",".join("?" for _ in missing) + ")", tuple(missing)) if missing else []
        mapped.update({row["digest"]: row for row in matches})
        for digest in missing:
            _remember(_targets, (identity, digest), mapped.get(digest))
        sources = {}
        if tasks and "discovery_tasks" in tables(conn):
            sources = {r["task_id"]: r["source_key"] for r in rows(conn,
                       "SELECT task_id,source_key FROM discovery_tasks WHERE task_id IN (" +
                       ",".join("?" for _ in tasks) + ")", tuple(tasks))}
        committed = {}
        unresolved = set()
        for item in items:
            if not item.get("span_id"):
                continue
            key = (item.get("run_id"), item["span_id"])
            found, cached = _cached(_commits, (identity, *key))
            if found:
                if cached:
                    committed[key] = cached
            else:
                unresolved.add(item["span_id"])
        if unresolved and "job_state_transitions" in tables(conn) and "metadata" in columns(conn, "job_state_transitions"):
            indexed = conn.execute("SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND tablename='job_state_transitions' "
                                   "AND strpos(indexdef,'operation_span_id')>0 LIMIT 1").fetchone()
            if indexed:
                source, scan_args = "job_state_transitions", []
            else:
                # Compatibility before the writer installs its expression index.
                # Time only bounds work; exact recorded IDs still establish linkage.
                earliest = min(r["at"] - float(r.get("duration") or 0) - 5 for r in items if r.get("span_id") in unresolved)
                source = "(SELECT id,to_state,at,reason,metadata FROM job_state_transitions WHERE at>=? ORDER BY at LIMIT 2000)"
                scan_args = [datetime.fromtimestamp(earliest, timezone.utc).isoformat()]
            transitions = rows(conn, "SELECT id,to_state,at,reason,metadata FROM " + source + " "
                               "WHERE ap_json_valid(metadata) AND COALESCE(ap_json_text(metadata,'$._operation_span_id'),ap_json_text(metadata,'$.operation_span_id')) IN (" +
                               ",".join("?" for _ in unresolved) + ") ORDER BY id DESC LIMIT 200", [*scan_args, *unresolved])
            for transition in transitions:
                meta = json.loads(transition.pop("metadata"))
                key = (meta.get("_operation_run_id") or meta.get("operation_run_id"),
                       meta.get("_operation_span_id") or meta.get("operation_span_id"))
                committed.setdefault(key, {**transition, "reason": sanitize(transition.get("reason")),
                                          "validation_errors": [sanitize(e, 700) for e in meta.get("validation_errors", [])[:20] if isinstance(e, str)]})
            for item in items:
                if item.get("span_id") in unresolved:
                    key = (item.get("run_id"), item["span_id"])
                    _remember(_commits, (identity, *key), committed.get(key))
        for item in items:
            job = mapped.get(item.get("job_id"))
            if job:
                item["job_url"] = job["url"]
                item["target"] = item.get("target") or job["title"]
                item["company"] = item.get("company") or job["company"]
            elif item.get("task_id") in sources:
                item["target"] = sanitize(sources[item["task_id"]], 200)
            saved = committed.get((item.get("run_id"), item.get("span_id")))
            if saved:
                item["committed_result"] = saved
                if item.get("outcome") == "success":
                    item["message"] = "Saved job state: " + saved["to_state"]


def _validation_report(root, name):
    """Only an explicitly recorded basename, within fixed document directories."""
    if not isinstance(name, str) or not re.fullmatch(r"[\w .-]{1,200}_REPORT\.json", name):
        return None
    for folder in (root / "tailored_resumes", root / "tailored_resumes" / "rejected"):
        path = folder / name
        try:
            if any(p.is_symlink() for p in (root, root / "tailored_resumes", folder, path)) or path.stat().st_size > 512000:
                continue
            path.resolve().relative_to((root / "tailored_resumes").resolve())
            report = json.loads(path.read_text(encoding="utf-8"))
            validator = report.get("validator") or {}
            return {"file": name, "status": sanitize(report.get("status"), 100), "attempts": report.get("attempts"),
                    "errors": [sanitize(error, 700) for key in ("errors", "impact_errors", "resume_fact_errors")
                               for error in (validator.get(key) or [])[:20] if isinstance(error, str)],
                    "judge_issues": sanitize((report.get("judge") or {}).get("issues"), 1800)}
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return None


def detail(root, store, identifier, setup_tasks=()):
    if not re.fullmatch(r"(event|attempt|outreach|referral|setup):[A-Za-z0-9_-]{1,100}", identifier):
        raise ValueError("Invalid work record")
    kind, key = identifier.split(":", 1)
    if kind == "event":
        with journal.reader(root) as conn:
            event = conn.execute("SELECT * FROM events WHERE id=?", (key,)).fetchone()
            if event is None:
                return {"available": False, "message": "This work record is no longer retained."}
            event = dict(event)
            related = rows(conn, "WITH RECURSIVE spans(id,depth) AS (SELECT ?,0 UNION ALL "
                           "SELECT e.span_id,s.depth+1 FROM events e JOIN spans s ON e.parent_span_id=s.id "
                           "WHERE e.run_id=? AND e.kind='start' AND s.depth<5 AND e.span_id!=s.id) "
                           "SELECT * FROM events WHERE run_id=? AND span_id IN (SELECT id FROM spans) ORDER BY id DESC LIMIT 201",
                           (event["span_id"], event["run_id"], event["run_id"])) if event["span_id"] else [event]
        for row in related:
            row["details"] = journal.decoded_details(row["details"])
        retained = related[:200]
        retained.reverse()
        item = _event_row(event)
        item.pop("evidence", None)
        _attach_targets(store, [item], resolve_links=True)
        report = _validation_report(root, journal.decoded_details(event["details"]).get("validation_report"))
        return {"available": True, "item": item, "events": retained, "truncated": len(related) > 200,
                "validation_report": report,
                "note": "Recorded exception and provider evidence is sanitized. Missing historical details cannot be reconstructed."}
    if kind == "setup":
        task = next((t for t in setup_tasks if str(t.get("id")) == key), None)
        return {"available": task is not None, "record": {k: sanitize(task.get(k), 1800) for k in
                ("operation", "state", "message", "started_at", "finished_at")} if task else None}
    items, _, _ = _domain_rows(store, (time.time() + 5, "~"), 1, selected=(kind, key))
    evidence = []
    if kind == "attempt" and items:
        try:
            with journal.reader(root) as conn:
                evidence = rows(conn, "SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 201", (key,))
            for event in evidence:
                event["details"] = journal.decoded_details(event["details"])
        except (postgres.Error, OSError, ValueError):
            pass
    return {"available": bool(items), "item": items[0] if items else None,
            "events": list(reversed(evidence[:200])), "truncated": len(evidence) > 200,
            "note": "This outcome is read from its committed business record. No timestamp-only correlation is assumed."}

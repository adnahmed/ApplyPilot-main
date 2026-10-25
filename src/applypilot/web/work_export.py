"""Complete, read-only JSON snapshots for one retained operations run."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from applypilot import postgres
import tempfile
import time

from applypilot.operations import store as journal
from applypilot.operations.runtime import exception_details, sanitize
from applypilot.operations.support import saved_diagnostics, snapshot as support_snapshot
from applypilot.postgres_reads import connect_readonly
from .data import columns, tables
from .work import _ACTIONS, _BOUNDARIES, _event_row


_RUN_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_PRIVATE = re.compile(
    r"(?:password|token|secret|cookie|credential|authorization|prompt|messages|"
    r"resume|full_description|content|html|target_url|^url$|payload|proof$)", re.I
)


def _fetch(cursor, size=500):
    while True:
        batch = cursor.fetchmany(size)
        if not batch:
            return
        for row in batch:
            yield dict(row)


def _safe(value, key=""):
    if isinstance(value, dict):
        result = {str(key)[:80]: _safe(item, key=str(key)) for key, item in value.items()
                  if not _PRIVATE.search(str(key))}
        if isinstance(value.get("stderr"), (str, bytes)) and len(value["stderr"]) > 6000:
            result["stderr_truncated"] = True
        return result
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float:
        return value if value == value and abs(value) != float("inf") else None
    return sanitize(value, 6000 if key == "stderr" else 1800)


def _event(event):
    value = {key: event.get(key) for key in (
        "id", "at", "run_id", "process_id", "span_id", "parent_span_id",
        "cycle_id", "task_id", "job_id", "stage", "component", "operation",
        "kind", "severity", "outcome", "error_kind", "message", "duration",
        "fingerprint")}
    value["message"] = sanitize(value["message"])
    details = event.get("details")
    if isinstance(details, str):
        details = journal.decoded_details(details)
    value["details"] = _safe(details or {})
    return value


def _begin(conn):
    conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
    return {"captured_at": datetime.now(timezone.utc).isoformat(),
            "captured_epoch": time.time()}


def list_runs(root):
    """Return every retained run; choose the newest application/autopilot run."""
    with journal.reader(root) as conn:
        capture = _begin(conn)
        cursor = conn.execute("""SELECT r.id,r.role,r.started_at,r.last_at,
            COUNT(e.id) event_count,
            COALESCE(SUM(CASE WHEN e.stage='applications' OR e.component='applications'
                         THEN 1 ELSE 0 END),0) application_event_count
            FROM runs r LEFT JOIN events e ON e.run_id=r.id
            GROUP BY r.id,r.role,r.started_at,r.last_at ORDER BY r.started_at DESC,r.id DESC""")
        result = list(_fetch(cursor))
    eligible = [row for row in result if row["application_event_count"] or
                re.search(r"(?:autopilot|application|apply)", row["role"], re.I)]
    return {"captured_at": capture["captured_at"], "items": result,
            "default_run_id": (eligible or result or [{}])[0].get("id"),
            "scope": "All runs retained in the operations journal."}


def validate_run_id(root, run_id):
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("Choose a valid retained work run")
    with journal.reader(root) as conn:
        if conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise ValueError("That work run is no longer retained. Reload the run list.")
    return run_id


def _journal_snapshot(root, run_id):
    with journal.reader(root) as conn:
        capture = _begin(conn)
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("That work run is no longer retained")
        expected = conn.execute("SELECT COUNT(*),MAX(id) FROM events WHERE run_id=?", (run_id,)).fetchone()
        capture.update(event_count=expected[0], event_highwater_id=expected[1])
        events = [_event(row) for row in _fetch(conn.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY id", (run_id,)))]
        capture["finished_at"] = datetime.now(timezone.utc).isoformat()
        capture["finished_epoch"] = time.time()
    return dict(run), events, capture


def _job_map(conn, urls):
    if not urls or "jobs" not in tables(conn):
        return {}
    result = {}
    values = sorted(urls)
    for offset in range(0, len(values), 800):
        batch = values[offset:offset + 800]
        cursor = conn.execute("SELECT url,title,company FROM jobs WHERE url IN (" +
                              ",".join("?" for _ in batch) + ")", batch)
        result.update((row["url"], dict(row)) for row in cursor)
    return result


def _read_applications(conn, run_id, task_ids):
    attempts = []
    if "application_attempts" not in tables(conn):
        return [], [], False
    available = columns(conn, "application_attempts")
    direct_run = "operation_run_id" in available or "queue_policy_json" in available
    wanted = set(task_ids)
    fields = [name for name in (
        "attempt_id", "job_url", "submission_identity", "channel", "target",
        "browser_runtime", "worker_id", "status", "prepared_at",
        "side_effect_intent_at", "submitted_at", "proof_kind", "proof",
        "verification_reason", "updated_at", "result_recorded_at",
        "operation_run_id", "operation_span_id", "queue_policy_json") if name in available]
    cursor = conn.execute("SELECT " + ",".join('"' + name + '"' for name in fields) +
                          " FROM application_attempts")
    for raw in _fetch(cursor):
        policy = {}
        if raw.get("queue_policy_json"):
            try:
                policy = json.loads(raw.pop("queue_policy_json"))
            except (ValueError, TypeError):
                policy = {}
        if not isinstance(policy, dict):
            policy = {}
        linked_run = (raw.get("operation_run_id") or policy.get("_operation_run_id") or
                      policy.get("operation_run_id"))
        linked_span = (raw.get("operation_span_id") or policy.get("_operation_span_id") or
                       policy.get("operation_span_id"))
        if linked_run != run_id and (linked_run or raw.get("attempt_id") not in wanted):
            continue
        proof = raw.pop('proof', None)
        if raw.get('status') == 'submitted':
            from applypilot.apply.guard import REPORTED_COMPLETION_KIND
            confidence = 'explicit-proof' if raw.get('proof_kind') and proof else 'unverified'
            if raw.get('proof_kind') == REPORTED_COMPLETION_KIND:
                try:
                    reported = json.loads(proof or '{}')
                except (ValueError, TypeError):
                    reported = {}
                confidence = ('user-confirmed' if isinstance(reported, dict) and reported.get('source') == 'user'
                              else 'agent-reported')
            raw['verification_confidence'] = confidence
        item = {key: (value if key == "job_url" else _safe(value))
                for key, value in raw.items()}
        item["attribution"] = ({"method": ("application_attempts.operation_run_id"
                                            if raw.get("operation_run_id") == run_id else
                                            "application_attempts.queue_policy_json.operation_run_id"),
                                "run_id": run_id, "span_id": linked_span}
                               if linked_run == run_id else
                               {"method": "events.task_id=application_attempts.attempt_id",
                                "task_id": raw.get("attempt_id")})
        attempts.append(item)
    attempt_ids = {row["attempt_id"] for row in attempts}
    outcomes = []
    if attempt_ids and "application_outcomes" in tables(conn):
        outcome_columns = columns(conn, "application_outcomes")
        allowed = [name for name in (
            "id", "job_url", "event_type", "occurred_at", "source", "notes",
            "submission_identifier", "document_variant", "role_family", "market",
            "created_at", "event_key", "trust", "voided_at", "void_reason",
            "supersedes_id") if name in outcome_columns]
        cursor = conn.execute("SELECT " + ",".join('"' + name + '"' for name in allowed) +
                              " FROM application_outcomes")
        for raw in _fetch(cursor):
            if raw.get("submission_identifier") in attempt_ids:
                value = {key: (item if key == "job_url" else _safe(item))
                         for key, item in raw.items()}
                value["attribution"] = {
                    "method": "application_outcomes.submission_identifier=application_attempts.attempt_id",
                    "attempt_id": raw["submission_identifier"]}
                outcomes.append(value)
    return attempts, outcomes, direct_run


def _transitions(conn, run_id):
    if "job_state_transitions" not in tables(conn) or "metadata" not in columns(conn, "job_state_transitions"):
        return []
    cursor = conn.execute("""SELECT id,job_url,from_state,to_state,at,reason,metadata
        FROM job_state_transitions WHERE ap_json_valid(metadata) AND
        COALESCE(ap_json_text(metadata,'$._operation_run_id'),
                 ap_json_text(metadata,'$.operation_run_id'))=? ORDER BY id""", (run_id,))
    result = []
    for raw in _fetch(cursor):
        metadata = json.loads(raw.pop("metadata"))
        raw["reason"] = sanitize(raw.get("reason"))
        raw["metadata"] = _safe(metadata)
        raw["attribution"] = {"method": "job_state_transitions.metadata.operation_run_id",
                              "run_id": run_id,
                              "span_id": metadata.get("_operation_span_id") or metadata.get("operation_span_id")}
        result.append(raw)
    return result


def _linked_records(conn, table, run_id, allowed, *, run_columns=(), json_fields=()):
    if table not in tables(conn):
        return [], {"supported": True, "unlinked_records": 0, "source_present": False}
    available = columns(conn, table)
    available_runs = [name for name in run_columns if name in available]
    json_column = next((name for name in json_fields if name in available), None)
    if not available_runs and not json_column:
        count = conn.execute("SELECT COUNT(*) FROM \"" + table + "\"").fetchone()[0]
        return [], {"supported": count == 0, "unlinked_records": count, "source_present": True}
    fields = [name for name in allowed if name in available]
    for run_column in available_runs:
        if run_column not in fields:
            fields.append(run_column)
    if json_column and json_column not in fields:
        fields.append(json_column)
    clauses, args = [], []
    for run_column in available_runs:
        clauses.append('"' + run_column + '"=?')
        args.append(run_id)
    if json_column:
        clauses.append("(ap_json_valid(\"" + json_column + "\") AND COALESCE(" +
                       "ap_json_text(\"" + json_column + "\",'$._operation_run_id')," +
                       "ap_json_text(\"" + json_column + "\",'$.operation_run_id'))=?)")
        args.append(run_id)
    cursor = conn.execute("SELECT " + ",".join('"' + name + '"' for name in fields) +
                          " FROM \"" + table + "\" WHERE " + " OR ".join(clauses), args)
    result = []
    for raw in _fetch(cursor):
        metadata = {}
        if json_column and raw.get(json_column):
            try:
                metadata = json.loads(raw.pop(json_column))
            except (ValueError, TypeError):
                metadata = {"decode_error": "Stored metadata is unreadable"}
        if not isinstance(metadata, dict):
            metadata = {"decode_error": "Stored metadata must be an object"}
        value = {key: _safe(item) for key, item in raw.items()}
        if metadata:
            value["metadata"] = _safe(metadata)
        links = [{"method": table + "." + name, "run_id": run_id,
                  "span_id": raw.get(name.replace("run_id", "span_id"))}
                 for name in available_runs if raw.get(name) == run_id]
        metadata_run = metadata.get("_operation_run_id") or metadata.get("operation_run_id")
        if metadata_run == run_id:
            links.append({"method": table + "." + json_column + ".operation_run_id",
                          "run_id": run_id, "span_id": metadata.get("_operation_span_id") or
                          metadata.get("operation_span_id")})
        value["attribution"] = {"method": "persisted_run_link", "links": links}
        result.append(value)
    linked = []
    for run_column in available_runs:
        linked.append("NULLIF(\"" + run_column + "\",'') IS NOT NULL")
    if json_column:
        linked.append("(ap_json_valid(\"" + json_column + "\") AND COALESCE(" +
                      "ap_json_text(\"" + json_column + "\",'$._operation_run_id')," +
                      "ap_json_text(\"" + json_column + "\",'$.operation_run_id')) IS NOT NULL)")
    unlinked = conn.execute("SELECT COUNT(*) FROM \"" + table + "\" WHERE NOT (" +
                            " OR ".join(linked) + ")").fetchone()[0]
    return result, {"supported": True, "unlinked_records": unlinked, "source_present": True}


def _setup_records(root, run_id):
    folder = Path(root) / "setup" / "tasks"
    result, linked_field, unlinked, skipped = [], False, 0, 0
    try:
        paths = list(folder.glob("*.state.json"))
    except OSError:
        return result, {"supported": False, "unlinked_records": unlinked,
                        "skipped_records": 1, "source_present": folder.exists()}
    linked_field = not paths
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > 1_000_000:
                skipped += 1
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            skipped += 1
            continue
        if not isinstance(raw, dict):
            skipped += 1
            continue
        linked_field = linked_field or "operation_run_id" in raw or "run_id" in raw
        linked = raw.get("operation_run_id") or raw.get("run_id")
        unlinked += int(not linked)
        if linked != run_id:
            continue
        record = {key: _safe(raw.get(key)) for key in (
            "id", "operation", "state", "message", "started_at", "finished_at",
            "runtime_build", "operation_run_id", "operation_span_id")}
        record["diagnostic"] = saved_diagnostics(raw.get("result"))
        result.append(record)
    return result, {"supported": linked_field, "unlinked_records": unlinked,
                    "skipped_records": skipped, "source_present": folder.exists()}


def _main_snapshot(database, root, run_id, events):
    capture = {"captured_at": None, "captured_epoch": None, "available": False}
    result = {"application_attempts": [], "application_outcomes": [],
              "job_state_transitions": [], "referral_outbox": [],
              "referral_events": [], "setup_tasks": []}
    capabilities = {}
    result["setup_tasks"], capabilities["setup_run_metadata"] = _setup_records(root, run_id)
    path = Path(database)
    if not path.exists():
        return result, {}, capture, capabilities
    conn = connect_readonly(path)
    try:
        capture.update(_begin(conn), available=True)
        task_ids = {event["task_id"] for event in events if event.get("task_id")}
        attempts, outcomes, direct_apps = _read_applications(conn, run_id, task_ids)
        result["application_attempts"], result["application_outcomes"] = attempts, outcomes
        capabilities["application_direct_run_metadata"] = direct_apps
        result["job_state_transitions"] = _transitions(conn, run_id)
        result["referral_outbox"], capabilities["outreach_run_metadata"] = _linked_records(
            conn, "referral_outbox", run_id,
            ("id", "case_id", "contact_id", "kind", "channel", "state", "attempted_at",
             "sent_at", "error_kind", "created_at", "updated_at", "created_run_id",
             "created_span_id", "dispatch_run_id", "dispatch_span_id"),
            run_columns=("created_run_id", "dispatch_run_id"))
        result["referral_events"], capabilities["referral_run_metadata"] = _linked_records(
            conn, "referral_events", run_id,
            ("id", "event_key", "case_id", "contact_id", "kind", "occurred_at", "quote",
             "trust", "created_at", "voided_at", "void_reason"),
            json_fields=("details_json", "metadata"))
        urls = {row.get("job_url") for values in result.values() for row in values
                if isinstance(row, dict) and row.get("job_url")}
        jobs = _job_map(conn, urls)
        capture["finished_at"] = datetime.now(timezone.utc).isoformat()
        capture["finished_epoch"] = time.time()
    finally:
        conn.rollback()
        conn.close()
    return result, jobs, capture, capabilities


def _validation(root, events, missing):
    names = {event.get("details", {}).get("validation_report") for event in events}
    names.discard(None)
    reports = []
    for name in sorted(names):
        if not isinstance(name, str) or not re.fullmatch(r"[\w .-]{1,200}_REPORT\.json", name):
            missing.append({"source": "validation", "record": sanitize(name, 200),
                            "reason": "Recorded validation reference is not a safe report basename."})
            continue
        found = None
        for folder in (Path(root) / "tailored_resumes", Path(root) / "tailored_resumes" / "rejected"):
            path = folder / name
            try:
                if any(item.is_symlink() for item in (Path(root), Path(root) / "tailored_resumes", folder, path)) or path.stat().st_size > 512000:
                    continue
                path.resolve().relative_to((Path(root) / "tailored_resumes").resolve())
                report = json.loads(path.read_text(encoding="utf-8"))
                validator = report.get("validator") or {}
                found = {"file": name, "status": sanitize(report.get("status"), 100),
                         "attempts": report.get("attempts"),
                         "validation_mode": sanitize(report.get("validation_mode"), 100),
                         "factual_review": sanitize(report.get("factual_review"), 300),
                         "errors": [sanitize(error, 700) for key in
                                    ("errors", "impact_errors", "resume_fact_errors")
                                    for error in (validator.get(key) or []) if isinstance(error, str)],
                         "judge_issues": _safe((report.get("judge") or {}).get("issues"))}
                break
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if found is None:
            missing.append({"source": "validation", "record": name,
                            "reason": "The referenced safe validation report is unavailable or unreadable."})
        else:
            reports.append(found)
    return reports


def _descendants(children, root_span, cache):
    if root_span in cache:
        return cache[root_span]
    found, pending = {root_span}, [root_span]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    cache[root_span] = found
    return found


def _work_items(events, committed, jobs, validation):
    claimed, result = set(), []
    by_task, by_span, children = defaultdict(list), defaultdict(list), defaultdict(set)
    for event in events:
        if event.get("task_id"):
            by_task[event["task_id"]].append(event["id"])
        if event.get("span_id"):
            by_span[event["span_id"]].append(event["id"])
        if event.get("kind") == "start" and event.get("span_id") and event.get("parent_span_id"):
            children[event["parent_span_id"]].add(event["span_id"])
    descendants = {}
    event_by_id = {event["id"]: event for event in events}
    def span_evidence(span_id):
        return [identifier for span in _descendants(children, span_id, descendants)
                for identifier in by_span.get(span, ())]
    def link_evidence(attribution):
        links = attribution.get("links") or [attribution]
        return list(dict.fromkeys(identifier for link in links if link.get("span_id")
                                  for identifier in span_evidence(link["span_id"])))
    def validation_files(identifiers):
        return sorted({event_by_id[identifier].get("details", {}).get("validation_report")
                       for identifier in identifiers
                       if event_by_id[identifier].get("details", {}).get("validation_report") in validation_by_name})
    transitions = defaultdict(list)
    for row in committed["job_state_transitions"]:
        transitions[row.get("attribution", {}).get("span_id")].append(row)
    outcomes = defaultdict(list)
    for row in committed["application_outcomes"]:
        outcomes[row.get("submission_identifier")].append(row)
    validation_by_name = {row["file"]: row for row in validation}
    for attempt in committed["application_attempts"]:
        evidence = list(dict.fromkeys([*by_task.get(attempt["attempt_id"], []),
                                      *link_evidence(attempt["attribution"])]))
        claimed.update(evidence)
        job = jobs.get(attempt.get("job_url"), {})
        result.append({"id": "attempt:" + attempt["attempt_id"], "source": "application",
                       "action": "Apply", "target": job.get("title"), "company": job.get("company"),
                       "outcome": attempt.get("status"), "reason": attempt.get("verification_reason"),
                       "attribution": attempt["attribution"], "committed_outcomes":
                       [attempt, *outcomes.get(attempt["attempt_id"], [])],
                       "state_basis": "Current committed state at the main database capture, not reconstructed end-of-run state.",
                       "evidence_event_ids": evidence, "validation_files": validation_files(evidence)})
    boundaries = set(_BOUNDARIES)
    for root_event in events:
        boundary = (root_event.get("stage"), root_event.get("component"), root_event.get("operation"))
        signal = (not root_event.get("span_id") and root_event.get("kind") == "signal" and
                  root_event.get("severity") in {"error", "critical"})
        if not signal and not (root_event.get("kind") == "finish" and boundary in boundaries and
                               root_event.get("outcome") not in {"skipped", "empty"}):
            continue
        evidence = span_evidence(root_event["span_id"]) if root_event.get("span_id") else [root_event["id"]]
        claimed.update(evidence)
        summary = _event_row(root_event)
        summary.pop("evidence", None)
        job = jobs.get(summary.get("job_url"), {})
        result.append({"id": summary["id"], "source": "operations",
                       "action": _ACTIONS.get((root_event["component"], root_event["operation"]),
                                              summary["action"]),
                       "target": summary.get("target") or job.get("title"),
                       "company": summary.get("company") or job.get("company"),
                       "outcome": root_event.get("outcome"), "reason": summary.get("message"),
                       "attribution": {"method": "events.run_id", "run_id": root_event["run_id"],
                                       "span_id": root_event.get("span_id")},
                       "committed_outcomes": transitions.get(root_event["span_id"], []) if root_event.get("span_id") else [],
                       "state_basis": "Operation outcome recorded in the selected run.",
                       "evidence_event_ids": evidence, "validation_files": validation_files(evidence)})
    attached_transitions = {row["id"] for item in result for row in item["committed_outcomes"]
                            if isinstance(row, dict) and "to_state" in row}
    for transition in committed["job_state_transitions"]:
        if transition["id"] in attached_transitions:
            continue
        evidence = link_evidence(transition["attribution"])
        claimed.update(evidence)
        job = jobs.get(transition.get("job_url"), {})
        result.append({"id": "transition:" + str(transition["id"]), "source": "preparation",
                       "action": "Save job state", "target": job.get("title"), "company": job.get("company"),
                       "outcome": transition.get("to_state"), "reason": transition.get("reason"),
                       "attribution": transition["attribution"], "committed_outcomes": [transition],
                       "state_basis": "Current audit record captured from the selected run.",
                       "evidence_event_ids": evidence, "validation_files": validation_files(evidence)})
    for source, action, state_key, reason_key in (
            ("referral_outbox", "Referral outreach", "state", "error_kind"),
            ("referral_events", "Referral outcome", "kind", "quote"),
            ("setup_tasks", "Setup task", "state", "message")):
        for record in committed[source]:
            evidence = link_evidence(record.get("attribution") or {})
            claimed.update(evidence)
            result.append({"id": source + ":" + str(record.get("id")), "source": source,
                           "action": action, "target": record.get("case_id") or "Workspace",
                           "company": None, "outcome": record.get(state_key),
                           "reason": record.get(reason_key) or "No separate reason was recorded.",
                           "attribution": record.get("attribution"), "committed_outcomes": [record],
                           "state_basis": "Current committed state at capture time; later changes may have updated this record.",
                           "evidence_event_ids": evidence, "validation_files": validation_files(evidence)})
    result.sort(key=lambda row: max((event_by_id[identifier].get("at", 0)
                                    for identifier in row["evidence_event_ids"]), default=0))
    return result, [event["id"] for event in events if event["id"] not in claimed]


def _application_trace_links(root, attempts):
    """Read only persisted links. Trace IDs are NOT reservation IDs or job IDs."""
    wanted = sorted({str(row["attempt_id"]) for row in attempts if row.get("attempt_id")})
    result = {"available": True, "links": [], "unlinked_attempts": wanted.copy(),
              "source": "learning.episodes.attempt_id", "captured_at": datetime.now(timezone.utc).isoformat()}
    if not wanted:
        return result
    directory = Path(os.getenv("APPLYPILOT_LEARNING_DIR", str(Path(root) / "learning"))).expanduser()
    path = directory / "learning.pgstore"
    if path.is_symlink() or not path.exists():
        return {**result, "available": False, "reason": "Persisted learning trace links are unavailable."}
    try:
        conn = connect_readonly(path)
        try:
            conn.row_factory = postgres.Row
            # One read-only snapshot; never create/migrate the learning corpus
            # just to export diagnostics.
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            if "episodes" not in tables(conn):
                return {**result, "available": False, "reason": "No persisted episode table."}
            for offset in range(0, len(wanted), 500):
                batch = wanted[offset:offset + 500]
                cursor = conn.execute("SELECT attempt_id, trace_id FROM episodes WHERE attempt_id IN (" +
                                      ",".join("?" for _ in batch) + ")", batch)
                for row in cursor:
                    trace_id = str(row["trace_id"] or "")
                    if re.fullmatch(r"[a-f0-9]{32}", trace_id):
                        result["links"].append({"attempt_id": row["attempt_id"], "trace_id": trace_id})
            conn.rollback()
        finally:
            conn.close()
    except (postgres.Error, OSError, ValueError) as exc:
        return {**result, "available": False, "reason": "Persisted trace lookup failed.", **exception_details(exc)}
    linked = {row["attempt_id"] for row in result["links"]}
    result["unlinked_attempts"] = [item for item in wanted if item not in linked]
    return result


def build_report(root, database, run_id, *, progress=lambda phase: None):
    run_id = validate_run_id(root, run_id)
    missing, errors, truncation = [], [], []
    progress("reading journal")
    run, events, journal_capture = _journal_snapshot(root, run_id)
    progress("reading committed outcomes")
    try:
        committed, jobs, main_capture, capabilities = _main_snapshot(database, root, run_id, events)
    except (postgres.Error, OSError, ValueError) as exc:
        committed = {name: [] for name in ("application_attempts", "application_outcomes",
                    "job_state_transitions", "referral_outbox", "referral_events", "setup_tasks")}
        setup_records, setup_capability = _setup_records(root, run_id)
        committed["setup_tasks"] = setup_records
        jobs, capabilities = {}, {"setup_run_metadata": setup_capability}
        main_capture = {"captured_at": None, "captured_epoch": None, "available": False}
        details = exception_details(exc)
        errors.append({"source": "main_database", "reason": details['exception_chain'][0]['message'], **details})
    if not main_capture["available"]:
        missing.append({"source": "main_database", "reason": "The main database was not available for committed outcomes."})
    for key, label in (("outreach_run_metadata", "referral_outbox"),
                       ("referral_run_metadata", "referral_events"),
                       ("setup_run_metadata", "setup_tasks")):
        status = capabilities.get(key) or {"supported": False, "unlinked_records": 0}
        if (main_capture["available"] or key == "setup_run_metadata") and (
                not status["supported"] or status["unlinked_records"]):
            missing.append({"source": label,
                            "records": status["unlinked_records"],
                            "reason": "Historical records without a persisted run link were left unassigned."})
        if status.get("skipped_records"):
            missing.append({"source": label, "records": status["skipped_records"],
                            "reason": "Saved setup task files were unsafe or unreadable and were skipped."})
    expected_count = journal_capture.get("event_count")
    expected_highwater = journal_capture.get("event_highwater_id")
    actual_highwater = max((event["id"] for event in events), default=None)
    if len({event["id"] for event in events}) != len(events):
        errors.append({"source": "operations_journal", "reason": "Duplicate event identifiers were read."})
    if len(events) != expected_count or actual_highwater != expected_highwater:
        truncation.append({"source": "operations_journal", "expected_events": expected_count,
                           "exported_events": len(events), "expected_highwater_id": expected_highwater,
                           "exported_highwater_id": actual_highwater})
    progress("reading validation")
    validation = _validation(root, events, missing)
    progress("assembling report")
    work_items, unattached = _work_items(events, committed, jobs, validation)
    counts = {"events": len(events), "work_items": len(work_items),
              "unattached_events": len(unattached), "validation_reports": len(validation),
              **{name: len(values) for name, values in committed.items()}}
    counts["event_outcomes"] = dict(Counter(event.get("outcome") or "" for event in events))
    counts["workspace_unassigned"] = {
        label: (capabilities.get(key) or {}).get("unlinked_records", 0)
        for key, label in (("outreach_run_metadata", "referral_outbox"),
                           ("referral_run_metadata", "referral_events"),
                           ("setup_run_metadata", "setup_tasks"))}
    try:
        support_context = support_snapshot(root)
    except (OSError, ValueError, TypeError) as exc:
        support_context = {"available": False, **exception_details(exc)}
        errors.append({"source": "support_context", "reason": sanitize(exc, 300), **exception_details(exc)})
    coverage_complete = not missing and not errors and not truncation
    report = {
        "schema": "applypilot.selected-work-run.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "run": {**_safe(run), "attribution": {"method": "events.run_id", "run_id": run_id}},
        "captures": {"operations_journal": journal_capture, "main_database": main_capture,
                     "note": "Each PostgreSQL source was read in one independent read-only transaction; capture times can differ."},
        "counts": counts, "work_items": work_items, "committed_records": committed,
        "evidence": events, "validation": validation, "unattached_events": unattached,
        "support_context": support_context,
        "application_trace_links": _application_trace_links(root, committed["application_attempts"]),
        "coverage": {"complete": coverage_complete,
                     "missing": missing, "errors": errors, "truncation": truncation,
                     "attribution_rules": [
                         "Journal evidence uses exact events.run_id.",
                         "Application attempts use persisted operation_run_id when present, otherwise exact events.task_id=application_attempts.attempt_id.",
                         "Application outcomes use exact submission_identifier=matched attempt_id.",
                         "Preparation transitions use persisted operation_run_id and operation_span_id metadata.",
                         "Outreach, referral, and setup records require a persisted run link; timestamps and job URLs are never used to guess attribution."],
                     "privacy": "Recorded diagnostics are sanitized. Raw resumes, email payloads, credentials, submission proof, and model transcripts are excluded."}
    }
    folder = Path(root) / "operations"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix="work-export-", suffix=".json", dir=folder)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
        if path.stat().st_size <= 2:
            raise ValueError("Work export validation failed")
        return path, {"complete": coverage_complete,
                      "warnings": len(missing) + len(errors) + len(truncation)}
    except BaseException:
        path.unlink(missing_ok=True)
        raise

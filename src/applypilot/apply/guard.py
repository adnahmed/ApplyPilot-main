"""Submission intent, immutable artifact bindings and externally verified outcomes.

There are no worker leases, clocks granting permission to resend, or capacity
allocations here. Short database transactions serialize integration idempotency.
Temporal owns execution, and unknown external outcomes remain protected.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from applypilot import postgres
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from applypilot import config
from applypilot.database import commit_with_retry, get_connection, transition_state


class GuardRejected(RuntimeError):
    """Raised when a reservation or state transition is unsafe."""




REPORTED_COMPLETION_KIND = "browser-reported-completion-v1"


_TERMINAL_ATTEMPT_STATUSES = frozenset({
    "submitted", "cancelled_before_submit", "verification_required",
    "verification_quarantined", "verified_absent",
})
def canonical_reason(reason: object, fallback: str = "outcome_unknown") -> str:
    """Preserve diagnostic text; error names never decide whether work retries."""
    return str(reason or "").strip() or fallback


def _reason_detail(reason: object, code: str, detail: object = "") -> str:
    explicit = str(detail or "")
    if explicit:
        return explicit
    value = str(reason or "").strip()
    if not value or value == code:
        return ""
    suffix = value[len(code):] if value.casefold().startswith(code) else value
    return suffix.lstrip(" \t_:;-–—") or value


def terminal_result(attempt: dict) -> dict | None:
    """Translate a persisted terminal attempt into the adapter's stable result."""
    guard_status = str(attempt.get("status") or "")
    if guard_status not in _TERMINAL_ATTEMPT_STATUSES:
        return None
    committed = guard_status == "submitted"
    fallback = "submitted" if committed else guard_status
    return {
        "status": "submitted" if committed else "incomplete",
        "committed": committed,
        "reason": canonical_reason(attempt.get("verification_reason"), fallback),
        "guard_status": guard_status,
        "next_action": "stop",
    }


@dataclass(frozen=True, slots=True)
class ApplicationAttempt:
    attempt_id: str
    application_id: str
    job_url: str
    channel: str
    target: str
    browser_runtime: str
    status: str
    prepared_at: str


def install_attempt_schema(conn: postgres.Connection) -> None:
    """Add snapshot fields in the caller's transaction; never commit here."""
    columns = {row[1] for row in conn.table_info('application_attempts')}
    for name, sql_type in {
        "snapshot_json": "TEXT", "artifact_hashes_json": "TEXT",
        "uploaded_artifacts_json": "TEXT", "browser_uploads_json": "TEXT", "queue_policy_json": "TEXT",
        "assessment_revision": "TEXT", "selected_arrangement": "TEXT",
        "context_key": "TEXT", "route_version": "INTEGER", "application_id": "TEXT",
        "doc_format": "TEXT",
        "document_variant": "TEXT", "role_family": "TEXT", "market": "TEXT",
        "selected_proof_ids_json": "TEXT", "feedback_selection_json": "TEXT",
        "result_recorded_at": "TEXT",
        "browser_submission_protocol": "TEXT",
        "browser_ambiguous_action_at": "TEXT",
        "browser_submit_history_json": "TEXT",
        "browser_capture_invocation": "TEXT",
        "operation_id": "TEXT", "predecessor_attempt_id": "TEXT",
        "adapter_dispatch_protocol": "TEXT", "adapter_invocation_id": "TEXT",
        "adapter_started_at": "TEXT", "adapter_ended_at": "TEXT",
        "cancellation_evidence_json": "TEXT",
    }.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE application_attempts ADD COLUMN {name} {sql_type}")


def upload_artifact_paths(resume, cover, doc_format: str = "pdf", *, require_resume=True) -> dict:
    """Resolve uploadable artifacts without reading/hashing file contents."""
    if doc_format not in {"pdf", "docx"}:
        raise ValueError("unsupported document format")
    output = {}
    for kind, value in (("resume", resume), ("cover_letter", cover)):
        if not value:
            if kind == "resume" and require_resume:
                raise ValueError("resume is required")
            output[kind] = None
            continue
        source = Path(str(value))
        from applypilot.documents.artifacts import resolve_rendered
        native = resolve_rendered(source, doc_format)
        preferred = source.with_suffix(f".{doc_format}")
        chosen = native or (preferred if preferred.is_file() else source)
        if (not chosen.is_file() or chosen.suffix.lower() not in {".pdf", ".docx"}
                or chosen.stat().st_size == 0):
            raise ValueError(f"missing uploadable {kind}")
        output[kind] = (source, chosen)
    return output


def artifact_snapshot(resume, cover, doc_format: str = "pdf", *, require_resume=True, job_url=None) -> str:
    """Hash the selected upload files and their source inputs.

    Queue listing uses ``upload_artifact_paths`` for a cheap readiness check;
    this stronger snapshot is computed only for shortlisted jobs and is repeated
    atomically at reservation time.
    """
    output = {}
    for kind, paths in upload_artifact_paths(resume, cover, doc_format, require_resume=require_resume).items():
        if paths is None:
            output[kind] = None
            continue
        source, chosen = paths
        files = {}
        for path in {source, chosen, source.with_suffix(".txt")}:
            if path.is_file():
                files[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
        output[kind] = {"path": str(chosen.resolve()), "files": files}
        if job_url:
            from applypilot.scoring.artifact_store import manifest_path
            try:
                proof = json.loads(manifest_path(source).read_text(encoding="utf-8"))
                source_hash = files.get(str(source.with_suffix(".txt").resolve()))
                if (proof.get("status") == "approved" and proof.get("job_url") == job_url
                        and proof.get("stage") == ("tailor" if kind == "resume" else "cover")
                        and source_hash and proof.get("sha256") == source_hash
                        and isinstance(proof.get("document_context"), dict)):
                    output[kind]["document_context"] = proof["document_context"]
            except (OSError, ValueError, TypeError):
                pass
    return json.dumps(output, sort_keys=True, separators=(",", ":"))


def attempt_snapshot(job: dict) -> str:
    """Opaque discovery projections avoid a parallel assessment/schema contract."""
    fields = {
        "url", "title", "company", "site", "strategy", "location", "full_description",
        "application_url", "application_route", "route_version", "eligibility",
        "fit_score", "scored_at", "discovered_at", "tailored_resume_path",
        "cover_letter_path", "artifact_snapshot", "selected_evidence", "document_variant",
        "posted_at", "scored_at", "tailored_at", "cover_letter_at", "networking",
        "company_research_revision", "posting_suitability_json",
        "application_contract_json",
    }
    return json.dumps({key: value for key, value in job.items()
                       if key in fields or (key.startswith("discovery_") and key != "discovery_published_at")},
                      sort_keys=True, separators=(",", ":"))


def _current_job(conn, job_url, doc_format):
    row = conn.execute(
        "SELECT r.target AS application_url, j.*, r.route AS application_route, "
        "r.version AS route_version FROM jobs j "
        "JOIN application_routes r ON r.job_url=j.url WHERE j.url=?", (job_url,),
    ).fetchone()
    if row is None:
        raise GuardRejected("job or route no longer exists")
    from applypilot.discovery.repository import hydrate_job
    job = hydrate_job(conn, row)
    from applypilot.apply.application_contract import selected_documents, missing_requirements
    if missing := missing_requirements(job, for_dispatch=True):
        raise GuardRejected("application_requirements: " + "; ".join(item["reason"] for item in missing))
    documents = selected_documents(job)
    try:
        job["artifact_snapshot"] = artifact_snapshot(
            documents["resume"], documents["cover_letter"], doc_format, require_resume=False, job_url=job["url"],
        )
    except (OSError, ValueError) as exc:
        raise GuardRejected(f"artifacts unavailable: {exc}") from exc
    from applypilot.referrals.store import for_application
    job["networking"] = for_application(conn, job["url"])
    return job


def _normalize_company(value: object) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()
    suffix = re.compile(
        r"\s+(private limited|pvt ltd|pvt limited|limited|ltd|llc|"
        r"incorporated|inc|corporation|corp|gmbh|plc|company|co)$"
    )
    while text and (cleaned := suffix.sub("", text)) != text:
        text = cleaned.strip()
    return text


def submission_identity(job: dict) -> str:
    if job.get("application_route") == "email":
        from applypilot.referrals.store import posting_key
        # A recruiting mailbox and a repeated role title are not requisition
        # identities. Preserve exact posting identity (including ATS IDs).
        return "email:" + posting_key({"url": job["url"], "application_url": job["url"]})
    company_value = job.get("company")
    if not company_value and str(job.get("site") or "").lower() not in {
        "linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "jobspy", "freehire",
    }:
        company_value = job.get("site")
    company = _normalize_company(company_value)
    title_words = sorted(
        re.sub(r"[^a-z0-9]+", " ", str(job.get("title") or "").lower()).split()
    )
    title = " ".join(title_words)
    if company and title:
        return f"{company}|{title}"
    return f"job:{job.get('url', '')}"


@lru_cache(maxsize=2048)
def _retired_application_receipt(path, fingerprint):
    """Cache validated immutable evidence only while its file metadata matches."""
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("token") != path.stem:
        raise GuardRejected("historical application evidence is invalid")
    targets = record.get("protected_targets", [])
    if not isinstance(targets, list) or any(not isinstance(target, str) for target in targets):
        raise GuardRejected("historical application targets are invalid")
    return record.get("attempt_id"), tuple(targets)


def _retired_application_conflicts(conn):
    """Retiring physical resources cannot erase missing application history."""
    receipts = {}
    root = config.APP_DIR / "resource-retirements"
    if root.is_symlink():
        raise GuardRejected("historical application evidence is redirected")
    try:
        directory = os.scandir(root)
    except FileNotFoundError:
        return {}
    with directory:
        for entry in directory:
            path = Path(entry.path)
            if not path.match("*.json"):
                continue
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise GuardRejected("historical application evidence is unavailable")
            info = entry.stat(follow_symlinks=False)
            if info.st_size > 262144:
                raise GuardRejected("historical application evidence is unavailable")
            fingerprint = (info.st_dev, entry.inode(), info.st_mtime_ns, info.st_ctime_ns, info.st_size)
            reservation, targets = _retired_application_receipt(path, fingerprint)
            if reservation and targets:
                receipts.setdefault(reservation, set()).update(targets)
    if not receipts:
        return {}
    known = set()
    reservations = list(receipts)
    for offset in range(0, len(reservations), 500):
        batch = reservations[offset:offset + 500]
        known.update(row[0] for row in conn.execute(
            "SELECT attempt_id FROM application_attempts WHERE attempt_id IN ("
            + ",".join("?" for _ in batch) + ")", batch))
    snapshot = {}
    for reservation, targets in receipts.items():
        if reservation in known:
            continue  # The restored durable attempt owns its outcome and guards.
        for target in targets:
            for basis in ("job_url", "target"):
                snapshot.setdefault((basis, target), {
                    "attempt_id": reservation, "status": "verification_required", "basis": basis,
                    "reason": "historical_application_outcome_unknown",
                })
    return snapshot


def application_conflict_snapshot(conn):
    """Index protected attempts once for a read-only queue preview."""
    from applypilot.apply.queue import ACTIVE_ATTEMPTS

    snapshot = _retired_application_conflicts(conn)
    for row in conn.execute(
        "SELECT attempt_id,status,job_url,target,submission_identity,channel "
        f"FROM application_attempts WHERE status IN ({ACTIVE_ATTEMPTS})"
    ):
        for basis in ('job_url', 'target', 'submission_identity'):
            if basis == 'target' and row['channel'] == 'email':
                continue  # A recruiting mailbox serves more than one requisition.
            snapshot.setdefault((basis, row[basis]), {
                'attempt_id': row['attempt_id'], 'status': row['status'], 'basis': basis,
            })
    return snapshot


def find_application_conflict(conn, *, job_url, target, identity, exclude_attempt_id=None,
                              snapshot=None):
    """Use the same protected-attempt rule before selection, allocation and launch."""
    from applypilot.apply.queue import ACTIVE_ATTEMPTS

    email_target = str(target).lower().startswith('mailto:')
    if snapshot is not None and exclude_attempt_id is None:
        return (snapshot.get(('job_url', job_url)) or (None if email_target else snapshot.get(('target', target)))
                or snapshot.get(('submission_identity', identity)))
    row = conn.execute(
        "SELECT attempt_id, status, CASE WHEN job_url=? THEN 'job_url' "
        "WHEN target=? THEN 'target' ELSE 'submission_identity' END AS basis "
        "FROM application_attempts WHERE (? IS NULL OR attempt_id!=?) "
        "AND (job_url=? OR (?=0 AND channel!='email' AND target=?) OR submission_identity=?) "
        f"AND status IN ({ACTIVE_ATTEMPTS}) LIMIT 1",
        (job_url, target, exclude_attempt_id, exclude_attempt_id,
         job_url, int(email_target), target, identity),
    ).fetchone()
    if row is not None:
        return dict(row)
    historical = _retired_application_conflicts(conn)
    conflict = historical.get(("job_url", job_url)) or (
        None if email_target else historical.get(("target", target)))
    return conflict if conflict and conflict["attempt_id"] != exclude_attempt_id else None


def _reject_application_conflict(conflict):
    if conflict:
        raise GuardRejected(
            f"conflicting application attempt: {conflict['attempt_id']} ({conflict['basis']})"
        )


def _application_metadata(job: dict) -> tuple[str, str, str, str]:
    from applypilot.tracking.feedback_store import job_features
    facts=job_features(job)
    return facts['document_variant'],facts['role_family'],facts['market'],json.dumps(facts['proof_ids'])


def _submission_locks(conn, job, target):
    """Transaction-scoped integration deduplication, never worker allocation."""
    identities = {"posting:" + job["url"], "identity:" + submission_identity(job)}
    if not str(target).lower().startswith("mailto:"):
        identities.add("target:" + str(target))
    for key in sorted(identities):
        conn.advisory_lock("submission-idempotency:" + key)
    conn.execute("SELECT url FROM jobs WHERE url=? FOR UPDATE", (job["url"],)).fetchone()
    conn.execute("SELECT job_url FROM application_routes WHERE job_url=? FOR UPDATE", (job["url"],)).fetchone()



def prepare_application_attempt(
    job: dict, *, attempt_id: str, workspace: str, channel: str, target: str,
    browser_runtime: str, operation_id: str, previous_attempt_id: str = "",
    conn: postgres.Connection | None = None,
) -> ApplicationAttempt:
    """Accept one immutable attempt using a Workflow-issued idempotency identity.

    A repeat with the same identity returns the existing business record. A
    different attempt cannot proceed while any submission outcome is unknown.
    No worker is registered, no lease expires, and no retry is scheduled.
    """
    from applypilot.apply.queue import _queue_predicate
    from applypilot.orchestration.contracts import digest, workspace_id
    workspace_id(workspace)
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", attempt_id):
        raise ValueError("A stable Workflow-issued attempt_id is required")
    if not re.fullmatch(r"[a-f0-9]{32,64}", operation_id):
        raise ValueError("A stable Workflow operation_id is required")
    if not job.get("_attempt_snapshot") or attempt_snapshot(job) != job["_attempt_snapshot"]:
        raise GuardRejected("application requires an unchanged prepared selection")
    application_id = digest({"workspace": workspace, "posting": job["url"]})
    conn = conn or get_connection()
    if conn.in_transaction:
        raise GuardRejected("attempt preparation requires a short independent transaction")
    now = datetime.now(UTC).isoformat()
    conn.execute("BEGIN")
    try:
        _submission_locks(conn, job, target)
        existing = conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing is not None:
            if (existing["application_id"] != application_id or existing["operation_id"] != operation_id
                    or existing["channel"] != channel
                    or existing["target"] != target or existing["snapshot_json"] != job["_attempt_snapshot"]):
                raise GuardRejected("attempt identity was reused for different application input")
            conn.rollback()
            return ApplicationAttempt(*(existing[field] for field in (
                "attempt_id", "application_id", "job_url", "channel", "target",
                "browser_runtime", "status", "prepared_at")))
        from applypilot.apply.attempt_lifecycle import safe_to_retry
        latest = conn.execute("SELECT * FROM application_attempts WHERE job_url=? "
                              "ORDER BY prepared_at DESC,attempt_id DESC LIMIT 1", (job["url"],)).fetchone()
        if latest is not None:
            if latest["attempt_id"] != previous_attempt_id or not safe_to_retry(dict(latest)):
                raise GuardRejected("Prior submission has no verified non-submission authorization")
        elif previous_attempt_id:
            raise GuardRejected("The specified previous attempt does not exist")
        policy = dict(job.get("_queue_policy") or {})
        clauses, params = _queue_predicate(policy.get("min_score"), policy.get("max_score"),
                                          policy.get("max_age_days"), policy.get("route_kind"))
        eligible = conn.execute("SELECT 1 FROM jobs j JOIN application_routes r ON r.job_url=j.url "
                                "WHERE j.url=? AND " + " AND ".join(clauses), (job["url"], *params)).fetchone()
        if not eligible:
            raise GuardRejected("application input is no longer eligible")
        current = _current_job(conn, job["url"], job.get("_doc_format", "pdf"))
        from applypilot.apply.routes import application_route_blocker
        if reason := application_route_blocker(current):
            raise GuardRejected(reason)
        if (current["application_route"] != channel or current["application_url"] != target
                or attempt_snapshot(current) != job["_attempt_snapshot"]):
            raise GuardRejected("route, applicant evidence or approved artifacts changed")
        _reject_application_conflict(find_application_conflict(
            conn, job_url=job["url"], target=target, identity=submission_identity(current)))
        variant, family, market, proof_ids = _application_metadata(current)
        conn.execute("""INSERT INTO application_attempts
            (attempt_id,application_id,job_url,submission_identity,channel,target,browser_runtime,
             status,prepared_at,updated_at,browser_submission_protocol,snapshot_json,
             artifact_hashes_json,queue_policy_json,assessment_revision,selected_arrangement,
             context_key,route_version,doc_format,document_variant,role_family,market,selected_proof_ids_json)
            VALUES (?,?,?,?,?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (attempt_id, application_id, job["url"], submission_identity(current), channel, target,
             browser_runtime, now, now, "owned-submit-v2" if browser_runtime == "browseros" else None,
             job["_attempt_snapshot"], current["artifact_snapshot"], json.dumps(policy),
             current.get("discovery_assessment_revision"), current.get("discovery_selected_arrangement"),
             current.get("discovery_context_key"), current["route_version"], job.get("_doc_format", "pdf"),
             variant, family, market, proof_ids))
        conn.execute("UPDATE application_attempts SET operation_id=?,predecessor_attempt_id=?,"
                     "adapter_dispatch_protocol='temporal-dispatch-v1' WHERE attempt_id=?",
                     (operation_id, previous_attempt_id or None, attempt_id))
        from applypilot.tracking.feedback_learning import log_decision
        selection = log_decision(conn, job, attempt_id)
        conn.execute("UPDATE application_attempts SET feedback_selection_json=? WHERE attempt_id=?",
                     (json.dumps(selection, sort_keys=True), attempt_id))
        conn.execute("UPDATE jobs SET application_attempt_id=?,apply_status='in_progress',last_attempted_at=? "
                     "WHERE url=? AND applied_at IS NULL", (attempt_id, now, job["url"]))
        transition_state(conn, job["url"], "applying", reason="application prepared",
                         metadata={"application_id": application_id, "attempt_id": attempt_id}, force=True)
        conn.commit()
        return ApplicationAttempt(attempt_id, application_id, job["url"], channel, target,
                                  browser_runtime, "prepared", now)
    except postgres.IntegrityError as exc:
        conn.rollback()
        raise GuardRejected("another protected attempt already represents this application") from exc
    except BaseException:
        conn.rollback()
        raise


def _load_attempt(conn: postgres.Connection, attempt_id: str, *, live: bool = False) -> postgres.Row:
    if not attempt_id:
        raise GuardRejected("application attempt identity is required")
    # Reads do not open an implicit long-lived transaction. Mutators lock the
    # business row only for their own explicit transaction.
    suffix = " FOR UPDATE" if conn.in_transaction else ""
    row = conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?" + suffix, (attempt_id,)).fetchone()
    if row is None:
        raise GuardRejected("unknown application attempt")
    if live:
        current = conn.execute("SELECT application_attempt_id,applied_at FROM jobs WHERE url=?" + suffix,
                               (row["job_url"],)).fetchone()
        if not current or current["application_attempt_id"] != attempt_id or current["applied_at"]:
            raise GuardRejected("application attempt has been superseded or submitted")
    return row


def validate_attempt(attempt_id: str, *, conn=None, submitting: bool = False) -> dict:
    """Keep claimed work owned and unique; admission was checked when reserving it."""
    conn = conn or get_connection()
    attempt = _load_attempt(conn, attempt_id, live=True)
    if attempt["status"] != ("side_effect_intent" if submitting else "prepared"):
        raise GuardRejected(f"cannot launch or submit from {attempt['status']}")
    if not submitting:
        from applypilot.integrations.research import posting_suitability_blocker
        job = conn.execute("SELECT * FROM jobs WHERE url=?", (attempt["job_url"],)).fetchone()
        reason = posting_suitability_blocker(dict(job)) if job else "company_research_pending"
        if reason:
            raise GuardRejected(reason)
        from applypilot.referrals.store import hold_sql
        if not conn.execute("SELECT 1 FROM jobs j WHERE j.url=? AND " + hold_sql("j"),
                            (attempt["job_url"],)).fetchone():
            raise GuardRejected("referral_hold")
    _reject_application_conflict(find_application_conflict(
        conn, job_url=attempt["job_url"], target=attempt["target"],
        identity=attempt["submission_identity"], exclude_attempt_id=attempt_id,
    ))
    return dict(attempt)


def bind_uploaded_artifacts(attempt_id: str, documents: dict, *, conn=None) -> None:
    """Check worker copies against the reserved hashes before exposing the agent."""
    conn = conn or get_connection()
    conn.execute("BEGIN")
    try:
        attempt = validate_attempt(attempt_id, conn=conn)
        manifest = json.loads(attempt["artifact_hashes_json"])
        copies = {}
        for kind, artifact in manifest.items():
            if artifact is None:
                if documents.get(kind):
                    raise GuardRejected("unexpected worker artifact")
                continue
            path = Path(documents.get(kind) or "")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != artifact["files"][artifact["path"]]:
                raise GuardRejected("worker copy does not match reserved artifact")
            copies[str(path.resolve())] = digest
        conn.execute("UPDATE application_attempts SET uploaded_artifacts_json=? WHERE attempt_id=?",
                     (json.dumps(copies, sort_keys=True), attempt_id))
        commit_with_retry(conn)
    except Exception:
        conn.rollback()
        raise


def begin_browser_upload(attempt_id: str, document: str, page: int, *, conn=None) -> dict:
    """Resolve only a bound worker copy and persist an unresolved upload first."""
    conn = conn or get_connection()
    conn.execute("BEGIN")
    try:
        attempt = validate_attempt(attempt_id, conn=conn)
        artifact = json.loads(attempt["artifact_hashes_json"]).get(document)
        if not artifact:
            raise FileNotFoundError("The requested application document is not available.")
        digest = artifact["files"][artifact["path"]]
        copies = json.loads(attempt["uploaded_artifacts_json"] or "{}")
        paths = [Path(path) for path, value in copies.items() if value == digest]
        if not paths:
            raise GuardRejected("No bound worker copy matches this application document.")
        path = paths[0]
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise GuardRejected("Application document changed before upload.")
        receipt = {"status": "pending", "page": page, "path": str(path),
                   "sha256": digest, "name": path.name, "size": path.stat().st_size}
        state = json.loads(attempt.get("browser_uploads_json") or "{}")
        state.setdefault("documents", {})[document] = receipt
        conn.execute("UPDATE application_attempts SET browser_uploads_json=? WHERE attempt_id=?",
                     (json.dumps(state, sort_keys=True), attempt_id))
        commit_with_retry(conn)
        return receipt
    except Exception:
        conn.rollback()
        raise


def finish_browser_upload(attempt_id: str, document: str, page: int, observed: dict, *, conn=None) -> dict:
    """Accept only adapter-read File metadata whose bytes match the reserved PDF/DOCX."""
    conn = conn or get_connection()
    conn.execute("BEGIN")
    try:
        attempt = validate_attempt(attempt_id, conn=conn)
        state = json.loads(attempt.get("browser_uploads_json") or "{}")
        receipt = state.get("documents", {}).get(document)
        if not receipt or receipt.get("status") != "pending" or receipt.get("page") != page:
            raise GuardRejected("Upload receipt is missing or belongs to a different page; inspect the input again.")
        if observed.get("verified") is not True or type(observed.get("backend_node_id")) is not int:
            raise GuardRejected("The browser did not return verifiable file metadata; the upload remains incomplete.")
        mismatches = [key for key in ("sha256", "name", "size") if observed.get(key) != receipt[key]]
        if mismatches:
            raise GuardRejected("Uploaded file metadata differs from the approved document: " + ", ".join(mismatches))
        receipt.update(status="verified", input_key=f"{page}:{observed['backend_node_id']}",
                       verified_at=datetime.now(UTC).isoformat())
        required = state.get("required_inputs", {})
        if receipt["input_key"] in required:
            required[receipt["input_key"]] = True
        conn.execute("UPDATE application_attempts SET browser_uploads_json=? WHERE attempt_id=?",
                     (json.dumps(state, sort_keys=True), attempt_id))
        commit_with_retry(conn)
        return {"document": document, "verified": True, "name": receipt["name"], "size": receipt["size"]}
    except Exception:
        conn.rollback()
        raise


def record_required_browser_uploads(attempt_id: str, page: int, inputs: list[dict], *, conn=None) -> None:
    """Remember inspected required inputs, allowing an existing selected file."""
    conn = conn or get_connection()
    conn.execute("BEGIN")
    try:
        attempt = validate_attempt(attempt_id, conn=conn)
        state = json.loads(attempt.get("browser_uploads_json") or "{}")
        required = state.setdefault("required_inputs", {})
        for key in list(required):
            if key.startswith(f"{page}:"):
                del required[key]
        for item in inputs:
            input_key = f"{page}:{item['backend_node_id']}"
            if item.get("has_file") is not True:
                for receipt in state.get("documents", {}).values():
                    if receipt.get("input_key") == input_key:
                        receipt["status"] = "missing"
            if item.get("required") is True and item.get("disabled") is not True:
                required[input_key] = item.get("has_file") is True
        conn.execute("UPDATE application_attempts SET browser_uploads_json=? WHERE attempt_id=?",
                     (json.dumps(state, sort_keys=True), attempt_id))
        commit_with_retry(conn)
    except Exception:
        conn.rollback()
        raise


def mark_side_effect_intent(
    attempt_id: str,
    *,
    email_payload_hash: str | None = None,
    email_transport: str | None = None,
    conn: postgres.Connection | None = None,
) -> dict:
    """Persist the point after which Submit/Send may have happened."""
    conn = conn or get_connection()
    now = datetime.now(UTC).isoformat()
    conn.execute("BEGIN")
    try:
        row = validate_attempt(attempt_id, conn=conn)
        if row.get("adapter_dispatch_protocol") == "temporal-dispatch-v1":
            if not row.get("adapter_invocation_id") or row.get("adapter_ended_at"):
                raise GuardRejected("The guarded adapter invocation is not active")
            from applypilot.apply.provider_policy import enforce
            enforce(row, conn=conn, at_intent=True)
            if row["channel"] != "email" and row["browser_runtime"] != "browseros":
                raise GuardRejected("Live browser submission requires an intent-fenced adapter")
        if row["browser_runtime"] == "browseros":
            if browser_activity_uncertain(dict(row)):
                raise GuardRejected("Earlier browser activation has an unknown submission outcome; verification is required.")
            from applypilot.apply.browseros_mcp import read_state
            bridge_path = config.APP_DIR / "browseros-sessions" / (attempt_id + ".json")
            bridge = read_state(bridge_path)
            if not bridge.get("invocation"):
                raise GuardRejected("The browser submission session is unavailable.")
        if not row["uploaded_artifacts_json"]:
            raise GuardRejected("worker upload artifacts have not been bound")
        if row["channel"] == "email":
            from applypilot.apply.email_application import validate_intent
            validate_intent(row, conn, expected_payload_hash=email_payload_hash, expected_transport=email_transport)
        conn.execute(
            "UPDATE application_attempts SET status='side_effect_intent', "
            "side_effect_intent_at=?, updated_at=? WHERE attempt_id=?",
            (now, now, attempt_id),
        )
        if row["browser_runtime"] == "browseros":
            history = json.loads(row["browser_submit_history_json"] or "[]")
            history.append({"intent_at": now, "invocation": bridge["invocation"], "disposition": "unknown"})
            conn.execute("UPDATE application_attempts SET browser_submit_history_json=?, browser_capture_invocation=? "
                         "WHERE attempt_id=?", (json.dumps(history), bridge["invocation"], attempt_id))
        commit_with_retry(conn)
        return dict(_load_attempt(conn, attempt_id))
    except Exception:
        conn.rollback()
        raise


def record_submission_report(
    attempt_id: str,
    report: dict,
    *,
    source: str = "agent",
    observed_at: str | None = None,
    duration_ms: int | None = None,
    conn: postgres.Connection | None = None,
) -> dict:
    """Reconcile captured success; prose alone cannot certify an application."""
    if report.get("status") not in {"submitted", "already-submitted"} or source not in {"agent", "user"}:
        raise GuardRejected("an explicit successful submission report is required")
    detail = str(report.get("proof") or report.get("reason") or "").strip()
    if not detail:
        if source == "user":
            raise GuardRejected("describe the observed submission confirmation")
        detail = "Agent reported a submitted outcome without confirmation details"
    if source == "agent":
        from applypilot.apply.submission_evidence import reconcile_captured_submission
        result = reconcile_captured_submission(attempt_id, conn=conn)
        if result.get("disposition") == "confirmed":
            return result["attempt"]
        from applypilot.discovery.repository import transaction
        conn = conn or get_connection()
        with transaction(conn):
            row = _load_attempt(conn, attempt_id)
            if row["status"] == "prepared":
                conn.execute("UPDATE application_attempts SET browser_ambiguous_action_at=COALESCE("
                             "browser_ambiguous_action_at,?) WHERE attempt_id=?",
                             (datetime.now(UTC).isoformat(), attempt_id))
        return require_verification(attempt_id, reason="unconfirmed_submission", quarantine=True,
                                    detail=json.dumps({"reported_url": str(report.get("current_url") or ""),
                                                       "agent_report": detail}, ensure_ascii=False), conn=conn)
    proof = json.dumps({
        "attempt_id": attempt_id, "source": source, "status": "submitted",
        "current_url": str(report.get("current_url") or ""), "completion": detail,
        "observed_at": observed_at or datetime.now(UTC).isoformat(),
    })
    return mark_submitted(attempt_id, proof_kind=REPORTED_COMPLETION_KIND, proof=proof,
                          duration_ms=duration_ms, conn=conn)


def mark_submitted(
    attempt_id: str,
    *,
    proof_kind: str,
    proof: str,
    duration_ms: int | None = None,
    conn: postgres.Connection | None = None,
) -> dict:
    """Commit captured or explicitly reported success once for the owning attempt."""
    if not proof_kind.strip() or not proof.strip():
        raise GuardRejected("submission proof is required")
    from applypilot.discovery.repository import transaction

    conn = conn or get_connection()
    now = datetime.now(UTC).isoformat()
    with transaction(conn, immediate=False):
        row = _load_attempt(conn, attempt_id)
        if row["status"] == "submitted":
            return dict(row)
        reported = proof_kind == REPORTED_COMPLETION_KIND
        browser = row["browser_runtime"] == "browseros"
        allowed = {"side_effect_intent"}
        if browser or row["channel"] == "email":
            allowed.update({"verification_required", "verification_quarantined"})
        if reported:
            allowed.add("prepared")
        if row["status"] not in allowed:
            raise GuardRejected(f"cannot submit from {row['status']}")
        submitted_at = now
        confidence = "explicit-proof"
        if reported:
            if not browser or row["channel"] == "email":
                raise GuardRejected("completion reports apply only to browser submissions")
            try:
                report = json.loads(proof)
                if (report.get("source") != "user" or report.get("attempt_id") != attempt_id
                        or report.get("status") != "submitted" or not str(report.get("completion") or "").strip()):
                    raise ValueError("only explicit user confirmation can replace captured browser evidence")
                observed = datetime.fromisoformat(report["observed_at"])
                if (observed.tzinfo is None
                        or not datetime.fromisoformat(row["prepared_at"]) <= observed <= datetime.fromisoformat(now)):
                    raise ValueError("invalid submission time")
            except (KeyError, TypeError, ValueError) as exc:
                raise GuardRejected("invalid submission report") from exc
            submitted_at = observed.astimezone(UTC).isoformat()
            confidence = "user-confirmed"
        elif row["channel"] == "email":
            # Provider proof records an already completed side effect, including
            # after a lease expires. It never grants permission for another send.
            from applypilot.apply.email_application import validate_delivery_proof
            validate_delivery_proof(row, proof_kind, proof, conn)
        elif browser:
            from applypilot.apply.submission_evidence import validate_existing_capture
            proof = validate_existing_capture(row, proof_kind, proof, conn=conn)
        else:
            validate_attempt(attempt_id, conn=conn, submitting=True)
        if row["channel"] == "linkedin-easy-apply":
            conn.execute("INSERT INTO linkedin_activity(event_type,job_url,occurred_at,metadata) "
                         "VALUES('confirmed_submission',?,?,?)",
                         (row["job_url"], submitted_at, json.dumps({"attempt_id": attempt_id})))
        conn.execute(
            """
            UPDATE application_attempts
               SET status='submitted', submitted_at=?, proof_kind=?, proof=?, updated_at=?, verification_reason=NULL
             WHERE attempt_id=?
            """,
            (submitted_at, proof_kind, proof.strip(), now, attempt_id),
        )
        conn.execute(
            """
            UPDATE jobs
               SET apply_status='applied', apply_category='applied', applied_at=?,
                   apply_error=NULL, apply_duration_ms=?,
                   verification_confidence=?
             WHERE url=? AND application_attempt_id=?
            """,
            (submitted_at, duration_ms, confidence, row["job_url"], attempt_id),
        )
        transition_state(
            conn,
            row["job_url"],
            "applied",
            reason="reported submission confirmed" if reported else "guarded submission confirmed",
            metadata={"attempt_id": attempt_id, "proof_kind": proof_kind,
                      **{key: value for key, value in json.loads(row["queue_policy_json"] or "{}").items()
                         if key in {"operation_run_id", "operation_span_id"}}},
            force=True,
        )
        from applypilot.tracking.feedback_store import capture_submission
        capture_submission(conn, attempt_id, origin=confidence if reported else "guarded")
        return dict(_load_attempt(conn, attempt_id))


def record_outcome_unknown(
    attempt_id: str,
    *,
    reason: str,
    quarantine: bool = False,
    detail: str = "",
    conn: postgres.Connection | None = None,
) -> dict:
    """Retain possible submission effects; release confirmed pre-submit failures."""
    conn = conn or get_connection()
    now = datetime.now(UTC).isoformat()
    target_status = "verification_required"
    reason_code = canonical_reason(reason, target_status)
    reason_detail = _reason_detail(reason, reason_code, detail)
    conn.execute("BEGIN")
    try:
        row = _load_attempt(conn, attempt_id)
        if row["status"] == "submitted":
            conn.rollback()
            return dict(row)
        if row["status"] == "prepared" and not browser_activity_uncertain(dict(row)):
            conn.rollback()
            return cancel_before_submit(attempt_id, reason=reason, detail=detail, conn=conn)
        if row["status"] in {"verification_required", "verification_quarantined"}:
            conn.rollback()
            return dict(row)
        if row["status"] not in {"prepared", "side_effect_intent"}:
            raise GuardRejected(f"cannot record an unknown outcome from {row['status']}")
        conn.execute(
            "UPDATE application_attempts SET status=?, verification_reason=?, updated_at=? "
            "WHERE attempt_id=?",
            (target_status, reason_code, now, attempt_id),
        )
        conn.execute(
            "UPDATE jobs SET apply_status='failed', apply_category=?, "
            "apply_error=? WHERE url=? AND application_attempt_id=?",
            (target_status, reason_detail or reason_code, row["job_url"], attempt_id),
        )
        transition_state(
            conn,
            row["job_url"],
            target_status,
            reason=reason_detail or reason_code,
            metadata={"attempt_id": attempt_id, "reason_code": reason_code,
                      **({"detail": reason_detail} if reason_detail else {})},
            force=True,
        )
        commit_with_retry(conn)
        return dict(_load_attempt(conn, attempt_id))
    except Exception:
        conn.rollback()
        raise


# Persisted histories and older workers can still call the original adapter API.
require_verification = record_outcome_unknown


def browser_activity_uncertain(attempt: dict) -> bool:
    """Only an actual or ambiguous submit action leaves an unknown outcome."""
    if attempt.get("browser_ambiguous_action_at") or attempt.get("side_effect_intent_at"):
        return True
    if attempt.get("browser_runtime") != "browseros":
        return False
    from applypilot.apply.browseros_mcp import read_state
    bridge = read_state(config.APP_DIR / "browseros-sessions" / (attempt["attempt_id"] + ".json"))
    if bridge.get("submission_attempted_at") and attempt.get("status") == "prepared":
        # The database commits a rejected/prevented click before the bridge
        # clears its marker. A process exit between those writes is retryable.
        history = json.loads(attempt.get("browser_submit_history_json") or "[]")
        latest = history[-1] if history else {}
        receipt = latest.get("prevention") or latest.get("rejection") or latest.get("transition") or {}
        if (latest.get("disposition") in {"pre_submit", "rejected", "intermediate"}
                and receipt.get("intent_at") == latest.get("intent_at")
                and receipt.get("invocation") == attempt.get("browser_capture_invocation")
                and receipt.get("target") == attempt.get("target")
                and receipt.get("kind") in {"adapter-validation-no-activation-v1", "browser-validation-rejected-v1", "browser-intermediate-step-v1"}):
            return False
    return bool(bridge.get("submission_attempted_at"))


def resume_browser_rejected_submission(attempt_id: str, *, conn=None, intermediate: bool = False) -> dict:
    """Resume after a proven rejection or a distinct unfinished application step."""
    from applypilot.apply.browseros_mcp import read_state, bridge_health
    conn = conn or get_connection()
    conn.execute("BEGIN")
    try:
        row = _load_attempt(conn, attempt_id, live=True)
        path = config.APP_DIR / "browseros-sessions" / (attempt_id + ".json")
        bridge = read_state(path)
        receipt = bridge.get("submission_transition" if intermediate else "submission_rejected") or {}
        validation = receipt.get("validation") or {}
        expected_kind = "browser-intermediate-step-v1" if intermediate else "browser-validation-rejected-v1"
        valid_outcome = (validation.get("rejected") is True
                         and bool(validation.get("fields") or validation.get("messages")))
        if intermediate:
            try:
                from applypilot.apply.browser_controls import intermediate_form_transition
                prepared = bridge.get("submission_prepared") or {}
                checked = intermediate_form_transition(prepared, {
                    "url": validation.get("page_url"), "pending": validation.get("pending"),
                    "forms": [{"identity": validation.get("current_form"),
                               "controls": validation.get("current_controls")}],
                })
                valid_outcome = (validation.get("advanced") is True and checked is not None
                                 and checked == validation
                                 and isinstance((prepared.get("target") or {}).get("frame_id"), str))
            except (ValueError, TypeError, KeyError):
                valid_outcome = False
        try:
            observed = datetime.fromisoformat(receipt.get("recorded_at", ""))
            fresh = 0 <= (datetime.now(UTC) - observed).total_seconds() <= 60
        except (ValueError, TypeError):
            fresh = False
        if (row["status"] != "side_effect_intent" or row["browser_runtime"] != "browseros"
                or row["browser_ambiguous_action_at"] or bridge_health(path) is not True
                or receipt.get("kind") != expected_kind
                or receipt.get("clicked") is not True or not fresh
                or receipt.get("intent_at") != row["side_effect_intent_at"]
                or not receipt.get("invocation")
                or receipt["invocation"] != row["browser_capture_invocation"]
                or receipt["invocation"] != bridge.get("invocation")
                or receipt.get("target") != row["target"]
                or (bridge.get("bound_pages") or {}).get(str(receipt.get("page_id"))) != row["target"]
                or validation.get("completion_visible") is not False
                or validation.get("pending") is not False
                or not valid_outcome):
            raise GuardRejected("No current browser observation establishes a safe form continuation.")
        history = json.loads(row["browser_submit_history_json"] or "[]")
        if not history or history[-1].get("intent_at") != row["side_effect_intent_at"]:
            raise GuardRejected("Submission history does not match the rejection observation.")
        history[-1] = {**history[-1], "disposition": "intermediate" if intermediate else "rejected",
                       "transition" if intermediate else "rejection": receipt}
        conn.execute("UPDATE application_attempts SET status='prepared', side_effect_intent_at=NULL, "
                     "browser_submit_history_json=?, updated_at=? WHERE attempt_id=?",
                     (json.dumps(history), datetime.now(UTC).isoformat(), attempt_id))
        commit_with_retry(conn)
        return dict(_load_attempt(conn, attempt_id))
    except Exception:
        conn.rollback()
        raise


def defer_application(attempt_id: str, reason_code: str, *, evidence_refs=None,
                      detail: str = "", quarantine: bool = False) -> dict:
    """Record an unresolved business outcome; never put it in a retry queue."""
    attempt = attempt_status(attempt_id)
    if terminal_result(attempt) is not None:
        return attempt
    return record_outcome_unknown(attempt_id, reason=reason_code, detail=detail)


def cancel_before_submit(attempt_id: str, *, reason: str, detail: str = "",
                         conn: postgres.Connection | None = None) -> dict:
    """Revoke this attempt only when its adapter proves no submit action began."""
    conn = conn or get_connection()
    now = datetime.now(UTC).isoformat()
    code = canonical_reason(reason, "cancelled_before_submit")
    detail = _reason_detail(reason, code, detail)
    conn.execute("BEGIN")
    try:
        row = _load_attempt(conn, attempt_id, live=True)
        if row["status"] == "cancelled_before_submit":
            conn.rollback()
            return dict(row)
        if row["status"] != "prepared":
            raise GuardRejected("cannot cancel after " + row["status"])
        if browser_activity_uncertain(dict(row)):
            conn.rollback()
            return require_verification(attempt_id, reason="browser_activity_uncertain",
                detail="No reliable pre-submit boundary is available.", quarantine=True, conn=conn)
        conn.execute("UPDATE application_attempts SET status='cancelled_before_submit', "
                     "verification_reason=?,updated_at=? WHERE attempt_id=?", (code, now, attempt_id))
        from applypilot.apply.attempt_lifecycle import cancellation_evidence
        conn.execute("UPDATE application_attempts SET cancellation_evidence_json=? WHERE attempt_id=?",
                     (json.dumps(cancellation_evidence(dict(row), code), sort_keys=True), attempt_id))
        conn.execute("UPDATE jobs SET apply_status=NULL,apply_error=? WHERE url=? AND application_attempt_id=?",
                     (detail or code, row["job_url"], attempt_id))
        transition_state(conn, row["job_url"], "ready_to_apply", reason=detail or code,
                         metadata={"attempt_id": attempt_id, "reason_code": code}, force=True)
        conn.commit()
        return dict(_load_attempt(conn, attempt_id))
    except BaseException:
        conn.rollback()
        raise


def attempt_status(attempt_id: str, conn: postgres.Connection | None = None) -> dict:
    conn = conn or get_connection()
    return dict(_load_attempt(conn, attempt_id))


def historical_attempt_status(attempt_id: str, conn: postgres.Connection | None = None) -> dict | None:
    """Read cleanup evidence without claiming that this attempt still owns its job."""
    conn = conn or get_connection()
    row = conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
    return dict(row) if row is not None else None

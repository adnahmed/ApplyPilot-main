"""Reopen legacy guessed failures only with attributable no-submission evidence."""
from datetime import UTC, datetime
import json

from applypilot.apply.attempt_lifecycle import PROTOCOL, cancellation_evidence, safe_to_retry
from .retire_execution import _archive_schema

MIGRATION = "application-evidence-recovery-v1"


def can_reopen(job, attempt, transition, *, browser_outcome_unknown=True):
    """An adapter-created failure is distinct from an owner's exclusion."""
    if (job.get("applied_at") or job.get("state") in {"applied", "archived"}
            or job.get("apply_status") == "applied"
            or job.get("eligibility") != "eligible"
            or job.get("discovery_source_status") == "closed"
            or job.get("apply_category") in {"archived_ineligible", "archived_expired", "archived_no_url"}
            or job.get("application_attempt_id") != attempt.get("attempt_id")
            or job.get("url") != attempt.get("job_url")):
        return False
    if (attempt.get("adapter_dispatch_protocol") != PROTOCOL
            or not attempt.get("adapter_invocation_id") or not attempt.get("adapter_ended_at")
            or attempt.get("status") not in {"cancelled_before_submit", "verification_required", "verification_quarantined"}
            or attempt.get("side_effect_intent_at") or attempt.get("browser_ambiguous_action_at")
            or browser_outcome_unknown):
        return False
    try:
        metadata = json.loads(transition.get("metadata") or "{}")
        ended = datetime.fromisoformat(attempt["adapter_ended_at"])
        if (not isinstance(metadata, dict) or metadata.get("attempt_id") != attempt["attempt_id"]
                or not set(metadata) <= {"attempt_id", "category", "reason_code", "detail"}
                or "reason_code" not in metadata or transition.get("to_state") != job.get("state")
                or ended.tzinfo is None or ended > datetime.now(UTC)):
            return False
        if attempt["status"] == "cancelled_before_submit":
            return safe_to_retry(attempt)
        return bool(cancellation_evidence(attempt, "Recovered pre-submit outcome"))
    except (KeyError, TypeError, ValueError):
        return False


def migrate(conn):
    """Run inside explicit schema initialization; archive and update atomically."""
    from applypilot.apply.guard import browser_activity_uncertain
    from applypilot.database import transition_state

    _archive_schema(conn)
    rows = conn.execute("""SELECT to_jsonb(j)::text AS job_json, to_jsonb(a)::text AS attempt_json,
            (SELECT to_jsonb(t)::text FROM job_state_transitions t
             WHERE t.job_url=j.url ORDER BY t.id DESC LIMIT 1) AS transition_json
        FROM jobs j JOIN application_attempts a ON a.attempt_id=j.application_attempt_id
        WHERE j.state IN ('manual_only','verification_required','verification_quarantined')
           OR j.apply_status='manual'
        FOR UPDATE OF j,a""").fetchall()
    reopened = 0
    for row in rows:
        job = json.loads(row["job_json"])
        attempt = json.loads(row["attempt_json"])
        transition = json.loads(row["transition_json"] or "{}")
        if not can_reopen(job, attempt, transition,
                          browser_outcome_unknown=browser_activity_uncertain(attempt)):
            continue
        archived = {
            "job": {key: job.get(key) for key in ("url", "state", "apply_status", "apply_category", "apply_error")},
            "attempt": {key: attempt.get(key) for key in ("attempt_id", "status", "verification_reason",
                                                         "cancellation_evidence_json", "updated_at")},
            "transition": transition,
        }
        conn.execute("""INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
            VALUES(?,?,?,?) ON CONFLICT(migration_id,source_table,row_identity) DO NOTHING""",
            (MIGRATION, "application_attempts.recovery", attempt["attempt_id"], json.dumps(archived, sort_keys=True)))
        proof = cancellation_evidence(attempt, attempt.get("verification_reason") or "Recovered pre-submit outcome")
        conn.execute("UPDATE application_attempts SET status='cancelled_before_submit',cancellation_evidence_json=?,"
                     "updated_at=? WHERE attempt_id=?",
                     (json.dumps(proof, sort_keys=True), datetime.now(UTC).isoformat(), attempt["attempt_id"]))
        conn.execute("UPDATE jobs SET apply_status=NULL,apply_category='pending',apply_error=NULL WHERE url=?",
                     (job["url"],))
        transition_state(conn, job["url"], "ready_to_apply", reason="Recovered confirmed pre-submit failure",
                         metadata={"attempt_id": attempt["attempt_id"], "migration": MIGRATION}, force=True)
        reopened += 1
    return reopened

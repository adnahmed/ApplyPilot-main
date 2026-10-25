"""Durable external-action evidence, not an execution lease or recovery queue.

A dispatch record is append-once side-effect intent for *one* adapter invocation.
It never expires or grants worker capacity. Activity delivery duplication cannot
launch another browser/send. Only proven negative evidence authorizes a new ID.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

PROTOCOL = "temporal-dispatch-v1"
NEGATIVE = "guarded-cancel-before-submit-v1"
REVIEW = "operator-verified-non-submission-v1"
PROVIDER_NEGATIVE = "adapter-observed-non-submission-v1"


def cancellation_evidence(attempt, reason):
    if (attempt.get("adapter_dispatch_protocol") != PROTOCOL or not attempt.get("snapshot_json")
            or attempt.get("side_effect_intent_at") or attempt.get("browser_ambiguous_action_at")):
        return {}  # A legacy absence of intent is not evidence of absence.
    return {"kind": NEGATIVE, "attempt_id": attempt["attempt_id"], "operation_id": attempt["operation_id"],
            "snapshot_sha256": hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest(),
            "reason": reason, "observed_at": datetime.now(UTC).isoformat()}


def safe_to_retry(attempt):
    try:
        if attempt.get("status") == "verified_absent":
            proof = json.loads(attempt.get("proof") or "{}")
            if attempt.get("proof_kind") == PROVIDER_NEGATIVE:
                return (attempt.get("adapter_dispatch_protocol") == PROTOCOL
                        and bool(attempt.get("operation_id"))
                        and proof.get("kind") == PROVIDER_NEGATIVE
                        and proof.get("attempt_id") == attempt["attempt_id"]
                        and proof.get("operation_id") == attempt.get("operation_id")
                        and proof.get("snapshot_sha256") == hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest()
                        and proof.get("adapter_ended_at") == attempt.get("adapter_ended_at")
                        and bool(attempt.get("adapter_ended_at"))
                        and _provider_rejection_matches(attempt, proof.get("receipt") or {}))
            return (attempt.get("proof_kind") == REVIEW and proof.get("attempt_id") == attempt["attempt_id"]
                    and bool(proof.get("reviewed_by")) and bool(proof.get("provider_evidence"))
                    and proof.get("adapter_stopped") is True and bool(proof.get("adapter_stop_evidence")))
        proof = json.loads(attempt.get("cancellation_evidence_json") or "{}")
        return (attempt.get("status") == "cancelled_before_submit"
                and attempt.get("adapter_dispatch_protocol") == PROTOCOL
                and not attempt.get("side_effect_intent_at") and not attempt.get("browser_ambiguous_action_at")
                and (not attempt.get("adapter_invocation_id") or bool(attempt.get("adapter_ended_at")))
                and proof.get("kind") == NEGATIVE and proof.get("attempt_id") == attempt["attempt_id"]
                and proof.get("operation_id") == attempt.get("operation_id")
                and proof.get("snapshot_sha256") == hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest())
    except (TypeError, ValueError, KeyError):
        return False


def _provider_rejection_matches(attempt, receipt):
    validation = receipt.get("validation") or {}
    try:
        observed = datetime.fromisoformat(receipt["recorded_at"])
        intended = datetime.fromisoformat(attempt["side_effect_intent_at"])
        ended = datetime.fromisoformat(attempt["adapter_ended_at"])
        return (not attempt.get("browser_ambiguous_action_at")
                and attempt.get("browser_runtime") == "browseros"
                and receipt.get("kind") == "browser-validation-rejected-v1"
                and receipt.get("clicked") is True
                and receipt.get("intent_at") == attempt["side_effect_intent_at"]
                and receipt.get("target") == attempt["target"]
                and bool(receipt.get("invocation"))
                and receipt["invocation"] == attempt.get("browser_capture_invocation")
                and intended <= observed <= ended <= datetime.now(UTC)
                and validation.get("rejected") is True
                and validation.get("completion_visible") is False
                and validation.get("pending") is False
                and bool(validation.get("fields") or validation.get("messages")))
    except (KeyError, TypeError, ValueError):
        return False


def reconcile_non_submission(attempt_id, *, conn):
    """Recover only stopped adapters with actual pre-submit or rejection evidence."""
    from .guard import _load_attempt, browser_activity_uncertain
    from applypilot.database import transition_state
    from applypilot.discovery.repository import transaction
    with transaction(conn):
        attempt = dict(_load_attempt(conn, attempt_id, live=True))
        if (attempt["status"] not in {"verification_required", "verification_quarantined", "side_effect_intent"}
                or attempt.get("adapter_dispatch_protocol") != PROTOCOL
                or (attempt.get("adapter_invocation_id") and not attempt.get("adapter_ended_at"))):
            return attempt
        proof = None
        if not browser_activity_uncertain(attempt):
            proof = cancellation_evidence(attempt, attempt.get("verification_reason") or "Adapter ended before submission")
            if not proof:
                return attempt
            conn.execute("UPDATE application_attempts SET status='cancelled_before_submit', "
                         "cancellation_evidence_json=?,updated_at=? WHERE attempt_id=?",
                         (json.dumps(proof, sort_keys=True), datetime.now(UTC).isoformat(), attempt_id))
        elif attempt.get("browser_runtime") == "browseros" and attempt.get("adapter_ended_at"):
            from applypilot import config
            from .browseros_mcp import read_state
            bridge = read_state(config.APP_DIR / "browseros-sessions" / (attempt_id + ".json"))
            receipt = bridge.get("submission_rejected") or {}
            if (bridge.get("invocation") != attempt.get("browser_capture_invocation")
                    or (bridge.get("bound_pages") or {}).get(str(receipt.get("page_id"))) != attempt["target"]
                    or not _provider_rejection_matches(attempt, receipt)):
                return attempt
            proof = {"kind": PROVIDER_NEGATIVE, "attempt_id": attempt_id, "operation_id": attempt["operation_id"],
                     "snapshot_sha256": hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest(),
                     "adapter_ended_at": attempt["adapter_ended_at"], "receipt": receipt}
            conn.execute("UPDATE application_attempts SET status='verified_absent',proof_kind=?,proof=?,updated_at=? "
                         "WHERE attempt_id=?", (PROVIDER_NEGATIVE, json.dumps(proof, sort_keys=True),
                                                datetime.now(UTC).isoformat(), attempt_id))
        if proof is None:
            return attempt
        updated = conn.execute("UPDATE jobs SET apply_status=NULL,apply_error=NULL,apply_category='pending' "
                               "WHERE url=? AND application_attempt_id=? AND state NOT IN ('archived','manual_only') "
                               "AND COALESCE(apply_status,'')!='manual'", (attempt["job_url"], attempt_id))
        if updated.rowcount == 1:
            transition_state(conn, attempt["job_url"], "ready_to_apply", reason="Adapter confirmed non-submission",
                             metadata={"attempt_id": attempt_id}, force=True)
        return dict(_load_attempt(conn, attempt_id))


def begin_dispatch(attempt_id, application_id, operation_id, invocation_id, *, conn):
    from .guard import _load_attempt, validate_attempt, GuardRejected, _current_job, attempt_snapshot
    from applypilot.discovery.repository import transaction
    with transaction(conn):
        attempt = dict(_load_attempt(conn, attempt_id))
        if attempt.get("application_id") != application_id or attempt.get("operation_id") != operation_id:
            raise GuardRejected("Dispatch identity does not match the prepared application")
        if attempt.get("adapter_dispatch_protocol") != PROTOCOL:
            raise GuardRejected("Legacy attempt cannot be adopted without reconciliation")
        if attempt.get("adapter_invocation_id") or attempt["status"] != "prepared":
            return False
        validate_attempt(attempt_id, conn=conn)
        current = _current_job(conn, attempt["job_url"], attempt["doc_format"])
        if attempt_snapshot(current) != attempt["snapshot_json"]:
            raise GuardRejected("Application artifacts, route or business input changed before dispatch")
        conn.execute("UPDATE application_attempts SET adapter_invocation_id=?,adapter_started_at=?,updated_at=? "
                     "WHERE attempt_id=?", (invocation_id, datetime.now(UTC).isoformat(),
                                             datetime.now(UTC).isoformat(), attempt_id))
    return True


def finish_dispatch(attempt_id, invocation_id, *, conn):
    # This records that the finite adapter call has returned, never that an
    # external action did not happen. Side-effect intent remains independently protected.
    from applypilot.discovery.repository import transaction
    with transaction(conn):
        conn.execute("UPDATE application_attempts SET adapter_ended_at=? WHERE attempt_id=? "
                     "AND adapter_invocation_id=? AND adapter_ended_at IS NULL",
                     (datetime.now(UTC).isoformat(), attempt_id, invocation_id))


def apply_review(attempt_id, application_id, review, *, conn):
    from .guard import _load_attempt, GuardRejected
    from applypilot.database import transition_state
    from applypilot.discovery.repository import transaction
    if (review.get("kind") != "application_review" or review.get("decision") != "confirmed_not_submitted"
            or review.get("attempt_id") != attempt_id or review.get("application_id") != application_id
            or review.get("source") != "operator_verified" or not str(review.get("reviewed_by", "")).strip()
            or not str(review.get("provider_evidence", "")).strip() or review.get("adapter_stopped") is not True
            or not str(review.get("adapter_stop_evidence", "")).strip()):
        raise ValueError("Review requires attributable provider-negative evidence and proof the old adapter was stopped")
    with transaction(conn):
        attempt = dict(_load_attempt(conn, attempt_id, live=True))
        if attempt.get("application_id") not in (None, application_id):
            raise GuardRejected("Review belongs to a different application")
        if attempt["status"] == "submitted":
            raise GuardRejected("A confirmed submission cannot be reopened")
        if attempt["status"] == "verified_absent":
            return attempt
        proof = {**review, "observed_at": datetime.now(UTC).isoformat()}
        conn.execute("UPDATE application_attempts SET status='verified_absent',proof_kind=?,proof=?,updated_at=? "
                     "WHERE attempt_id=?", (REVIEW, json.dumps(proof, sort_keys=True), proof["observed_at"], attempt_id))
        job = conn.execute("SELECT state,apply_status FROM jobs WHERE url=? FOR UPDATE", (attempt["job_url"],)).fetchone()
        if job['state'] not in {'manual_only', 'archived'} and job['apply_status'] != 'manual':
            conn.execute("UPDATE jobs SET apply_status=NULL,apply_error=NULL WHERE url=? AND application_attempt_id=?",
                         (attempt["job_url"], attempt_id))
            transition_state(conn, attempt["job_url"], "ready_to_apply", reason="Operator verified non-submission",
                             metadata={"attempt_id": attempt_id, "reviewed_by": review["reviewed_by"]}, force=True)
        return dict(_load_attempt(conn, attempt_id))

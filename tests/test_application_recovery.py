"""Submission recovery uses observed effects, never guessed error categories."""
import asyncio
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from applypilot.apply import attempt_lifecycle as lifecycle
from applypilot.apply import email_application, guard, service


def attempt():
    now = datetime.now(UTC)
    return {
        "attempt_id": "attempt-123456789", "operation_id": "operation-123456789",
        "application_id": "application-1", "job_url": "https://example.test/job",
        "target": "https://example.test/application", "status": "verification_required",
        "adapter_dispatch_protocol": lifecycle.PROTOCOL, "snapshot_json": '{"job":1}',
        "adapter_invocation_id": "activity-1", "adapter_ended_at": now.isoformat(),
        "browser_runtime": "browseros", "browser_capture_invocation": "browser-1",
        "browser_ambiguous_action_at": None, "side_effect_intent_at": None,
    }


def rejection_attempt():
    row = attempt()
    ended = datetime.fromisoformat(row["adapter_ended_at"])
    row["side_effect_intent_at"] = (ended - timedelta(seconds=2)).isoformat()
    receipt = {
        "kind": "browser-validation-rejected-v1", "clicked": True,
        "intent_at": row["side_effect_intent_at"], "target": row["target"],
        "invocation": row["browser_capture_invocation"], "page_id": 1,
        "recorded_at": (ended - timedelta(seconds=1)).isoformat(),
        "validation": {"rejected": True, "completion_visible": False,
                       "pending": False, "fields": ["required answer"]},
    }
    return row, receipt


def test_unknown_pre_submit_failure_never_requires_operator(monkeypatch):
    reason = "Unexpected provider response"
    row = {**attempt(), "status": "prepared"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: False)
    cancelled = {**row, "status": "cancelled_before_submit"}
    cancel = MagicMock(return_value=cancelled)
    monkeypatch.setattr(guard, "cancel_before_submit", cancel)
    assert guard.record_outcome_unknown(row["attempt_id"], reason=reason,
                                        quarantine=True, conn=conn) == cancelled
    cancel.assert_called_once_with(row["attempt_id"], reason=reason, detail="", conn=conn)
    assert not any("UPDATE application_attempts" in str(call) for call in conn.execute.call_args_list)


def test_possible_submission_is_retained_even_for_retryable_sounding_error(monkeypatch):
    row = {**attempt(), "status": "prepared", "browser_ambiguous_action_at": "observed"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: True)
    monkeypatch.setattr(guard, "transition_state", MagicMock())
    monkeypatch.setattr(guard, "commit_with_retry", MagicMock())
    cancel = MagicMock()
    monkeypatch.setattr(guard, "cancel_before_submit", cancel)
    guard.record_outcome_unknown(row["attempt_id"], reason="rate_limit", conn=conn)
    cancel.assert_not_called()
    writes = [call.args for call in conn.execute.call_args_list if "UPDATE application_attempts" in call.args[0]]
    assert writes[0][1][0] == "verification_required"


def test_provider_rejection_allows_retry_without_operator_identity():
    row, receipt = rejection_attempt()
    row.update(status="verified_absent", proof_kind=lifecycle.PROVIDER_NEGATIVE)
    row["proof"] = json.dumps({
        "kind": lifecycle.PROVIDER_NEGATIVE, "attempt_id": row["attempt_id"],
        "operation_id": row["operation_id"],
        "snapshot_sha256": hashlib.sha256(row["snapshot_json"].encode()).hexdigest(),
        "adapter_ended_at": row["adapter_ended_at"], "receipt": receipt,
    })
    assert lifecycle.safe_to_retry(row)
    row["adapter_ended_at"] = None
    assert not lifecycle.safe_to_retry(row)


@pytest.mark.parametrize("change", [
    {"invocation": "other-session"}, {"target": "https://example.test/other"},
    {"intent_at": "other-intent"}, {"clicked": False},
    {"validation": {"rejected": False, "completion_visible": False, "pending": False}},
    {"validation": {"rejected": True, "completion_visible": True, "pending": False, "messages": ["bad"]}},
    {"validation": {"rejected": True, "completion_visible": False, "pending": True, "messages": ["bad"]}},
])
def test_foreign_or_ambiguous_rejection_never_authorizes_resend(change):
    row, receipt = rejection_attempt()
    receipt.update(change)
    assert not lifecycle._provider_rejection_matches(row, receipt)


def test_missing_receipt_never_proves_non_submission(monkeypatch):
    row = attempt()
    row["side_effect_intent_at"] = row["adapter_ended_at"]
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: True)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("applypilot.apply.browseros_mcp.read_state", lambda path: {})
    assert lifecycle.reconcile_non_submission(row["attempt_id"], conn=conn) == row
    conn.execute.assert_not_called()


def test_legacy_pre_submit_quarantine_recovers_automatically(monkeypatch):
    row = attempt()
    row["status"] = "verification_quarantined"
    updated = {**row, "status": "cancelled_before_submit"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", MagicMock(side_effect=[row, updated]))
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: False)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("applypilot.database.transition_state", MagicMock())
    assert lifecycle.reconcile_non_submission(row["attempt_id"], conn=conn) == updated
    writes = [call.args for call in conn.execute.call_args_list]
    assert "cancelled_before_submit" in writes[0][0]
    proof = json.loads(writes[0][1][0])
    assert proof["kind"] == lifecycle.NEGATIVE


def test_in_flight_adapter_cannot_be_reopened(monkeypatch):
    row = {**attempt(), "adapter_ended_at": None}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    assert lifecycle.reconcile_non_submission(row["attempt_id"], conn=conn) == row
    conn.execute.assert_not_called()


def test_captured_provider_rejection_recovers_without_operator(monkeypatch):
    row, receipt = rejection_attempt()
    updated = {**row, "status": "verified_absent"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", MagicMock(side_effect=[row, updated]))
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: True)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("applypilot.database.transition_state", MagicMock())
    monkeypatch.setattr("applypilot.apply.browseros_mcp.read_state", lambda path: {
        "invocation": row["browser_capture_invocation"], "bound_pages": {"1": row["target"]},
        "submission_rejected": receipt,
    })
    assert lifecycle.reconcile_non_submission(row["attempt_id"], conn=conn) == updated
    proof_write = conn.execute.call_args_list[0].args
    assert proof_write[1][0] == lifecycle.PROVIDER_NEGATIVE
    proof = json.loads(proof_write[1][1])
    assert proof["receipt"] == receipt


def test_prior_submission_report_is_not_mistaken_for_pre_submit_failure(monkeypatch):
    row = {**attempt(), "status": "prepared"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("applypilot.apply.submission_evidence.reconcile_captured_submission",
                        lambda *args, **kwargs: {"disposition": "unknown"})
    unknown = MagicMock(return_value={"status": "verification_required"})
    monkeypatch.setattr(guard, "require_verification", unknown)
    guard.record_submission_report(row["attempt_id"],
        {"status": "already-submitted", "reason": "Provider page says already applied"}, conn=conn)
    assert "browser_ambiguous_action_at" in conn.execute.call_args.args[0]
    unknown.assert_called_once()


def test_submitted_claim_without_proof_cannot_reenable_a_submit(monkeypatch):
    row = {**attempt(), "status": "prepared"}
    conn = MagicMock()
    monkeypatch.setattr(guard, "_load_attempt", lambda *args, **kwargs: row)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr("applypilot.apply.submission_evidence.reconcile_captured_submission",
                        lambda *args, **kwargs: {"disposition": "unknown"})
    unknown = MagicMock(return_value={"status": "verification_required"})
    monkeypatch.setattr(guard, "require_verification", unknown)
    guard.record_submission_report(row["attempt_id"], {"status": "submitted", "reason": ""}, conn=conn)
    assert "browser_ambiguous_action_at" in conn.execute.call_args.args[0]
    unknown.assert_called_once()


def test_reconciliation_does_not_override_archived_job(monkeypatch):
    row = attempt()
    updated = {**row, "status": "cancelled_before_submit"}
    conn = MagicMock()
    conn.execute.return_value.rowcount = 0
    monkeypatch.setattr(guard, "_load_attempt", MagicMock(side_effect=[row, updated]))
    monkeypatch.setattr(guard, "browser_activity_uncertain", lambda value: False)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    transition = MagicMock()
    monkeypatch.setattr("applypilot.database.transition_state", transition)
    lifecycle.reconcile_non_submission(row["attempt_id"], conn=conn)
    job_write = next(call.args[0] for call in conn.execute.call_args_list if "UPDATE jobs" in call.args[0])
    assert "state NOT IN ('archived','manual_only')" in job_write
    transition.assert_not_called()


def test_email_adapter_preserves_unexpected_failure(monkeypatch):
    row = attempt()
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"route_id": "route-1"}
    monkeypatch.setattr(email_application, "get_connection", lambda: conn)
    closed = MagicMock()
    monkeypatch.setattr(email_application, "close_connection", closed)
    monkeypatch.setattr(guard, "attempt_status", lambda *args, **kwargs: row)
    error = ValueError("new provider response format")

    async def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(email_application, "submit_email_application", fail)
    with pytest.raises(ValueError) as caught:
        email_application.run_email_job({"url": row["job_url"]}, 0, row["attempt_id"])
    assert caught.value is error
    closed.assert_called_once()


def test_email_reconciliation_failure_keeps_intent_and_propagates(monkeypatch):
    row = {"attempt_id": "attempt-1", "state": "uncertain"}
    monkeypatch.setattr(email_application, "_delivery", lambda *args: row)
    monkeypatch.setattr(email_application, "_validated_payload", MagicMock(side_effect=TimeoutError("provider timeout")))
    monkeypatch.setattr(guard, "attempt_status", MagicMock(return_value={}))
    uncertain = MagicMock()
    monkeypatch.setattr(email_application, "_uncertain", uncertain)
    with pytest.raises(TimeoutError):
        asyncio.run(email_application.reconcile_email_application("attempt-1", conn=MagicMock()))
    uncertain.assert_called_once()


@pytest.mark.parametrize("status,outcome", [
    ("cancelled_before_submit", "confirmed_not_submitted"),
    ("verification_required", "outcome_unknown"),
    ("submitted", "submitted"),
    ("prepared", "outcome_unknown"),
])
def test_native_activity_retry_observes_existing_attempt_without_reinvoking_adapter(monkeypatch, status, outcome):
    from applypilot.apply import launcher
    from applypilot.orchestration.contracts import job_identity
    row = {**attempt(), "status": status}
    application_id = job_identity("testing", row["job_url"])
    row["application_id"] = application_id
    command = {"workspace": "testing", "application_id": application_id,
               "attempt_id": row["attempt_id"], "operation_id": row["operation_id"]}
    value = {"job_url": row["job_url"], "job_id": application_id}
    conn = MagicMock()
    load_attempt = MagicMock(return_value=row)
    monkeypatch.setattr(guard, "_load_attempt", load_attempt)
    monkeypatch.setattr("applypilot.discovery.repository.transaction", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(service, "get_connection", lambda: conn)
    monkeypatch.setattr(service, "close_connection", MagicMock())
    monkeypatch.setattr(service, "check_cancelled", MagicMock())
    monkeypatch.setattr(service, "current_task", lambda: {"key": "workflow/run/activity"})
    observed = {"state": outcome, "attempt_id": row["attempt_id"]}
    inspect = MagicMock(return_value=observed)
    monkeypatch.setattr(service, "inspect", inspect)
    browser = MagicMock(side_effect=AssertionError("A redelivered Activity must not invoke browser submission"))
    email = MagicMock(side_effect=AssertionError("A redelivered Activity must not resend email"))
    monkeypatch.setattr(launcher, "run_job", browser)
    monkeypatch.setattr(email_application, "run_email_job", email)
    assert service.submit(value, {"options": {}}, command) == observed
    inspect.assert_called_once_with(value, {"options": {}}, {**command, "after_dispatch": True})
    browser.assert_not_called()
    email.assert_not_called()
    # Completed submissions must reach observation before checking active-job ownership.
    load_attempt.assert_called_once_with(conn, row["attempt_id"])

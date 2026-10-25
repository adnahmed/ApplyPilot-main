"""Migration recovery requires recorded effects and adapter-authored transitions."""
from datetime import UTC, datetime
import json
from unittest.mock import MagicMock

import pytest

from applypilot.apply.attempt_lifecycle import PROTOCOL, cancellation_evidence
from applypilot.migrations import application_recovery as migration


def records():
    attempt = {
        "attempt_id": "attempt-123456789", "operation_id": "operation-123456789",
        "job_url": "https://example.test/job", "status": "cancelled_before_submit",
        "adapter_dispatch_protocol": PROTOCOL, "snapshot_json": '{"job":1}',
        "adapter_invocation_id": "activity-1", "adapter_ended_at": datetime.now(UTC).isoformat(),
        "browser_ambiguous_action_at": None, "side_effect_intent_at": None,
    }
    attempt["cancellation_evidence_json"] = json.dumps(cancellation_evidence(attempt, "arbitrary failure"))
    job = {"url": attempt["job_url"], "application_attempt_id": attempt["attempt_id"],
           "state": "manual_only", "apply_status": "manual", "eligibility": "eligible",
           "apply_category": "archived_platform", "apply_error": "arbitrary failure"}
    transition = {"to_state": "manual_only", "metadata": json.dumps({
        "attempt_id": attempt["attempt_id"], "reason_code": "arbitrary failure", "category": "archived_platform"})}
    return job, attempt, transition


def test_current_adapter_failure_with_negative_evidence_reopens():
    assert migration.can_reopen(*records(), browser_outcome_unknown=False)


@pytest.mark.parametrize("changes", [
    {"status": "submitted"}, {"side_effect_intent_at": "recorded"},
    {"browser_ambiguous_action_at": "recorded"}, {"adapter_ended_at": None},
    {"adapter_dispatch_protocol": "legacy"}, {"adapter_invocation_id": None},
    {"cancellation_evidence_json": "{}"}, {"snapshot_json": '{"job":2}'},
])
def test_attempt_with_missing_or_uncertain_evidence_remains_protected(changes):
    job, attempt, transition = records()
    attempt.update(changes)
    assert not migration.can_reopen(job, attempt, transition, browser_outcome_unknown=False)


@pytest.mark.parametrize("changes", [
    {"applied_at": "recorded"}, {"state": "archived"}, {"apply_status": "applied"},
    {"eligibility": "ineligible"}, {"discovery_source_status": "closed"},
    {"apply_category": "archived_expired"}, {"apply_category": "archived_ineligible"},
    {"application_attempt_id": "other-attempt"},
])
def test_business_outcomes_and_exclusions_are_preserved(changes):
    job, attempt, transition = records()
    job.update(changes)
    assert not migration.can_reopen(job, attempt, transition, browser_outcome_unknown=False)


@pytest.mark.parametrize("metadata", [None, "bad-json", "[]", '{}',
    '{"attempt_id":"attempt-123456789","reason_code":"skip","source":"operator"}',
    '{"attempt_id":"other-attempt","reason_code":"failed"}',
])
def test_unattributed_or_owner_transition_is_not_reinterpreted(metadata):
    job, attempt, transition = records()
    transition["metadata"] = metadata
    assert not migration.can_reopen(job, attempt, transition, browser_outcome_unknown=False)


def test_browser_unknown_defaults_to_protected():
    assert not migration.can_reopen(*records())


def test_legacy_quarantine_requires_actual_no_effect():
    job, attempt, transition = records()
    attempt["status"] = "verification_quarantined"
    assert migration.can_reopen(job, attempt, transition, browser_outcome_unknown=False)
    assert not migration.can_reopen(job, attempt, transition, browser_outcome_unknown=True)


def test_migration_archives_before_changing_business_rows(monkeypatch):
    job, attempt, transition = records()
    conn = MagicMock()
    conn.in_transaction = True
    selected = MagicMock()
    selected.fetchall.return_value = [{"job_json": json.dumps(job), "attempt_json": json.dumps(attempt),
                                      "transition_json": json.dumps(transition)}]
    conn.execute.side_effect = lambda sql, *args: selected if sql.startswith("SELECT to_jsonb") else MagicMock()
    monkeypatch.setattr("applypilot.apply.guard.browser_activity_uncertain", lambda row: False)
    transition_state = MagicMock()
    monkeypatch.setattr("applypilot.database.transition_state", transition_state)
    assert migration.migrate(conn) == 1
    calls = [call.args for call in conn.execute.call_args_list]
    archive = next(i for i, args in enumerate(calls) if "INSERT INTO retired_execution_history" in args[0])
    update = next(i for i, args in enumerate(calls) if "UPDATE application_attempts" in args[0])
    assert archive < update
    saved = json.loads(calls[archive][1][3])
    assert saved["job"]["state"] == "manual_only"
    assert saved["job"]["apply_error"] == "arbitrary failure"
    assert saved["attempt"]["cancellation_evidence_json"] == attempt["cancellation_evidence_json"]
    conn.commit.assert_not_called()
    transition_state.assert_called_once()


def test_migration_requires_the_callers_schema_transaction():
    conn = MagicMock()
    conn.in_transaction = False
    with pytest.raises(RuntimeError, match="schema transaction"):
        migration.migrate(conn)
    conn.execute.assert_not_called()

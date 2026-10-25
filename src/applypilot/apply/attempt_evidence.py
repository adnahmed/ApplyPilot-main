"""Proof of a pre-submit cancellation; no clock grants permission to send."""
from __future__ import annotations


def safe_pre_submit(attempt: dict) -> bool:
    return bool(attempt.get("status") == "cancelled_before_submit"
                and attempt.get("application_id") and attempt.get("attempt_id")
                and attempt.get("snapshot_json") and attempt.get("artifact_hashes_json")
                and not attempt.get("side_effect_intent_at") and not attempt.get("submitted_at")
                and not attempt.get("proof") and not attempt.get("proof_kind")
                and not attempt.get("browser_ambiguous_action_at"))

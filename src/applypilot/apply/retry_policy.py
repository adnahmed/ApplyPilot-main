"""Bounded pacing for positively identified pre-submit infrastructure failures.

No history is erased. A cancellation alone is NOT permission to retry: legacy
attempts without a complete ownership binding are deliberately excluded.
"""
from __future__ import annotations

NO_STRUCTURED_RESULT = "application agent returned no structured result before submit"
PRE_SUBMIT_INFRASTRUCTURE = frozenset({
    "no_structured_result", "browser_bridge_unavailable", "browser_session_not_connected", "mcp_session_expired",
    "browser_transport_lost", "browser_heartbeat_timeout", "browser_session_ownership_lost",
    "browser_disconnected", "browser_session_error",
    "form_interaction_error", "page_error", "browser_unavailable", "file_upload_blocked",
    "resume_upload_blocked", "account_creation_broken", "screening_questions", "resource_database_busy",
    "agent_cli_terminal_error",
})


def transient_pre_submit(attempt: dict, error: str | None, *, permanent: bool = False) -> bool:
    from applypilot.scoring.provider_backoff import admission_details
    return bool(
        not permanent and (error in PRE_SUBMIT_INFRASTRUCTURE or admission_details(error) is not None)
        and attempt.get("status") in {"prepared", "cancelled_before_submit"}
        and attempt.get("ownership_token") == attempt.get("attempt_id")
        and attempt.get("ownership_token")
        and attempt.get("snapshot_json") and attempt.get("artifact_hashes_json")
        and not attempt.get("side_effect_intent_at") and not attempt.get("submitted_at")
        and not attempt.get("proof") and not attempt.get("proof_kind")
        and not attempt.get("browser_ambiguous_action_at")
    )


def backoff_seconds(failures: int) -> int:
    """One minute, then exponential backoff capped at one hour."""
    return min(3600, 60 * 2 ** min(6, max(0, int(failures) - 1)))

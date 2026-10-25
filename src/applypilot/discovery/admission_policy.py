"""Revisioned operator assumptions, kept separate from employer evidence."""
from __future__ import annotations

import hashlib
import json

POLICY_VERSION = "admission-policy-1"
POLICY_KEYS = frozenset({"optimistic_missing_requirements", "fill_idle_capacity",
    "allow_off_portfolio_prepare", "allow_off_portfolio_apply", "reallocate_unused_to_preferred"})


def policy_revision(policy=None) -> str:
    policy = policy or {}
    values = {key: policy.get(key, False) for key in sorted(POLICY_KEYS)}
    if any(type(value) is not bool for value in values.values()):
        raise ValueError("admission policy values must be booleans")
    return hashlib.sha256(json.dumps([POLICY_VERSION, values], sort_keys=True).encode()).hexdigest()[:20]


def omitted_requirement(option, facts, name: str) -> bool:
    """Only genuine omission qualifies; observed unknowns and failures do not."""
    from .models import TriState, WorkArrangement, WorkMode

    keys = {
        "location": ("countries", "eligible_scope", "regions", "cities", "office_countries", "office_locations"),
        "authorization": ("authorization_required", "authorization_countries", "authorization_requirements"),
        "sponsorship": ("sponsorship_supported",),
        "engagement": ("engagement",),
    }.get(name)
    if keys is None:
        return False
    if name == "location" and option.work_mode == WorkMode.REMOTE:
        keys = tuple(key for key in keys if not key.startswith("office_"))
    if name == "authorization" and (
            option.sponsorship_supported == TriState.NO or any(
                f.kind == "sponsorship_supported" and f.value not in (True, "yes", TriState.YES)
                for f in facts)):
        return False
    if any(f.kind in {"normalization_issue", "conflicting_statement"} for f in facts):
        return False
    if any(f.kind in keys for f in facts):
        return False
    defaults = WorkArrangement(id=option.id)
    if any(getattr(option, key) != getattr(defaults, key) for key in keys):
        return False
    # A remote label does not disclose applicant scope. An explicit unknown
    # mode, however, is failed evidence rather than an omitted requirement.
    if name == "location" and any(f.kind == "work_mode" and f.value in (None, "unknown", WorkMode.UNKNOWN) for f in facts):
        return False
    return True


def effective_score_eligibility(result, discovery_eligibility: str, policy=None) -> str:
    """Model failures/explicit denials are never converted into permission."""
    value = result.get("eligibility", "unknown")
    if value not in {"eligible", "ineligible_location", "unknown"}:
        return "unknown"
    if (value == "unknown" and (policy or {}).get("optimistic_missing_requirements")
            and discovery_eligibility == "eligible" and result.get("score") is not None
            and not result.get("error") and not result.get("hard_blockers")
            and result.get("eligibility_basis") == "omitted_requirements"):
        return "eligible"
    return value

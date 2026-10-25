"""Preparation output buffers, independent of execution and outcome services."""
from __future__ import annotations

from collections.abc import Mapping

def default_buffers(root=None):
    from applypilot.runtime_policy import load_policy
    return {name: row.model_dump() for name, row in load_policy(root).buffers.items()}


MAX_SCORE_RETRIES = 5
# Max enrichment retries before giving up and marking a job as permanently failed.
MAX_ENRICH_RETRIES = 5
MAX_BRIEF_RETRIES = 5

_RETIRED_FLOW_KEYS = frozenset({
    "shared_pool", "workers", "refresh_seconds", "snapshot_timeout_seconds",
    "max_snapshot_age_seconds", "stalled_after_seconds", "probe_interval_seconds",
    "background_interval_seconds", "background_total_workers", "finish_reserve",
    "enabled", "referral_backlog_limit", "network_research_per_cycle", "referral_plans_per_pass",
})


def validate_backlog(value=None, *, defaults=None, default_mode=None):
    if defaults is None or default_mode is None:
        from applypilot.runtime_policy import load_policy
        policy = load_policy()
        if defaults is None:
            defaults = {name: row.model_dump() for name, row in policy.buffers.items()}
        if default_mode is None:
            default_mode = policy.buffer_mode
    value = {} if value is None else value
    if not isinstance(value, Mapping) or set(value) - {"mode", "buffers"} - _RETIRED_FLOW_KEYS:
        raise ValueError("runtime.flow_control contains unknown backlog settings")
    saved = value.get("buffers", {})
    if not isinstance(saved, Mapping) or set(saved) - set(defaults) - {"enrich", "referrals"}:
        raise ValueError("runtime.flow_control.buffers contains unknown preparation buffers")
    buffers = {}
    for name, base in defaults.items():
        limits = saved.get(name, {})
        if not isinstance(limits, Mapping) or set(limits) - {"low", "high"}:
            raise ValueError(f"Buffer {name} requires low and high watermarks")
        limits = base | dict(limits)
        if (any(type(n) is not int for n in limits.values())
                or not 0 <= limits["low"] < limits["high"] <= 100000):
            raise ValueError(f"Buffer {name} requires 0 <= low < high <= 100000")
        buffers[name] = limits
    mode = value.get("mode", default_mode if buffers == defaults else "watermarks")
    if mode not in ("automatic", "watermarks"):
        raise ValueError("runtime.flow_control.mode must be automatic or watermarks")
    return {"mode": mode, "buffers": buffers}

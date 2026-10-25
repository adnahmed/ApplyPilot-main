"""Owner-facing, secret-free integration setup and throughput controls."""
from __future__ import annotations

from copy import deepcopy
import yaml

from .storage import atomic, backup, digest, read_bytes, read_json, require_stopped, setup_lock

EDITABLE = {"request_budget", "http_budget", "browser_budget", "model_budget",
            "authorized_sources", "rollout_mode", "source_inputs", "research", "request_defaults",
            "evidence_budget", "max_pages_per_task", "probe_limit", "buffer", "flow_control"}
SIGNALS = ("web_search", "twitter", "linkedin_posts")


def state(root):
    from .environment import child_env
    from .github_connect import status as github_status
    from .sessions import status as session_status
    from applypilot.integrations import mcporter
    env = child_env(root)
    rows = read_json(root / "setup" / "mcporter.json", {})
    return {"github": github_status(root), "sessions": session_status(root, env),
            "mcp": {**rows, "installed": bool(mcporter.command()),
                    "browser_candidates": mcporter.browser_candidates(root),
                    "bindings": read_json(root / "setup" / "integration-bindings.json", {})},
            "research_overrides": {key: env[key] for key in
                ("APPLYPILOT_RESEARCH_ON_ENRICH", "APPLYPILOT_RESEARCH_COMPANIES") if key in env}}


def merge(saved, patch):
    """Merge nested editable settings without discarding undisplayed source inputs.

    Access grants are the complete form value so selecting 'Default' can remove
    an old grant. No generic deep-merge of credentials or other workspace files.
    """
    result = deepcopy(saved)
    for key, value in patch.items():
        if key == "source_inputs" and isinstance(value, dict):
            existing = result.setdefault(key, {})
            if not isinstance(existing, dict):
                raise ValueError("source_inputs must be a mapping")
            for name, fields in value.items():
                if not isinstance(fields, dict):
                    raise ValueError("Each source input must be a mapping")
                existing[name] = {**existing.get(name, {}), **fields}
        elif key in {"research", "request_defaults", "buffer", "flow_control"} and isinstance(value, dict):
            result[key] = {**result.get(key, {}), **value}
        else:
            result[key] = deepcopy(value)
    return result


def save_policy(root, data):
    from applypilot.config import validate_discovery_runtime
    require_stopped(root)
    patch = data.get("policy", {})
    if not isinstance(patch, dict) or set(patch) - EDITABLE:
        raise ValueError("Only the displayed source-policy fields are editable")
    with setup_lock(root):
        require_stopped(root)
        path = root / "discovery_runtime.yaml"
        raw = read_bytes(path)
        if data.get("revision") != digest(raw):
            raise ValueError("Source policy changed. Reload before saving")
        saved = yaml.safe_load(raw) if raw else {}
        if saved is None:
            saved = {}
        validate_discovery_runtime(saved)
        merged = merge(saved, patch)
        policy = validate_discovery_runtime(merged)
        backup(root, ["discovery_runtime.yaml"])
        encoded = yaml.safe_dump(merged, sort_keys=False).encode()
        atomic(path, encoded)
    return {"saved": True, "revision": digest(encoded), "restart_required": True, "policy": policy}


def effective_policy(root):
    from applypilot.config import validate_discovery_runtime
    raw=read_bytes(root / "discovery_runtime.yaml")
    search_raw=read_bytes(root / "searches.yaml")
    searches=yaml.safe_load(search_raw) if search_raw else {}
    if searches is not None and not isinstance(searches,dict):
        raise ValueError("Search configuration must be a mapping")
    saved=yaml.safe_load(raw) if raw else {}
    return validate_discovery_runtime({} if saved is None else saved,
        legacy_defaults=(searches or {}).get("defaults",{}))


def maximum_patch(current):
    inputs = deepcopy(current["source_inputs"])
    for name in SIGNALS:
        fields = inputs.setdefault(name, {})
        fields["limit"] = 0 if fields.get("limit") == 0 else -1
        # Keep populated exports and explicitly selected adapters. The old empty
        # export placeholders are not useful connections and can safely become auto.
        if name != "web_search" and fields.get("mode") == "export" and not fields.get("export_files"):
            fields["mode"] = "auto"
    grants = dict(current["authorized_sources"])
    for name in SIGNALS:
        # A persisted deny remains a deny, even when credentials are discovered.
        grants.setdefault(name, True)
    patch = {"source_inputs": inputs, "authorized_sources": grants,
             "research": {"enabled": current["research"]["enabled"],
                          "companies": 0 if current["research"]["companies"] == 0 else -1},
             "request_defaults": {"results_per_site": 0 if current["request_defaults"]["results_per_site"] == 0 else -1}}
    # Preserve deliberate zero/disabled settings. Positive volume caps become -1.
    for key in ("request_budget", "http_budget", "browser_budget", "model_budget",
                "evidence_budget", "max_pages_per_task", "probe_limit"):
        patch[key] = 0 if current[key] == 0 else -1
    return patch


def maximum_applied(root):
    current=effective_policy(root)
    return merge(current,maximum_patch(current)) == current


def maximum(root, data):
    """Explicit migration of existing low limits; never run during page loading."""
    if data.get("confirm") is not True:
        raise ValueError("Confirm the high-throughput, read-only local-source preset")
    patch=maximum_patch(effective_policy(root))
    result = save_policy(root, {"revision": data.get("revision"), "policy": patch})
    result["note"] = ("Uncapped local workload preset saved (-1): discovery, pages, probes and company count. "
                      "Existing zeros/pauses, denies, nonempty exports, paid budgets and outreach settings are unchanged. "
                      "Only sources enabled in Your search can be scheduled. Environment overrides still take precedence.")
    return result

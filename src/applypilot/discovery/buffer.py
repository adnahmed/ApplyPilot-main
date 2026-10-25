"""Read-only discovery inventory diagnostics.

Discovery inventory is durable and independent from preparation/application
watermarks.  This module may report downstream stock, but it never turns that
stock into permission to crawl sources.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from applypilot import config, postgres
from .admission import last_seen_sql


def snapshot(conn: postgres.Connection, root: Path, *, policy=None, min_score=None,
             document_count: int | None = None) -> dict:
    from applypilot.onboarding.integrations import effective_policy
    from applypilot.onboarding.storage import digest, read_bytes, read_json

    policy = policy or effective_policy(root)
    limits = policy["buffer"]
    if min_score is None:
        options = read_json(root / "autopilot-options.json", {})
        min_score = options.get("min_score", config.DEFAULTS["min_score"])
    at = datetime.now(timezone.utc).isoformat()
    value = {
        "available": True,
        "at": at,
        "limits": limits,
        "revision": digest(read_bytes(root / "discovery_runtime.yaml")),
        "min_score": min_score,
        "held": False,
        "backlog_held": False,
        "discovery_allowed": True,
        "preparation_tasks": 0,
        "reason": "Discovery inventory is independent of preparation/application stock.",
    }
    context = conn.execute(
        "SELECT context_key FROM discovery_active_context WHERE singleton_id=1"
    ).fetchone()
    value["context_key"] = context[0] if context else None
    value["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    counts = conn.execute(
        "SELECT SUM(CASE WHEN state='ready_to_apply' THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN discovery_source_status='closed' AND applied_at IS NULL THEN 1 ELSE 0 END) "
        "FROM jobs"
    ).fetchone()
    value["recorded_ready"], value["closed"] = [int(n or 0) for n in counts]
    value["actionable"] = value["recorded_ready"]
    value["actionable_capped"] = False
    value["stages"] = {}
    age = limits["revalidate_after_days"]
    value["stale"] = conn.execute(
        "SELECT COUNT(*) FROM jobs j WHERE j.applied_at IS NULL "
        "AND COALESCE(j.discovery_source_status,'unknown')!='closed' "
        "AND COALESCE(j.state,'') NOT IN ('applying','applied','archived',"
        "'verification_required','verification_quarantined') "
        "AND (ap_day(" + last_seen_sql() + ") IS NULL OR ap_day(" + last_seen_sql()
        + ") <= ap_day('now', ?))", (f"-{age} days",)
    ).fetchone()[0]
    value["last_observed_at"] = conn.execute(
        "SELECT MAX(last_seen_at) FROM discovery_postings"
    ).fetchone()[0]

    # Keep preparation telemetry visible without feeding it back into discovery.
    from applypilot.live_status import read_component
    from applypilot.engine_status import read_engine_heartbeat
    preparation = read_engine_heartbeat(root) or read_component(root, "preparation") or {}
    value["pipeline_buffers"] = (
        preparation.get("buffers", {})
        if preparation.get("buffer_context_key") == value["context_key"] else {}
    )
    value["pipeline_buffers_at"] = preparation.get("at") if value["pipeline_buffers"] else None
    value["pipeline_buffers_running"] = bool(preparation)
    return value


def read_snapshot(root: Path, *, policy=None, min_score=None, persist=False) -> dict:
    """Short read snapshot; never initialize/migrate the database on a GET."""
    from applypilot.web.data import Store
    try:
        store = Store(root / "main.pgstore")
        with store.read() as conn:
            if conn is None:
                from applypilot.onboarding.integrations import effective_policy
                return {
                    "available": False,
                    "needs_initialization": True,
                    "limits": (policy or effective_policy(root))["buffer"],
                    "preparation_tasks": 0,
                    "held": False,
                    "discovery_allowed": True,
                    "reason": "The first discovery pass will initialize durable inventory.",
                }
            tables = {row[0] for row in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname=current_schema()"
            )}
            if not {"discovery_active_context", "discovery_postings", "application_attempts"} <= tables:
                from applypilot.onboarding.integrations import effective_policy
                return {
                    "available": False,
                    "needs_initialization": True,
                    "limits": (policy or effective_policy(root))["buffer"],
                    "preparation_tasks": 0,
                    "held": False,
                    "discovery_allowed": True,
                    "reason": "Discovery will initialize durable source inventory.",
                }
            value = snapshot(conn, root, policy=policy, min_score=min_score)
    except (OSError, postgres.Error, ValueError, TypeError, KeyError, ImportError) as exc:
        return {
            "available": False,
            "held": False,
            "discovery_allowed": True,
            "reason": f"Discovery inventory diagnostics unavailable: {type(exc).__name__}",
        }
    if persist:
        try:
            from applypilot.live_status import publish
            publish("discovery", value, root=root)
        except (OSError, ValueError, TypeError):
            pass
    return value

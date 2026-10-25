"""Read aggregate metrics from all local workers, not the viewer process registry."""
from __future__ import annotations

import json
from applypilot import postgres
import time
from .store import BUCKETS, reader


def labels(values):
    return "{" + ",".join(key + "=" + json.dumps(str(value), ensure_ascii=True) for key, value in values.items()) + "}"


def render(root=None):
    lines = []
    def declare(name, kind, help_text):
        lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
    def emit(name, value, **tags):
        lines.append(name + (labels(tags) if tags else "") + " " + str(value))

    # refresh_all.py writes only this non-secret summary into the shared operations
    # directory. Export it independently of the PostgreSQL journal so catalog-refresh
    # visibility still works when the application pipeline itself is not running.
    declare("applypilot_provider_refresh_status_up", "gauge", "The latest provider refresh status JSON is readable and valid.")
    status_path = (root / "operations" / "provider_refresh_status.json") if root is not None else None
    status = None
    if status_path is not None:
        try:
            candidate = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("schema_version") == 1:
                status = candidate
        except (OSError, ValueError, TypeError):
            pass
    emit("applypilot_provider_refresh_status_up", 1 if status is not None else 0)
    if status is not None:
        finished = float(status.get("finished_at_epoch") or 0.0)
        declare("applypilot_provider_refresh_last_timestamp_seconds", "gauge", "Completion time of the latest provider inventory refresh.")
        declare("applypilot_provider_refresh_age_seconds", "gauge", "Seconds since the latest provider inventory refresh completed.")
        declare("applypilot_provider_refresh_run_success", "gauge", "1 when the latest provider refresh/check completed without updater or blocking audit failures.")
        declare("applypilot_provider_refresh_run_blocked", "gauge", "1 when the latest provider refresh/check hit a blocking safety audit.")
        declare("applypilot_provider_refresh_check_only", "gauge", "1 when the latest provider catalog result came from --check-only and did not modify source inventories.")
        declare("applypilot_provider_refresh_audit_errors", "gauge", "Blocking audit errors in the latest provider refresh.")
        declare("applypilot_provider_refresh_audit_warnings", "gauge", "Non-blocking audit warnings in the latest provider refresh.")
        emit("applypilot_provider_refresh_last_timestamp_seconds", finished)
        emit("applypilot_provider_refresh_age_seconds", max(0.0, time.time() - finished))
        emit("applypilot_provider_refresh_run_success", 1 if status.get("overall_status") == "ok" else 0)
        declare("applypilot_provider_refresh_run_completed", "gauge", "1 when refresh finished and generation can continue, including partial provider failures.")
        declare("applypilot_provider_refresh_errors", "gauge", "Providers with errors in the latest completed refresh.")
        emit("applypilot_provider_refresh_run_completed", 1 if status.get("overall_status") in {"ok", "partial"} else 0)
        emit("applypilot_provider_refresh_errors", int(status.get("provider_error_count") or 0))
        emit("applypilot_provider_refresh_run_blocked", 1 if status.get("overall_status") == "blocked" else 0)
        emit("applypilot_provider_refresh_check_only", 1 if status.get("check_only") else 0)
        emit("applypilot_provider_refresh_audit_errors", int(status.get("audit_error_count") or 0))
        emit("applypilot_provider_refresh_audit_warnings", int(status.get("audit_warning_count") or 0))
        declare("applypilot_provider_refresh_provider_ok", "gauge", "1 when this provider updater succeeded in the latest refresh run.")
        declare("applypilot_provider_refresh_inventory_retained", "gauge", "1 when a provider error retained the previous inventory.")
        declare("applypilot_provider_refresh_models_added", "gauge", "Models added by this provider's latest refresh comparison.")
        declare("applypilot_provider_refresh_models_removed", "gauge", "Models removed by this provider's latest refresh comparison.")
        declare("applypilot_provider_refresh_free_to_paid", "gauge", "Models that changed from free to paid in this provider's latest refresh comparison.")
        declare("applypilot_provider_refresh_paid_to_free", "gauge", "Models that changed from paid to free in this provider's latest refresh comparison.")
        declare("applypilot_provider_refresh_changed", "gauge", "1 when the latest provider refresh reported any inventory/model change for this provider.")
        for provider, item in sorted((status.get("providers") or {}).items()):
            if not isinstance(item, dict):
                continue
            emit("applypilot_provider_refresh_provider_ok", 1 if item.get("status") == "OK" else 0, provider=provider)
            emit("applypilot_provider_refresh_inventory_retained", 1 if item.get("inventory_retained") else 0, provider=provider)
            added = item.get("models_added") or []
            removed = item.get("models_removed") or []
            free_to_paid = item.get("free_to_paid") or []
            paid_to_free = item.get("paid_to_free") or []
            emit("applypilot_provider_refresh_models_added", len(added), provider=provider)
            emit("applypilot_provider_refresh_models_removed", len(removed), provider=provider)
            emit("applypilot_provider_refresh_free_to_paid", len(free_to_paid), provider=provider)
            emit("applypilot_provider_refresh_paid_to_free", len(paid_to_free), provider=provider)
            meaningful = bool(added or removed or free_to_paid or paid_to_free)
            if not meaningful:
                changes = item.get("changes") or []
                meaningful = any(change != "no JSON changes" for change in changes)
            emit("applypilot_provider_refresh_changed", 1 if meaningful else 0, provider=provider)

    declare("applypilot_operations_store_up", "gauge", "The local operational journal is readable; not pipeline health.")
    try:
        with reader(root) as conn:
            gauges = [dict(r) for r in conn.execute("SELECT * FROM gauges")]
            items = [dict(r) for r in conn.execute("SELECT * FROM items")]
            counts = [dict(r) for r in conn.execute("SELECT * FROM counters")]
            processes = [dict(r) for r in conn.execute("SELECT * FROM processes")]
            incidents = [dict(r) for r in conn.execute("SELECT component,stage,COALESCE(display_severity,severity) severity,"
                "COUNT(*) count FROM incidents WHERE acknowledged_at IS NULL OR last_at>acknowledged_at GROUP BY 1,2,3")]
            active = [dict(r) for r in conn.execute("SELECT a.stage,a.component,a.operation,COUNT(*) count,MIN(a.started_at) oldest "
                "FROM active a JOIN processes p ON p.id=a.process_id WHERE p.running=1 AND p.heartbeat>? GROUP BY 1,2,3", (time.time() - 120,))]
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        emit("applypilot_operations_store_up", 1)
    except (postgres.Error, OSError):
        emit("applypilot_operations_store_up", 0)
        return "\n".join(lines) + "\n"
    declare("applypilot_pending_jobs", "gauge", "Last observed eligible stage queue; inspect its timestamp for freshness.")
    declare("applypilot_queue_updated_timestamp_seconds", "gauge", "Time of the last stage queue sample.")
    for row in gauges:
        emit("applypilot_pending_jobs", row["value"], stage=row["stage"])
        emit("applypilot_queue_updated_timestamp_seconds", row["updated_at"], stage=row["stage"])
    declare("applypilot_items_total", "counter", "Items counted by an instrumented boundary; do not sum overlapping operations.")
    for row in items:
        emit("applypilot_items_total", row["count"], **{k: row[k] for k in ("stage", "component", "operation", "measure")})
    declare("applypilot_operations_total", "counter", "Completed instrumented operations, not distinct jobs or submissions.")
    declare("applypilot_operation_duration_seconds", "histogram", "Duration of completed operations including failed operations.")
    declare("applypilot_last_operation_timestamp_seconds", "gauge", "Most recent completion by boundary and outcome.")
    for row in counts:
        tags = {key: row[key] for key in ("stage", "component", "operation", "outcome")}
        emit("applypilot_operations_total", row["count"], **tags)
        emit("applypilot_last_operation_timestamp_seconds", row["last_at"], **tags)
        for bound, count in zip(BUCKETS, json.loads(row["buckets"])):
            emit("applypilot_operation_duration_seconds_bucket", count, **tags, le=str(bound))
        emit("applypilot_operation_duration_seconds_bucket", row["count"], **tags, le="+Inf")
        emit("applypilot_operation_duration_seconds_count", row["count"], **tags)
        emit("applypilot_operation_duration_seconds_sum", row["duration"], **tags)
    declare("applypilot_active_operations", "gauge", "Active operations with a live recorder heartbeat (not proof of progress).")
    declare("applypilot_oldest_active_started_timestamp_seconds", "gauge", "Start time of the oldest live operation for diagnosing stalls.")
    for row in active:
        tags = {key: row[key] for key in ("stage", "component", "operation")}
        emit("applypilot_active_operations", row["count"], **tags)
        emit("applypilot_oldest_active_started_timestamp_seconds", row["oldest"], **tags)
    declare("applypilot_processes", "gauge", "Recorder processes by liveness; stale is not healthy and may mean a crash.")
    for state in ("alive", "stale", "stopped"):
        emit("applypilot_processes", sum(("stopped" if not p["running"] else "stale" if time.time() - p["heartbeat"] > 120 else "alive") == state for p in processes), state=state)
    declare("applypilot_recent_stale_processes", "gauge", "Unclosed recorders last seen 2 to 15 minutes ago; older stale history remains in Operations.")
    emit("applypilot_recent_stale_processes", sum(p["running"] and 120 < time.time() - p["heartbeat"] < 900 for p in processes))
    declare("applypilot_telemetry_dropped_events", "gauge", "Dropped events reported by retained processes; pruned with process retention.")
    declare("applypilot_telemetry_write_errors", "gauge", "Journal write errors reported by retained processes.")
    declare("applypilot_telemetry_queue_depth", "gauge", "Queued events across recently alive workers.")
    emit("applypilot_telemetry_dropped_events", sum(p["dropped"] for p in processes))
    emit("applypilot_telemetry_write_errors", sum(p["write_errors"] for p in processes))
    emit("applypilot_telemetry_queue_depth", sum(p["queue_depth"] for p in processes if p["running"] and time.time() - p["heartbeat"] < 120))
    declare("applypilot_unacknowledged_incidents", "gauge", "Retained grouped incidents; demotion changes display only, not failure counters.")
    for row in incidents:
        emit("applypilot_unacknowledged_incidents", row["count"], **{key: row[key] for key in ("stage", "component", "severity")})
    declare("applypilot_retained_events", "gauge", "Number of retained operational events.")
    emit("applypilot_retained_events", event_count)
    return "\n".join(lines) + "\n"

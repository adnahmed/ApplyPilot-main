"""Shared read-only evidence for saved run totals and scheduled recovery."""
from __future__ import annotations

import re
from applypilot import postgres

from applypilot.operations import store as journal
from applypilot.operations.runtime import sanitize
from .data import columns, now_iso, tables


def run_totals(root, store, runtime):
    run_id = runtime.get("operations_run_id")
    value = {"run_id": run_id, "available": False, "captured_at": now_iso(),
             "attempts": None, "submitted_attempts": None, "applications": None,
             "verification_holds": None, "cancellations": None, "unresolved_intents": None,
             "scope": "Saved attempts linked by exact operations run ID, with exact journal task IDs "
                      "only as a fallback for unattributed attempts. Unlinked history is excluded; timestamps never establish run membership."}
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        value["reason"] = "This runtime has no saved operations-run identity."
        return value
    try:
        with journal.reader(root) as conn:
            task_ids = [row[0] for row in conn.execute(
                "SELECT DISTINCT task_id FROM events WHERE run_id=? AND task_id!=''", (run_id,))]
        with store.read() as conn:
            if "application_attempts" not in tables(conn):
                value["reason"] = "Saved application attempts are unavailable."
                return value
            available = columns(conn, "application_attempts")
            links = ["NULLIF(operation_run_id,'')"] if "operation_run_id" in available else []
            if "queue_policy_json" in available:
                policy = "CASE WHEN ap_json_valid(queue_policy_json) THEN queue_policy_json ELSE '{}' END"
                links += [f"NULLIF(ap_json_text({policy},'$._operation_run_id'),'')",
                          f"NULLIF(ap_json_text({policy},'$.operation_run_id'),'')"]
            attribution = "COALESCE(" + ",".join([*links, "NULL"]) + ")" if links else "NULL"
            fields = "attempt_id,job_url,status,submitted_at"
            records = {row["attempt_id"]: dict(row) for row in conn.execute(
                f"SELECT {fields} FROM application_attempts WHERE {attribution}=?", (run_id,))}
            for offset in range(0, len(task_ids), 800):
                batch = task_ids[offset:offset + 800]
                records.update((row["attempt_id"], dict(row)) for row in conn.execute(
                    f"SELECT {fields} FROM application_attempts WHERE {attribution} IS NULL AND attempt_id IN (" +
                    ",".join("?" for _ in batch) + ")", batch))
        submitted = [row for row in records.values() if row["status"] == "submitted" and row["submitted_at"] is not None]
        value.update(available=True, attempts=len(records), submitted_attempts=len(submitted),
                     applications=len({row["job_url"] for row in submitted}),
                     verification_holds=sum(row["status"] in {"verification_required", "verification_quarantined"} for row in records.values()),
                     cancellations=sum(row["status"] in {"cancelled", "cancelled_before_submit"} for row in records.values()),
                     unresolved_intents=sum(row["status"] == "side_effect_intent" for row in records.values()))
    except (postgres.Error, OSError, ValueError) as exc:
        value["reason"] = "Saved run totals unavailable: " + sanitize(exc, 200)
    return value


def recovery_snapshot(store):
    from applypilot.recovery import snapshot
    try:
        with store.read() as conn:
            if conn is None:
                raise ValueError("Workspace database unavailable")
            value = snapshot(conn)
        if value.get("available"):
            return value
        reason = "Recovery records are not available yet."
    except (postgres.Error, OSError, ValueError):
        reason = "Recovery records could not be read; retry timing is unknown."
    return {"available": False, "at": now_iso(), "pending": None, "running": None,
            "completed": None, "items": [], "reason": reason}

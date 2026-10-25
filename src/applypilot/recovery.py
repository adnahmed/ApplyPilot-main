"""Business recovery history only. Temporal owns execution and retry timing.

Legacy tables, when present, are retained as historical evidence and copied into
an immutable ledger. No reader uses their deadlines, credits or owners to grant
execution. The remaining scheduler entry points fail closed for old callers;
their CLI/continuous callers are migrated in Part 2.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
import hashlib
import json
from uuid import uuid4


@contextmanager
def transaction(conn=None):
    from applypilot import config, postgres
    owned = conn is None
    if owned:
        conn = postgres.connect(str(config.DB_PATH), timeout=2, isolation_level=None)
        conn.row_factory = postgres.Row
    nested = conn.in_transaction
    savepoint = "recovery_history_" + uuid4().hex
    try:
        conn.execute("SAVEPOINT " + savepoint if nested else "BEGIN")
        yield conn
        conn.execute("RELEASE " + savepoint if nested else "COMMIT")
    except BaseException:
        if nested:
            conn.execute("ROLLBACK TO " + savepoint)
            conn.execute("RELEASE " + savepoint)
        else:
            conn.rollback()
        raise
    finally:
        if owned:
            conn.close()


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS recovery_history (
        id TEXT PRIMARY KEY, job_url TEXT NOT NULL, stage TEXT NOT NULL,
        reason_code TEXT NOT NULL, attempt_id TEXT, evidence_json TEXT NOT NULL,
        disposition TEXT NOT NULL, recorded_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS recovery_history_job ON recovery_history(job_url,recorded_at)")
    # Append each historical version once; never discard uncertain submission
    # evidence or translate an old lease expiry / budget refund into permission.
    for table, stage in (("recovery_work", "legacy_recovery"),
                         ("recovery_budget_credits", "legacy_budget_history")):
        exists = conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename=?",
                              (table,)).fetchone()
        if exists:
            conn.execute("""INSERT INTO recovery_history
                (id,job_url,stage,reason_code,attempt_id,evidence_json,disposition,recorded_at)
                SELECT ? || md5(to_jsonb(r)::text),r.job_url,?,'legacy_execution_retired',
                       r.attempt_id,to_jsonb(r)::text,'historical_only',?
                FROM """ + table + " r ON CONFLICT(id) DO NOTHING",
                (table + ":", stage, datetime.now(UTC).isoformat()))


def defer_work(job_url, stage, reason_code, scope="job", retry_at=None,
               retry_trigger=None, basis_revision="", attempt_id=None,
               evidence_refs=None, submission_disposition="pre_submit", conn=None):
    """Compatibility writer: append business evidence, NOT delayed work.

    Deprecated timing arguments are accepted only as historical annotations.
    The return value is an evidence ID, not a queued task or retry permission.
    """
    if not job_url or not stage or not reason_code:
        raise ValueError("Recovery history requires a job, stage and reason")
    evidence = json.dumps({"scope": scope, "basis_revision": basis_revision,
        "references": evidence_refs or [], "legacy_requested_retry_at": str(retry_at) if retry_at is not None else None,
        "legacy_retry_trigger": retry_trigger}, ensure_ascii=False, sort_keys=True)
    if len(evidence) > 32000:
        raise ValueError("Store references rather than large recovery evidence")
    key = hashlib.sha256(json.dumps([job_url, stage, reason_code, attempt_id,
                                    submission_disposition, evidence]).encode()).hexdigest()
    with transaction(conn) as db:
        db.execute("""INSERT INTO recovery_history
            (id,job_url,stage,reason_code,attempt_id,evidence_json,disposition,recorded_at)
            VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING""",
            (key, job_url, stage, reason_code, attempt_id, evidence,
             submission_disposition, datetime.now(UTC).isoformat()))
    return key


def safe_pre_submit(attempt):
    """Only an attributed negative certificate can permit a new attempt."""
    from applypilot.apply.attempt_lifecycle import safe_to_retry
    return safe_to_retry(attempt)


def legacy_pre_submit_failure(attempt):
    # Missing local browser files or worker disappearance is NOT negative proof.
    return False


def _retired(*args, **kwargs):
    raise RuntimeError("Local application recovery execution is retired; use ApplicationWorkflow evidence/review")


claim_due_recovery = _retired
complete_recovery = _retired
recover_one = _retired
credit_safe_failures = _retired
seed_backlog = _retired
recovery_delay = _retired


def recovery_schedule(conn):
    """Read-only compatibility for deferred dashboards: there is no local queue."""
    return {"due": 0, "next_retry_at": None, "execution_owner": "temporal"}


def snapshot(conn, *, limit=20):
    if type(limit) is not int or not 0 <= limit <= 128:
        raise ValueError("History limit must be between 0 and 128")
    exists = conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename='recovery_history'").fetchone()
    items = [dict(row) for row in conn.execute(
        "SELECT * FROM recovery_history ORDER BY recorded_at DESC,id DESC LIMIT ?", (limit,))] if exists else []
    return {"available": bool(exists), "execution_owner": "temporal", "items": items,
            "pending": 0, "running": 0, "completed": 0, "overdue": 0,
            "ready_now": 0, "next_retry_at": None, "oldest_overdue_seconds": 0,
            "history_only": True}

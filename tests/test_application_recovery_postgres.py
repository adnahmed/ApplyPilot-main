"""Opt-in PostgreSQL checks; only an explicitly isolated validation DB is accepted."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from applypilot import postgres
from applypilot.apply import attempt_lifecycle as lifecycle, guard, service
from applypilot.migrations import application_recovery

pytestmark = pytest.mark.skipif(not os.getenv("APPLYPILOT_ISOLATED_DATABASE_URL"),
                                reason="APPLYPILOT_ISOLATED_DATABASE_URL is opt-in")


def isolated_url():
    value = os.environ["APPLYPILOT_ISOLATED_DATABASE_URL"]
    parsed = urlsplit(value)
    directory = Path(os.environ.get("APPLYPILOT_DIR", "")).resolve()
    if (parsed.hostname not in {"127.0.0.1", "localhost"}
            or parsed.path != "/applypilot_validation" or parsed.username != "validation"
            or not directory.is_relative_to(Path(tempfile.gettempdir()).resolve())
            or not directory.name.startswith("applypilot-isolated-")):
        raise ValueError("Integration checks require a disposable validation database and temporary APPLYPILOT_DIR")
    return value


@pytest.fixture
def database():
    dsn = isolated_url()
    schema = "recovery_" + uuid4().hex
    admin = psycopg.connect(dsn, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        raw = psycopg.connect(dsn, autocommit=True)
        raw.execute(sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(schema)))
        return postgres.Connection(raw, Path(os.environ["APPLYPILOT_DIR"]) / "integration.pgstore",
                                   schema, isolation_level=None, timeout=5)

    conn = connect()
    try:
        conn.execute("""CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, state TEXT, apply_status TEXT, apply_category TEXT,
            apply_error TEXT, eligibility TEXT, discovery_source_status TEXT,
            application_attempt_id TEXT, applied_at TEXT)""")
        conn.execute("""CREATE TABLE application_attempts (
            attempt_id TEXT PRIMARY KEY, application_id TEXT, job_url TEXT, operation_id TEXT, status TEXT,
            adapter_dispatch_protocol TEXT, snapshot_json TEXT, adapter_invocation_id TEXT,
            adapter_started_at TEXT, adapter_ended_at TEXT, browser_runtime TEXT,
            browser_capture_invocation TEXT, browser_ambiguous_action_at TEXT, side_effect_intent_at TEXT,
            cancellation_evidence_json TEXT, verification_reason TEXT, updated_at TEXT, doc_format TEXT,
            channel TEXT, proof_kind TEXT, proof TEXT)""")
        conn.execute("""CREATE TABLE job_state_transitions (
            id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, job_url TEXT NOT NULL,
            from_state TEXT, to_state TEXT NOT NULL, at TEXT NOT NULL, reason TEXT, metadata TEXT)""")
        yield conn, connect
    finally:
        conn.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


def seed(conn, suffix, *, state="manual_only", status="cancelled_before_submit", intent=False,
         applied=False, owner=False, active=False, unknown=False):
    now = datetime.now(UTC).isoformat()
    attempt = {"attempt_id": "attempt-" + suffix, "application_id": "application-" + suffix,
        "operation_id": "operation-" + suffix, "job_url": "https://example.test/" + suffix,
        "status": status, "adapter_dispatch_protocol": lifecycle.PROTOCOL,
        "snapshot_json": '{"title":"Role"}', "adapter_invocation_id": "activity-" + suffix,
        "adapter_ended_at": None if active else now, "side_effect_intent_at": now if intent else None,
        "browser_ambiguous_action_at": now if unknown else None, "browser_runtime": "browseros",
        "channel": "external", "doc_format": "pdf", "verification_reason": "An unseen provider failure"}
    attempt["cancellation_evidence_json"] = json.dumps(lifecycle.cancellation_evidence(attempt, "Original failure"))
    names = list(attempt)
    conn.execute("INSERT INTO application_attempts(" + ",".join(names) + ") VALUES (" +
                 ",".join("?" for _ in names) + ")", tuple(attempt.values()))
    conn.execute("INSERT INTO jobs(url,title,state,apply_status,apply_category,apply_error,eligibility,"
                 "application_attempt_id,applied_at) VALUES(?,?,?,?,?,?,?,?,?)",
                 (attempt["job_url"], "Role", state, "applied" if applied else "manual", "archived_platform",
                  "Original failure", "eligible", attempt["attempt_id"], now if applied else None))
    metadata = {"attempt_id": attempt["attempt_id"], "reason_code": "An unseen provider failure"}
    if owner:
        metadata["source"] = "operator"
    conn.execute("INSERT INTO job_state_transitions(job_url,to_state,at,reason,metadata) VALUES(?,?,?,?,?)",
                 (attempt["job_url"], state, now, "Original transition", json.dumps(metadata)))
    return attempt


def test_real_migration_archives_reopens_once_and_preserves_uncertainty(database):
    conn, _ = database
    reopened = seed(conn, "recoverable")
    held = seed(conn, "quarantine", state="verification_quarantined", status="verification_quarantined")
    retained = [seed(conn, "intent", intent=True, status="verification_required"),
                seed(conn, "submitted", status="submitted", applied=True),
                seed(conn, "excluded", owner=True), seed(conn, "archived", state="archived"),
                seed(conn, "active", active=True), seed(conn, "ambiguous", unknown=True)]
    conn.execute("BEGIN")
    assert application_recovery.migrate(conn) == 2
    conn.commit()
    for row in (reopened, held):
        job = conn.execute("SELECT * FROM jobs WHERE url=?", (row["job_url"],)).fetchone()
        assert job["state"] == "ready_to_apply"
        assert job["apply_status"] is None
        current = dict(conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?", (row["attempt_id"],)).fetchone())
        assert lifecycle.safe_to_retry(current)
    for row in retained:
        job = conn.execute("SELECT * FROM jobs WHERE url=?", (row["job_url"],)).fetchone()
        assert job["state"] in {"manual_only", "archived"}
        assert job["apply_error"] == "Original failure"
    history = conn.execute("SELECT payload_json FROM retired_execution_history ORDER BY row_identity").fetchall()
    assert len(history) == 2
    assert all(json.loads(item[0])["job"]["apply_error"] == "Original failure" for item in history)
    conn.execute("BEGIN")
    assert application_recovery.migrate(conn) == 0
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM retired_execution_history").fetchone()[0] == 2


def test_real_migration_rolls_back_archive_and_state_together(database):
    conn, _ = database
    row = seed(conn, "rollback")
    conn.execute("BEGIN")
    assert application_recovery.migrate(conn) == 1
    conn.rollback()
    assert conn.execute("SELECT state FROM jobs WHERE url=?", (row["job_url"],)).fetchone()[0] == "manual_only"
    assert conn.execute("SELECT to_regclass('retired_execution_history')").fetchone()[0] is None


def test_real_dispatch_receipt_fences_concurrent_adapter_invocations(database, monkeypatch):
    conn, connect = database
    row = seed(conn, "concurrent", state="applying", status="prepared", active=True)
    conn.execute("UPDATE application_attempts SET adapter_invocation_id=NULL WHERE attempt_id=?", (row["attempt_id"],))
    monkeypatch.setattr(guard, "validate_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(guard, "_current_job", lambda *args, **kwargs: {"title": "Role"})
    assert guard.attempt_snapshot({"title": "Role"}) == row["snapshot_json"]
    barrier = threading.Barrier(2)

    def compete(invocation):
        local = connect()
        try:
            barrier.wait(timeout=5)
            return lifecycle.begin_dispatch(row["attempt_id"], row["application_id"], row["operation_id"],
                                            invocation, conn=local)
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(compete, "invocation-" + str(index)) for index in range(2)]
        assert sorted(future.result(timeout=15) for future in futures) == [False, True]
    current = conn.execute("SELECT adapter_invocation_id FROM application_attempts WHERE attempt_id=?",
                           (row["attempt_id"],)).fetchone()[0]
    assert current in {"invocation-0", "invocation-1"}


def test_real_completed_dispatch_replay_never_reenters_live_submission(database, monkeypatch):
    from applypilot.apply import launcher
    from applypilot.orchestration.contracts import job_identity
    conn, _ = database
    row = seed(conn, "replay", state="applied", status="submitted", applied=True)
    assert lifecycle.begin_dispatch(row["attempt_id"], row["application_id"], row["operation_id"],
                                    "redelivery", conn=conn) is False
    assert conn.execute("SELECT adapter_invocation_id FROM application_attempts WHERE attempt_id=?",
                        (row["attempt_id"],)).fetchone()[0] == row["adapter_invocation_id"]
    application_id = job_identity("testing", row["job_url"])
    conn.execute("UPDATE application_attempts SET application_id=? WHERE attempt_id=?",
                 (application_id, row["attempt_id"]))
    monkeypatch.setattr(service, "get_connection", lambda: conn)
    monkeypatch.setattr(service, "close_connection", lambda: None)
    monkeypatch.setattr(service, "current_task", lambda: {"key": "workflow/run/activity"})
    monkeypatch.setattr(service, "check_cancelled", lambda: None)

    def no_submission(*args, **kwargs):
        raise AssertionError("A replayed completion cannot invoke the external adapter")

    monkeypatch.setattr(launcher, "run_job", no_submission)
    result = service.submit({"job_url": row["job_url"], "job_id": application_id}, {"options": {}},
        {"workspace": "testing", "application_id": application_id,
         "attempt_id": row["attempt_id"], "operation_id": row["operation_id"]})
    assert result["state"] == "submitted"

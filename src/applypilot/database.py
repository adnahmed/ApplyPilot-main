"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import hashlib
import json
import logging
import os
import re as _re
from applypilot import postgres
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from applypilot.backlog import MAX_ENRICH_RETRIES, MAX_SCORE_RETRIES
from applypilot.config import DB_PATH

_log = logging.getLogger(__name__)

# Workers cache logical handles; idle handles don't reserve pooled connections.
_local = threading.local()


def write_with_retry(conn: postgres.Connection, fn, *args, max_retries=2, base_delay=0.05, **kwargs):
    """Compatibility entry point for one atomic write; callers own retries."""
    from applypilot.discovery.repository import transaction
    with transaction(conn, immediate=False):
        return fn(*args, **kwargs)


FLUSH_BATCH_SIZE = 15


def chunked_write_with_retry(
    conn: postgres.Connection,
    fn,
    items: list,
    *args,
    batch_size: int = FLUSH_BATCH_SIZE,
    **kwargs,
) -> None:
    """Call fn(conn, chunk, *args, **kwargs) for each batch of *items*.

    Each batch is committed via write_with_retry so the transaction is
    released between chunks, giving other writers (apply workers, pipeline
    stages) a chance to proceed.  Prevents "database is locked" when flushing
    large tailor/score/cover batches.
    """
    for i in range(0, len(items), batch_size):
        chunk = items[i : i + batch_size]
        write_with_retry(conn, fn, conn, chunk, *args, **kwargs)


def commit_with_retry(conn: postgres.Connection, max_retries=1, base_delay=0):
    """Compatibility name for existing owners; PostgreSQL COMMIT is attempted once."""
    conn.commit()



def get_connection(db_path: Path | str | None = None) -> postgres.Connection:
    """Return a process/thread-owned handle that leases connections during SQL."""
    path = str(db_path or DB_PATH)
    if getattr(_local, 'pid', None) != os.getpid():
        _local.connections = {}
        _local.pid = os.getpid()
    conn = _local.connections.get(path)
    if conn is None or conn.closed:
        if conn is not None:
            conn.close()
        conn = postgres.connect(path, timeout=5)
        _local.connections[path] = conn
    return conn



def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if getattr(_local, 'pid', None) == os.getpid():
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


_schema_init_lock = threading.RLock()
_schema_ready: set[tuple[int, str]] = set()


def _after_fork() -> None:
    global _schema_init_lock
    _schema_init_lock = threading.RLock()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(after_in_child=_after_fork)


def init_db(db_path: Path | str | None = None) -> postgres.Connection:
    """Create the fresh PostgreSQL schema once, with cross-process DDL ownership."""
    key = os.getpid(), str(db_path or DB_PATH)
    with _schema_init_lock:
        if key not in _schema_ready:
            postgres.initialize_store(db_path or DB_PATH)
        conn = get_connection(db_path)
        if key not in _schema_ready:
            try:
                with conn.schema_lock():
                    _init_db_schema(db_path)
                _schema_ready.add(key)
            except BaseException:
                conn.rollback()
                close_connection(db_path)
                raise
        return conn



def _init_db_schema(db_path: Path | str | None = None) -> postgres.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, scored_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Brief:      interview_brief_path, brief_generated_at, brief_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, application_attempt_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        postgres.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    columns = dict(_ALL_COLUMNS)
    columns['queue_id'] = 'BIGINT GENERATED ALWAYS AS IDENTITY UNIQUE'
    conn.execute('CREATE TABLE IF NOT EXISTS jobs (' + ','.join(
        postgres.identifier(name) + ' ' + dtype for name, dtype in columns.items()) + ')')
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            site TEXT NOT NULL,
            domain TEXT NOT NULL,
            email TEXT NOT NULL,
            password TEXT,
            created_at TEXT NOT NULL,
            job_url TEXT REFERENCES jobs(url),
            notes TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracking_emails (
            email_id       TEXT PRIMARY KEY,
            thread_id      TEXT,
            job_url        TEXT NOT NULL REFERENCES jobs(url),
            sender         TEXT,
            sender_name    TEXT,
            subject        TEXT,
            received_at    TEXT,
            snippet        TEXT,
            body_text      TEXT,
            classification TEXT,
            extracted_data TEXT,
            classified_at  TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracking_people (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            job_url         TEXT NOT NULL REFERENCES jobs(url),
            name            TEXT,
            title           TEXT,
            email           TEXT,
            source_email_id TEXT REFERENCES tracking_emails(email_id),
            first_seen_at   TEXT,
            UNIQUE(job_url, email)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS qa_knowledge (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            question_text   TEXT NOT NULL,
            question_key    TEXT NOT NULL,
            answer_text     TEXT NOT NULL,
            answer_source   TEXT NOT NULL,
            field_type      TEXT,
            options_json    TEXT,
            ats_slug        TEXT,
            job_url         TEXT,
            outcome         TEXT DEFAULT 'unknown',
            created_at      TEXT NOT NULL,
            updated_at      TEXT,
            UNIQUE(question_key, answer_text)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_qa_key ON qa_knowledge(question_key)"
    )

    # Source health + company registry tables owned by their respective modules.
    # Call their ensure_* functions so the schema stays in sync with the code
    # that writes to it (avoid column mismatches).
    from applypilot.discovery.freehire_registry import ensure_registry_table
    from applypilot.discovery.health import ensure_source_health_table

    ensure_source_health_table(conn, commit=False)
    ensure_registry_table(conn, commit=False)

    commit_with_retry(conn)

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    from applypilot.migrations.application_identity import migrate as migrate_application_identity
    from applypilot.migrations.company_execution import retire_company_execution
    from applypilot.discovery.repository import transaction
    with transaction(conn):
        migrate_application_identity(conn)
        retire_company_execution(conn)

    # New queue readers fail closed for legacy rows, but still require their
    # additive projection tables to exist. This does not activate a context.
    from applypilot.discovery.repository import ensure_schema as ensure_discovery_schema
    from applypilot.apply.guard import install_attempt_schema

    # Discovery indexes reference reservation migration fields. Upgrade the
    # attempts table before building those indexes, on new AND existing DBs.
    install_attempt_schema(conn)
    from applypilot.apply.submission_evidence import install_capture_schema
    install_capture_schema(conn)
    ensure_discovery_schema(conn)
    from applypilot.discovery.freshness import initialize as initialize_source_observations
    initialize_source_observations(conn)
    from applypilot.tracking.feedback_store import ensure_schema as ensure_feedback_schema
    ensure_feedback_schema(conn)
    from applypilot.referrals.store import ensure_schema as ensure_referral_schema
    ensure_referral_schema(conn)
    from applypilot.company_discovery.store import ensure_schema as ensure_company_discovery
    ensure_company_discovery(conn)
    from applypilot.career_ops.store import ensure_schema as ensure_career_ops
    ensure_career_ops(conn)
    from applypilot.career_ops.invitations import seed as seed_career_invitations
    seed_career_invitations(conn)
    from applypilot.integrations.state import SourceState
    SourceState().initialize()
    from applypilot.apply.email_application import ensure_schema as ensure_email_schema
    ensure_email_schema(conn)
    from applypilot.recovery import ensure_schema as ensure_recovery_schema
    ensure_recovery_schema(conn)





    from applypilot.apply.readiness import ensure_schema as ensure_apply_readiness
    ensure_apply_readiness(conn)
    from applypilot.migrations.retire_execution import application_execution_v4, discovery_execution_v4, integration_execution_v4
    from applypilot.discovery.repository import transaction
    with transaction(conn):
        application_execution_v4(conn)
        from applypilot.migrations.application_recovery import migrate as migrate_application_recovery
        migrate_application_recovery(conn)
        discovery_execution_v4(conn)
        integration_execution_v4(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending_score ON jobs(fit_score) "
                 "WHERE applied_at IS NULL AND full_description IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending_enrich ON jobs(discovered_at) "
                 "WHERE applied_at IS NULL")
    for field in ('tailored_at', 'cover_letter_at', 'applied_at'):
        conn.execute(f'CREATE INDEX IF NOT EXISTS idx_jobs_finish_{field} ON jobs({field} DESC) WHERE {field} IS NOT NULL')
    commit_with_retry(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "strategy": "TEXT",
    # Semantic dedup signature (company|title|city|region)
    "signature": "TEXT",
    "discovered_at": "TEXT",
    "posted_at": "TEXT",                     # original posting date from the employer/board
    # Canonical pipeline state. Enum enforced in Python (see VALID_STATES)
    # rather than a SQL CHECK — PostgreSQL can't easily add CHECKs post-hoc.
    "state": "TEXT DEFAULT 'discovered'",
    # Company
    "company": "TEXT",
    "company_industry": "TEXT",            # Indeed: industry of hiring company
    "company_employees": "TEXT",           # Indeed: employee count label (e.g. "1001 to 5000")
    "company_revenue": "TEXT",             # Indeed: revenue label (e.g. "$100M to $500M")
    "company_description": "TEXT",         # Indeed: brief company description
    # Naukri-specific fields
    "skills": "TEXT",                      # Naukri: required skills (JSON list)
    "experience_range": "TEXT",            # Naukri: e.g. "2-5 years"
    "work_from_home_type": "TEXT",         # Naukri: "Remote" | "Hybrid" | "Work from office"
    # Enrichment
    "full_description": "TEXT",
    "application_contract_json": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    "detail_error_category": "TEXT",       # 'expired' | 'retriable' | 'permanent' | 'partial'
    # Market salary (FreeHire insights)
    "salary_p25": "INTEGER",
    "salary_p50": "INTEGER",
    "salary_p75": "INTEGER",
    "salary_source": "TEXT",
    "salary_sample_size": "INTEGER",
    "salary_currency": "TEXT",
    "salary_period": "TEXT",
    # Scoring
    "fit_score": "INTEGER",
    "fit_requirements": "TEXT",            # JSON array of {requirement, kind, evidence_class}
    "score_reasoning": "TEXT",
    "scored_at": "TEXT",
    "score_assessment_revision": "TEXT",
    "company_research_revision": "TEXT",  # employer evidence revision, independent of candidate fit
    "posting_suitability_json": "TEXT",   # revisioned sourced credibility/engagement decision
    "score_error": "TEXT",                 # set when all LLM providers failed; fit_score stays NULL
    # 2026-04-30: terminal eligibility tag set by scorer.
    # Values: NULL (not yet evaluated) | 'eligible' | 'non_us_only'.
    # Tailor/cover/apply require explicit current eligibility.
    "eligibility": "TEXT",
    # (score_attempts → score_attempts via _COLUMN_RENAMES below)
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "document_variant": "TEXT",  # actual generated presentation label, not a career taxonomy
    "tailor_attempts": "INTEGER DEFAULT 0",
    "tailor_error": "TEXT",
    "tailor_deferred_count": "INTEGER DEFAULT 0",
    "cover_error": "TEXT",
    "cover_deferred_count": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Interview defense brief (2026-09-02, #66 sidecar): per-job brief for
    # the top eligible ledger record. Non-blocking — never gates apply.
    "interview_brief_path": "TEXT",
    "brief_generated_at": "TEXT",
    "brief_attempts": "INTEGER DEFAULT 0",
    # Impact evidence pack (2026-09-01, #65): immutable per-job selected
    # evidence snapshot JSON (SelectedEvidenceSnapshot.to_dict()).
    "selected_evidence": "TEXT",
    # Reusable JD analysis (2026-09-02, interview sidecar wiring): the single
    # generalized JobSignals artifact computed ONCE per job during scoring and
    # consumed by the matcher/claim-pack and the interview defense arrow. See
    # CLAUDE.md decision #66. JSON: JobSignals.to_dict().
    "job_signals": "TEXT",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "apply_recovery_credit": "INTEGER NOT NULL DEFAULT 0",
    "apply_pre_submit_failures": "INTEGER NOT NULL DEFAULT 0",
    "apply_retry_after": "TEXT",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "application_attempt_id": "TEXT",
    "verification_confidence": "TEXT",
    # Tracking
    "tracking_status": "TEXT",
    "tracking_updated_at": "TEXT",
    "tracking_doc_path": "TEXT",
    "last_email_at": "TEXT",
    "next_action": "TEXT",
    "next_action_due": "TEXT",
    # Apply category (semantic classification of apply outcome)
    # DEPRECATED 2026-04-24: consolidated into the `state` column. Kept for
    # backward compat; new code should use state + job_state_transitions.
    "apply_category": "TEXT",
    # Renamed 2026-04-24 for cross-stage consistency
    # (old names detail_retry_count / score_attempts are RENAMEd below).
    "enrich_attempts": "INTEGER DEFAULT 0",
    "enrich_recovery_revision": "TEXT",
    "score_attempts": "INTEGER DEFAULT 0",
}


def ensure_columns(
    conn: postgres.Connection | None = None,
    *,
    commit: bool | None = None,
) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PostgreSQL catalog metadata and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    Also handles one-shot renames defined in `_COLUMN_RENAMES`. PostgreSQL 3.25+
    supports ALTER TABLE ... RENAME COLUMN; we run the rename once if the
    old name exists and the new one doesn't.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added OR renamed (empty if current).
    """
    owns_transaction = conn is None
    if conn is None:
        conn = get_connection()
    if commit is None:
        commit = owns_transaction

    existing = {row[1] for row in conn.table_info('jobs').fetchall()}
    changed: list[str] = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            changed.append(col)

    # Ensure the job_state_transitions audit table exists (added 2026-04-24).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_state_transitions (
            id         BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            job_url    TEXT NOT NULL REFERENCES jobs(url) ON DELETE CASCADE,
            from_state TEXT,
            to_state   TEXT NOT NULL,
            at         TEXT NOT NULL,
            reason     TEXT,
            metadata   TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jst_job ON job_state_transitions(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jst_at ON job_state_transitions(at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jst_operation_span ON job_state_transitions("
                 "COALESCE(ap_json_text(metadata,'$._operation_span_id'),ap_json_text(metadata,'$.operation_span_id'))) "
                 "WHERE ap_json_valid(metadata)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
    # Dashboard aggregates must not repeatedly read pages containing full resumes
    # and job descriptions. This compact covering index also serves old databases.
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dashboard_summary "
                 "ON jobs(company,fit_score,state,discovered_at,applied_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_signature ON jobs(signature)")

    # Route and send-state tables are additive.  They deliberately do not
    # backfill jobs: existing application rows keep their exact values until
    # the operator explicitly runs a future repair command.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_routes (
            job_url      TEXT PRIMARY KEY REFERENCES jobs(url) ON DELETE CASCADE,
            route        TEXT NOT NULL,
            target       TEXT NOT NULL,
            evidence     TEXT NOT NULL,
            resolved_at  TEXT NOT NULL,
            version      INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_routes_kind ON application_routes(route)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_attempts (
            attempt_id       TEXT PRIMARY KEY,
            job_url              TEXT NOT NULL REFERENCES jobs(url) ON DELETE CASCADE,
            submission_identity  TEXT NOT NULL,
            channel              TEXT NOT NULL,
            target               TEXT NOT NULL,
            browser_runtime      TEXT NOT NULL,
            worker_id            INTEGER,
            status               TEXT NOT NULL,
            prepared_at          TEXT NOT NULL,
            side_effect_intent_at TEXT,
            submitted_at         TEXT,
            proof_kind           TEXT,
            proof                TEXT,
            verification_reason  TEXT,
            updated_at           TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_job ON application_attempts(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_identity ON application_attempts(submission_identity)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_status ON application_attempts(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_job_status ON application_attempts(job_url,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attempts_target_status ON application_attempts(target,status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_routes_target ON application_routes(target)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_application_url ON jobs(application_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_apply_queue ON jobs(eligibility,apply_status,fit_score DESC)")
    # Durable uniqueness for active/uncertain/submitted attempts on the same
    # application target (canonical requisition form). Terminal outcomes
    # (verified_absent, cancelled_before_submit) are excluded so a safe retry
    # is never blocked. Guarded during creation: a legacy database may already
    # hold duplicate active rows for one target; those are surfaced as a
    # warning instead of failing startup. New writes are still protected by
    # the explicit conflict check in guard.prepare_application_attempt.
    # These uniqueness constraints are mandatory, not optional warnings.
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_active_target
        ON application_attempts(target) WHERE channel != 'email' AND status IN
        ('prepared','side_effect_intent','verification_required','verification_quarantined','submitted')""")
    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_active_email_identity
        ON application_attempts(submission_identity) WHERE channel='email' AND status IN
        ('prepared','side_effect_intent','verification_required','verification_quarantined','submitted')""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_activity (
            id          BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            event_type  TEXT NOT NULL,
            job_url     TEXT,
            occurred_at TEXT NOT NULL,
            signal_type TEXT,
            metadata    TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_linkedin_activity_at ON linkedin_activity(occurred_at)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS linkedin_governor_state (
            singleton_id          INTEGER PRIMARY KEY CHECK(singleton_id = 1),
            manual_block_reason   TEXT,
            updated_at            TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS domain_circuit_breakers (
            domain          TEXT PRIMARY KEY,
            status          TEXT NOT NULL,
            reason          TEXT NOT NULL,
            opened_at       TEXT NOT NULL,
            retry_after     TEXT,
            manual_clear    INTEGER NOT NULL DEFAULT 0,
            updated_at      TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_answers (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            job_url         TEXT NOT NULL REFERENCES jobs(url) ON DELETE CASCADE,
            question_key    TEXT NOT NULL,
            question_text   TEXT NOT NULL,
            answer_text     TEXT NOT NULL,
            provenance      TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE(job_url, question_key)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_outcomes (
            id                       BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            job_url                  TEXT NOT NULL REFERENCES jobs(url) ON DELETE CASCADE,
            event_type               TEXT NOT NULL,
            occurred_at              TEXT NOT NULL,
            source                   TEXT NOT NULL,
            notes                    TEXT,
            submission_identifier    TEXT,
            document_variant         TEXT,
            document_hash            TEXT,
            role_family              TEXT,
            market                   TEXT,
            selected_proof_ids_json  TEXT,
            created_at               TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_outcomes_job ON application_outcomes(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_outcomes_event ON application_outcomes(event_type, occurred_at)")
    # Feedback migration owns message/submission-scoped outcome idempotency.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_run_metrics (
            id              BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
            job_url         TEXT,
            worker_id       INTEGER,
            browser_runtime TEXT,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            input_tokens    INTEGER DEFAULT 0,
            output_tokens   INTEGER DEFAULT 0,
            cache_read      INTEGER DEFAULT 0,
            tool_calls      INTEGER DEFAULT 0,
            snapshots       INTEGER DEFAULT 0,
            screenshots     INTEGER DEFAULT 0,
            duration_ms     INTEGER,
            outcome         TEXT
        )
    """)

    if commit:
        commit_with_retry(conn)

    return changed






# One-shot column renames. When the old name exists but the new one doesn't,
# ensure_columns() issues ALTER TABLE ... RENAME COLUMN once. After the
# rename lands in prod, the entry can stay here indefinitely (idempotent).



# ---------------------------------------------------------------------------
# Pipeline state machine
# ---------------------------------------------------------------------------
#
# Each job flows through a lifecycle captured by the `state` column on the
# jobs table. Transitions are recorded in `job_state_transitions` for audit.
# The valid transitions are enforced in `transition_state()` below.
#
# State taxonomy:
#
#   discovered       → found by scraper, no description yet
#   enriched         → full description loaded
#   enrich_failed    → permanent enrichment failure (e.g. 404, bad_data)
#   scored           → LLM scored with fit_score set
#   score_failed     → LLM scoring failed after retries
#   low_score        → score < threshold (effectively terminal)
#   tailoring        → in progress
#   tailored         → resume ready
#   tailor_failed    → permanent tailoring failure
#   cover_writing    → in progress
#   cover_failed     → permanent cover failure
#   ready_to_apply   → resume + cover + application_url all present
#   applying         → worker holding this (in-flight)
#   applied          → submission completed
#   apply_failed     → permanent apply failure
#   manual_only      → user must apply manually (ATS blocks automation)
#   responded        → confirmation email received
#   interview        → got an interview
#   offer            → received an offer
#   rejected         → explicit rejection
#   ghosted          → no response after follow-ups
#   archived         → opted out / manually closed

VALID_STATES: frozenset[str] = frozenset({
    "discovered", "enriched", "enrich_failed",
    "scored", "score_failed", "low_score",
    "tailoring", "tailored", "tailor_failed",
    "cover_writing", "cover_failed",
    "ready_to_apply", "applying", "applied",
    "apply_failed", "manual_only",
    "verification_required", "verification_quarantined",
    "responded", "interview", "offer",
    "rejected", "ghosted", "archived",
})

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "discovered":     frozenset({"enriched", "enrich_failed", "archived"}),
    "enriched":       frozenset({"scored", "score_failed", "archived"}),
    "enrich_failed":  frozenset({"enriched", "archived"}),  # retriable
    "scored":         frozenset({"low_score", "tailoring", "tailored", "tailor_failed", "archived"}),
    "score_failed":   frozenset({"scored", "archived"}),
    "low_score":      frozenset({"archived", "tailoring"}),  # manual override
    "tailoring":      frozenset({"tailored", "tailor_failed"}),
    "tailor_failed":  frozenset({"tailoring", "tailored", "archived"}),
    "tailored":       frozenset({"cover_writing", "ready_to_apply", "archived"}),
    "cover_writing":  frozenset({"ready_to_apply", "cover_failed"}),
    "cover_failed":   frozenset({"cover_writing", "ready_to_apply", "archived"}),
    "ready_to_apply": frozenset({"applying", "manual_only", "archived"}),
    "applying":       frozenset({"applied", "apply_failed", "ready_to_apply", "verification_required", "verification_quarantined"}),
    "apply_failed":   frozenset({"applying", "manual_only", "archived", "verification_required"}),
    "verification_required": frozenset({"verification_quarantined", "applied", "ready_to_apply", "archived"}),
    "verification_quarantined": frozenset({"verification_required", "applied", "archived"}),
    "manual_only":    frozenset({"applied", "archived"}),
    "applied":        frozenset({"responded", "ghosted", "rejected", "archived"}),
    "responded":      frozenset({"interview", "rejected", "ghosted"}),
    "interview":      frozenset({"offer", "rejected", "ghosted"}),
    "offer":          frozenset({"archived"}),  # accept or decline → archived
    "rejected":       frozenset({"archived"}),
    "ghosted":        frozenset({"archived", "responded"}),  # reopen possible
    "archived":       frozenset(),
}


def transition_state(conn: postgres.Connection, job_url: str, to_state: str,
                     reason: str | None = None,
                     metadata: dict | None = None,
                     force: bool = False) -> bool:
    """Atomically transition a job to a new state and write an audit row.

    Validates the transition against VALID_TRANSITIONS unless force=True.
    Uses BEGIN so concurrent writers don't race on the state
    column. Returns True on success, False if the transition is illegal.

    metadata, when provided, is serialized to JSON and stored.
    """
    if to_state not in VALID_STATES:
        raise ValueError(f"Unknown state: {to_state!r}")

    nested = conn.in_transaction
    savepoint = f"state_{threading.get_ident()}_{time.time_ns()}"
    try:
        conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN")
        row = conn.execute("SELECT state FROM jobs WHERE url = ?", (job_url,)).fetchone()
        if not row:
            raise ValueError(f"Job not found: {job_url}")
        from_state = row["state"] if isinstance(row, postgres.Row) else row[0]

        if not force:
            allowed = VALID_TRANSITIONS.get(from_state, frozenset())
            if to_state not in allowed and to_state != from_state:
                _log.debug("Rejected transition %s → %s for %s (allowed: %s)",
                           from_state, to_state, job_url[:60], sorted(allowed))
                if nested:
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                else:
                    conn.rollback()
                return False

        now = datetime.now(timezone.utc).isoformat()
        meta_json = json.dumps(metadata, default=str) if metadata else None
        updated = conn.execute(
            "UPDATE jobs SET state = ? WHERE url = ? AND state IS NOT DISTINCT FROM ?",
            (to_state, job_url, from_state),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"concurrent state transition for {job_url}")
        conn.execute(
            "INSERT INTO job_state_transitions (job_url, from_state, to_state, at, reason, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (job_url, from_state, to_state, now, reason, meta_json),
        )
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
        return True
    except BaseException:
        if nested:
            try:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            except BaseException:
                conn.rollback()
        else:
            conn.rollback()
        raise


def current_state(conn: postgres.Connection, job_url: str) -> str | None:
    """Return the job's current state, or None if the job doesn't exist."""
    row = conn.execute("SELECT state FROM jobs WHERE url = ?", (job_url,)).fetchone()
    if not row:
        return None
    return row["state"] if isinstance(row, postgres.Row) else row[0]


def state_history(conn: postgres.Connection, job_url: str) -> list[dict]:
    """Return all state transitions for a job, newest first."""
    rows = conn.execute(
        "SELECT from_state, to_state, at, reason, metadata "
        "FROM job_state_transitions WHERE job_url = ? ORDER BY id DESC",
        (job_url,),
    ).fetchall()
    return [dict(r) if isinstance(r, postgres.Row) else
            {"from_state": r[0], "to_state": r[1], "at": r[2],
             "reason": r[3], "metadata": r[4]}
            for r in rows]






# ---------------------------------------------------------------------------
# Apply category classification
# ---------------------------------------------------------------------------

def categorize_apply_result(apply_status: str | None,
                            apply_error: str | None) -> str:
    """Project observed application lifecycle; error text never decides execution."""
    return {None: "pending", "applied": "applied", "in_progress": "in_progress",
            "failed": "apply_retry", "manual": "manual_only",
            "verification_required": "verification_required",
            "verification_quarantined": "verification_quarantined"}.get(apply_status, "pending")



def reset_by_category(category: str,
                      conn: postgres.Connection | None = None) -> int:
    """Reset all jobs in a given category so they can be retried.

    Clears apply_status, apply_error, apply_attempts, and apply_category.

    Args:
        category: The apply_category to reset (e.g., 'blocked_technical').

    Returns:
        Number of jobs reset.
    """
    if conn is None:
        conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, apply_recovery_credit = 0, agent_id = NULL,
                       apply_category = NULL
        WHERE apply_category = ?
    """, (category,))
    commit_with_retry(conn)
    return cursor.rowcount


def prune_stale_attempts(conn: postgres.Connection | None = None) -> int:
    """Preserve application-attempt evidence until lease-aware expiry exists.

    State labels alone cannot prove that an external submission did not happen.
    In particular, verification-required/quarantined rows are duplicate-submit
    guards, not stale reservations. Safe recovery is handled by the fenced
    lifecycle instead of deleting audit records here.
    """
    return 0


def get_stats(conn: postgres.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, with_brief, pending_brief, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    stats["routes"] = {
        row["route"]: row["cnt"]
        for row in conn.execute(
            "SELECT route, COUNT(*) AS cnt FROM application_routes GROUP BY route"
        ).fetchall()
    }
    stats["guard_states"] = {
        row["status"]: row["cnt"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM application_attempts GROUP BY status"
        ).fetchall()
    }

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        "SELECT site, COUNT(*) as cnt FROM jobs GROUP BY site ORDER BY cnt DESC"
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL"
    ).fetchone()[0]

    from applypilot.config import DEFAULTS as _DEFAULTS
    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score >= ? AND full_description IS NOT NULL "
        "AND tailored_resume_path IS NULL "
        "AND eligibility = 'eligible'",
        (_DEFAULTS["min_score"],),
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Interview brief stage (sidecar)
    stats["with_brief"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE interview_brief_path IS NOT NULL AND interview_brief_path != ''"
    ).fetchone()[0]

    stats["pending_brief"] = conn.execute(
        "SELECT COUNT(*) FROM jobs j WHERE " + interview_brief_pending_sql()
    ).fetchone()[0]
    stats["brief_exhausted"] = 0  # Attempts are history, not a permanent stop.

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND applied_at IS NULL "
        "AND application_url IS NOT NULL "
        "AND eligibility = 'eligible'"
    ).fetchone()[0]

    # Apply category breakdown with per-score counts
    cat_rows = conn.execute("""
        SELECT apply_category,
               COUNT(*) AS total,
               SUM(CASE WHEN fit_score = 10 THEN 1 ELSE 0 END) AS s10,
               SUM(CASE WHEN fit_score = 9  THEN 1 ELSE 0 END) AS s9,
               SUM(CASE WHEN fit_score = 8  THEN 1 ELSE 0 END) AS s8,
               SUM(CASE WHEN fit_score = 7  THEN 1 ELSE 0 END) AS s7,
               SUM(CASE WHEN fit_score = 6  THEN 1 ELSE 0 END) AS s6,
               SUM(CASE WHEN fit_score < 6 OR fit_score IS NULL THEN 1 ELSE 0 END) AS s_low
        FROM jobs
        WHERE apply_category IS NOT NULL
        GROUP BY apply_category
        ORDER BY total DESC
    """).fetchall()
    stats["by_category"] = {
        row[0]: {
            "total": row[1],
            "10": row[2], "9": row[3], "8": row[4],
            "7": row[5], "6": row[6], "<6": row[7],
        }
        for row in cat_rows
    }

    # Per-score funnel: breakdown of pipeline stage at each score level (6-10)
    funnel_rows = conn.execute("""
        SELECT
            fit_score,
            SUM(CASE WHEN applied_at IS NOT NULL THEN 1 ELSE 0 END)            AS applied,
            SUM(CASE WHEN applied_at IS NULL
                      AND cover_letter_path IS NOT NULL
                      AND COALESCE(apply_status,'') NOT IN ('applied','manual')
                      AND COALESCE(state,'') NOT IN ('verification_required','verification_quarantined')
                 THEN 1 ELSE 0 END)                                             AS cover_ready,
            SUM(CASE WHEN applied_at IS NULL
                      AND tailored_resume_path IS NOT NULL
                      AND (cover_letter_path IS NULL OR cover_letter_path = '')
                      AND COALESCE(apply_status,'') NOT IN ('applied','manual')
                      AND COALESCE(state,'') NOT IN ('verification_required','verification_quarantined')
                 THEN 1 ELSE 0 END)                                             AS tailored,
            SUM(CASE WHEN applied_at IS NULL
                      AND tailored_resume_path IS NULL
                      AND full_description IS NOT NULL
                      AND COALESCE(apply_status,'') NOT IN ('applied','manual')
                 THEN 1 ELSE 0 END)                                             AS needs_tailor,
            SUM(CASE WHEN apply_error IS NOT NULL THEN 1 ELSE 0 END)           AS errors
        FROM jobs
        WHERE fit_score >= 6
        GROUP BY fit_score
        ORDER BY fit_score DESC
    """).fetchall()
    stats["score_funnel"] = [
        {"score": row[0], "applied": row[1], "cover_ready": row[2],
         "tailored": row[3], "needs_tailor": row[4], "errors": row[5]}
        for row in funnel_rows
    ]

    # Funnel diagnostics (2026-04-23 funnel spec)
    from applypilot.config import DEFAULTS, get_company_limit
    max_age = DEFAULTS["max_job_age_days"]

    # Ready-to-apply jobs that would be skipped by the age cutoff
    stats["skipped_stale"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND (apply_status IS NULL OR apply_status = 'failed') "
        "AND (discovered_at IS NULL OR discovered_at <= ap_datetime('now', ?))",
        (f"-{max_age} days",),
    ).fetchone()[0]

    # Companies currently blocked by the per-company cap
    in_flight = get_in_flight_by_company(conn)
    blocked_companies: list[str] = []
    for co, stamps in in_flight.items():
        cap, window = get_company_limit(co)
        if cap == 0:
            blocked_companies.append(co)
            continue
        if cap < 0:
            continue
        cutoff = (datetime.now(timezone.utc) - timedelta(days=window)).isoformat()
        recent_count = sum(1 for ts in stamps if ts and ts > cutoff)
        if recent_count >= cap:
            blocked_companies.append(co)

    stats["blocked_by_cap"] = {
        "count": len(blocked_companies),
        "companies": sorted(blocked_companies)[:20],
    }

    return stats


def extract_company(application_url: str | None) -> str | None:
    """Extract a company name from an application URL domain.

    Handles common ATS patterns:
      - Workday:     {company}.wd*.myworkdayjobs.com
      - Greenhouse:  job-boards.greenhouse.io/{company}/...
      - Lever:       jobs.lever.co/{company}/...
      - iCIMS:       careers-{company}.icims.com
      - Jobvite:     jobs.jobvite.com/en/{company}/...
      - Direct:      careers.{company}.com, {company}.com/careers
    """
    if not application_url:
        return None
    try:
        from urllib.parse import urlparse
        parsed = urlparse(application_url)
        host = parsed.hostname or ""
        path = parsed.path or ""

        # Workday: workiva.wd503.myworkdayjobs.com → workiva
        if "myworkdayjobs.com" in host:
            return host.split(".")[0].lower()

        # Greenhouse job boards: job-boards.greenhouse.io/hudl/... → hudl
        # Greenhouse embed:     job-boards.greenhouse.io/embed/job_app?for=coinbase → coinbase
        if "greenhouse.io" in host:
            from urllib.parse import parse_qs, urlparse as _urlparse
            qs = parse_qs(_urlparse(application_url).query)
            if "for" in qs:
                return qs["for"][0].lower()
            if "/job_boards/" not in path:
                parts = [p for p in path.split("/") if p and p not in ("embed", "job_app")]
                if parts:
                    return parts[0].lower()

        # Lever: jobs.lever.co/LuminDigital/... → lumindigital
        if "lever.co" in host:
            parts = [p for p in path.split("/") if p]
            if parts:
                return parts[0].lower()

        # iCIMS: careers-mercuryinsurance.icims.com → mercuryinsurance
        if "icims.com" in host:
            prefix = host.split(".icims.com")[0]
            prefix = prefix.replace("careers-", "").replace("careers.", "")
            return prefix.lower() if prefix else None

        # Jobvite: jobs.jobvite.com/en/company/... → company
        if "jobvite.com" in host:
            parts = [p for p in path.split("/") if p and p != "en"]
            if parts:
                return parts[0].lower()

        # Ashby: jobs.ashbyhq.com/{company-slug}/... → company-slug
        if "ashbyhq.com" in host:
            from urllib.parse import unquote
            parts = [p for p in path.split("/") if p]
            if parts:
                return unquote(parts[0]).lower()

        # Rippling ATS: ats.rippling.com/{company-slug}/jobs/... → company-slug
        if "rippling.com" in host and host.startswith("ats."):
            parts = [p for p in path.split("/") if p]
            if parts:
                return parts[0].lower()

        # Workable: apply.workable.com/{company}/j/... → company
        # (short-form apply.workable.com/j/CODE has no company name — skip those)
        if "workable.com" in host:
            parts = [p for p in path.split("/") if p]
            if parts and parts[0] != "j":
                return parts[0].lower()

        # Recruitee: {company}.recruitee.com/... → company
        if "recruitee.com" in host:
            sub = host.split(".recruitee.com")[0]
            if sub and sub not in ("www", "app", "jobs"):
                return sub.lower()

        # SmartRecruiters: careers.smartrecruiters.com/{Company}/... → company
        if "smartrecruiters.com" in host:
            parts = [p for p in path.split("/") if p]
            if parts:
                return parts[0].lower()

        # Oracle Cloud ATS: skip (company not in URL)
        if "oraclecloud.com" in host:
            return None

        # Greenhouse short URLs: grnh.se → skip
        if host == "grnh.se":
            return None

        # Direct company domains: jobs.twilio.com → twilio
        # careers.ascensus.com → ascensus
        # www.kentik.com/careers → kentik
        # Skip major job boards / ATS platforms
        skip_domains = {
            "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
            "dice.com", "simplyhired.com", "monster.com", "careerjet.ca",
            "talent.com", "jobbank.gc.ca", "wellfound.com",
        }
        if any(host.endswith(d) for d in skip_domains):
            return None

        # Strip common subdomains
        parts = host.split(".")
        if len(parts) >= 2:
            # jobs.twilio.com → twilio, careers.foo.com → foo, www.foo.com → foo
            if parts[0] in ("jobs", "careers", "career", "www", "apply", "hire"):
                company = parts[1]
            else:
                # foo.com → foo
                company = parts[-2]
            # Skip generic TLDs as company names
            if company not in ("com", "org", "net", "io", "co", "ca"):
                return company.lower()

        return None
    except Exception:
        return None


def backfill_companies(conn: postgres.Connection | None = None) -> int:
    """Populate the company column for all jobs that have an application_url but no company."""
    if conn is None:
        conn = get_connection()

    rows = conn.execute(
        "SELECT url, application_url FROM jobs WHERE company IS NULL AND application_url IS NOT NULL"
    ).fetchall()

    updated = 0
    for row in rows:
        company = extract_company(row[1])
        if company:
            conn.execute("UPDATE jobs SET company = ? WHERE url = ?", (company, row[0]))
            updated += 1

    if updated:
        commit_with_retry(conn)
    return updated


def store_account(conn: postgres.Connection, account: dict,
                  job_url: str | None = None) -> None:
    """Store a newly created account in the accounts table.

    Args:
        conn: Database connection.
        account: Dict with keys: site, domain, email, password,
                 and optionally login_method ('email' | 'linkedin').
        job_url: The job URL that triggered this account creation.
    """
    now = datetime.now(timezone.utc).isoformat()
    # login_method is stored in notes so it shows up in the CLI and prompt
    notes = account.get("notes") or account.get("login_method")
    conn.execute(
        "INSERT INTO accounts (site, domain, email, password, created_at, job_url, notes) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            account.get("site", "unknown"),
            account.get("domain", "unknown"),
            account.get("email", ""),
            account.get("password", ""),
            now,
            job_url,
            notes,
        ),
    )
    commit_with_retry(conn)


def get_all_accounts(conn: postgres.Connection | None = None) -> list[dict]:
    """Return all rows from the accounts table, newest first."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT id, site, domain, email, password, notes, created_at, job_url "
        "FROM accounts ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def mine_accounts_from_logs(log_dir: str) -> list[dict]:
    """Scan apply log files and extract credential hints.

    Two strategies:
    1. Structured: lines matching ``ACCOUNT_CREATED:{...}`` (current format).
    2. Free-form: ATS subdomain URLs appearing within 60 lines of a
       ``Password: XXXX`` line (older logs where the agent wrote prose).

    Returns a list of dicts with keys: domain, email, password, source_file.
    Duplicates (same domain) are collapsed, keeping the first occurrence.
    """
    import re
    from pathlib import Path

    log_path = Path(log_dir)
    if not log_path.is_dir():
        return []

    # Regex patterns
    account_created_re = re.compile(r'ACCOUNT_CREATED:\s*(\{.*\})')
    password_re = re.compile(
        r'(?:^|[-*\s])Password[:\s]+([A-Za-z0-9!@#$%^&*()\-_=+]{8,32})\s*$',
        re.IGNORECASE | re.MULTILINE,
    )
    # ATS subdomain patterns worth capturing
    domain_re = re.compile(
        r'https?://([a-z0-9][\w.-]+\.'
        r'(?:myworkdayjobs\.com|icims\.com|taleo\.net|successfactors\.com|'
        r'greenhouse\.io|lever\.co|ashbyhq\.com|jobvite\.com))',
        re.IGNORECASE,
    )

    results: dict[str, dict] = {}  # domain → entry

    for log_file in sorted(log_path.glob("claude_*.txt")):
        try:
            text = log_file.read_text(errors="replace")
        except OSError:
            continue

        lines = text.splitlines()

        # Strategy 1: structured ACCOUNT_CREATED lines
        for line in lines:
            m = account_created_re.search(line)
            if not m:
                continue
            try:
                entry = json.loads(m.group(1))
                domain = entry.get("domain", "").strip()
                email  = entry.get("email", "").strip()
                pwd    = entry.get("password", "").strip()
                site   = entry.get("site", "").strip()
                if domain and email and domain not in results:
                    results[domain] = {
                        "domain": domain, "email": email,
                        "password": pwd, "site": site,
                        "source": "structured", "source_file": log_file.name,
                    }
            except (json.JSONDecodeError, AttributeError):
                continue

        # Strategy 2: free-form — find password mentions near ATS domain URLs
        # Build index: line_no → domain found on that line
        domain_lines: list[tuple[int, str]] = []
        for i, line in enumerate(lines):
            dm = domain_re.search(line)
            if dm:
                domain_lines.append((i, dm.group(1).lower()))

        if not domain_lines:
            continue

        # Find all password mentions with their line numbers
        for pm in password_re.finditer(text):
            pwd = pm.group(1).strip()
            # Ignore template-like passwords from the prompt injection
            if pwd in ("{personal", "personal.get", "password", "PASSWORD"):
                continue
            pwd_lineno = text[:pm.start()].count("\n")

            # Find the nearest domain within a 60-line window
            nearest = min(
                domain_lines,
                key=lambda t: abs(t[0] - pwd_lineno),
                default=None,
            )
            if nearest is None or abs(nearest[0] - pwd_lineno) > 60:
                continue
            domain = nearest[1]
            if domain in results:
                continue  # structured entry takes priority

            # Try to find the email on nearby lines
            window = "\n".join(lines[max(0, pwd_lineno - 10): pwd_lineno + 10])
            email_m = re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', window)
            email = email_m.group(0) if email_m else ""

            results[domain] = {
                "domain": domain, "email": email,
                "password": pwd, "site": "",
                "source": "free-form", "source_file": log_file.name,
            }

    return list(results.values())


def upsert_account(domain: str, email: str, password: str | None,
                   site: str | None = None, notes: str | None = None,
                   conn: postgres.Connection | None = None) -> str:
    """Insert or update a credential row for a domain.

    If a row already exists for the domain, updates email/password/notes.
    Returns "created" or "updated".
    """
    if conn is None:
        conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM accounts WHERE domain = ? ORDER BY created_at DESC LIMIT 1",
        (domain,)
    ).fetchone()
    now = datetime.now(timezone.utc).isoformat()
    if existing:
        updates = ["email = ?", "password = ?"]
        params: list = [email, password]
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if site is not None:
            updates.append("site = ?")
            params.append(site)
        params.append(existing["id"])
        conn.execute(
            f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?", params
        )
        commit_with_retry(conn)
        return "updated"
    else:
        conn.execute(
            "INSERT INTO accounts (site, domain, email, password, notes, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (site or domain.split(".")[0], domain, email, password, notes, now),
        )
        commit_with_retry(conn)
        return "created"


def delete_account(domain: str,
                   conn: postgres.Connection | None = None) -> int:
    """Delete all credential rows for a domain. Returns number of rows deleted."""
    if conn is None:
        conn = get_connection()
    cursor = conn.execute("DELETE FROM accounts WHERE domain = ?", (domain,))
    commit_with_retry(conn)
    return cursor.rowcount


def interview_invitation_pending_sql(alias: str = "j") -> str:
    """Invited applications, including already submitted jobs; no discovery gates."""
    return (f"({alias}.tracking_status='interview' OR "
            f"({alias}.tracking_status IS NULL AND {alias}.state='interview')) AND "
            f"(NULLIF(TRIM({alias}.interview_brief_path),'') IS NULL OR "
            f"{alias}.brief_generated_at IS NULL OR EXISTS (SELECT 1 FROM tracking_emails e "
            f"WHERE e.job_url={alias}.url AND e.classification='interview' "
            f"AND ap_day(e.received_at)>ap_day({alias}.brief_generated_at)) OR "
            f"EXISTS (SELECT 1 FROM application_outcomes e WHERE e.job_url={alias}.url "
            "AND e.voided_at IS NULL AND e.event_type IN ('screen','interview','technical_interview') "
            f"AND ap_day(e.occurred_at)>ap_day({alias}.brief_generated_at)))")


def interview_brief_pending_sql(alias: str = 'j') -> str:
    """Invitation/report business readiness; only Temporal retries interview briefs."""
    return ("(" + interview_invitation_pending_sql(alias) + f" OR EXISTS (SELECT 1 FROM career_ops_reports r WHERE r.job_url={alias}.url "
            "AND r.mode='interview_brief' AND r.state IN ('pending','running','error')))" )


def preparation_checks(stage: str, min_score: int, max_age_days: int) -> list[tuple[str, str, tuple]]:
    """Named production predicates shared by preparation queues and diagnostics.

    Order controls only the first blocker explanation. Every predicate must pass.
    Company-cap ranking and admission allocation are applied to this base query
    afterwards, using the same production implementation as count/select.
    """
    from applypilot.config import DEFAULTS
    from applypilot.discovery.admission import current_admission_sql, last_seen_sql
    from applypilot.integrations.research import posting_suitability_sql
    stage = stage.removeprefix("pending_")
    if stage not in {"tailor", "cover", "brief"}:
        raise ValueError("Unknown preparation stage")
    if stage == "brief":
        return [("interview_invitation", interview_brief_pending_sql(), ())]
    checks = [
        ("already_applied", "j.applied_at IS NULL", ()),
        ("archived", "COALESCE(j.state,'')!='archived'", ()),
        ("protected_lifecycle", "COALESCE(j.state,'') NOT IN ('applying','applied',"
         "'verification_required','verification_quarantined')", ()),
        ("application_status", "(j.apply_status IS NULL OR j.apply_status='failed')", ()),
        ("protected_attempt", "NOT EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url "
         "AND aa.status IN ('prepared','side_effect_intent','verification_required',"
         "'verification_quarantined','submitted'))", ()),
        ("valid_score_timestamp", "ap_day(j.scored_at) IS NOT NULL AND ap_day(j.scored_at)<=ap_day('now')", ()),
        ("minimum_score", "j.fit_score>=?", (min_score,)),
        ("description_missing", "j.full_description IS NOT NULL", ()),
        ("score_eligibility", "j.eligibility='eligible'", ()),
        ("company_posting_suitability", posting_suitability_sql("j"), ()),
    ]
    checks.extend([
        ("application_attempt_limit", "GREATEST(0,COALESCE(j.apply_attempts,0)-COALESCE(j.apply_recovery_credit,0))<?", (DEFAULTS["max_apply_attempts"],)),
        ("application_route_missing", "EXISTS (SELECT 1 FROM application_routes ar WHERE ar.job_url=j.url "
         "AND ar.route IN ('external','linkedin-easy-apply','email') AND NULLIF(TRIM(ar.target),'') IS NOT NULL)", ()),
    ])
    if stage == "tailor":
        checks.append(("already_has_resume", "NULLIF(TRIM(j.tailored_resume_path),'') IS NULL", ()))
        checks.append(("resume_permitted", "COALESCE(NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,resume,status}','optional')!='prohibited'", ()))
    elif stage == "cover":
        checks.extend([
            ("employer_requests_cover", "COALESCE(NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,cover_letter,status}','optional')='required'", ()),
            ("resume_missing", "(NULLIF(TRIM(j.tailored_resume_path),'') IS NOT NULL OR NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,resume,status}'='prohibited')", ()),
            ("already_has_cover", "NULLIF(TRIM(j.cover_letter_path),'') IS NULL", ()),
        ])
    checks.append(("current_discovery_admission", current_admission_sql("j", "prepare"), ()))
    if max_age_days > 0:
        checks.append(("source_age", "ap_day("+last_seen_sql("j")+") > ap_day('now', ?)",
                       (f"-{max_age_days} days",)))
    return checks


def enrich_recovery_revision_sql(alias: str = "j") -> str:
    """A partial extraction can be retried when its actual source changes."""
    return ("jsonb_build_array(COALESCE((SELECT context_key FROM discovery_active_context "
            f"WHERE singleton_id=1),{alias}.discovery_context_key),"
            f"{alias}.discovery_content_hash,{alias}.discovery_source_revision)::text")


def enrich_retry_ready_sql(alias: str = "j") -> str:
    changed = (f"({alias}.detail_error_category='partial' AND "
               f"{alias}.enrich_recovery_revision IS DISTINCT FROM {enrich_recovery_revision_sql(alias)})")
    return (f"({changed} OR COALESCE({alias}.detail_error_category,'') "
            "NOT IN ('permanent','terminal'))")


def _jobs_by_stage(conn: postgres.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      max_age_days: int | None = None,
                      limit: int = 100, *, count: bool = False, exclude_urls=(), metrics=False,
                      preferred_only=False, query_only=False, include_company_cap=True, reserved_urls=(), repair_only=False,
                      include_deferred=False):
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied",
               "pending_score", "pending_tailor", "pending_cover", "pending_apply",
               "pending_brief".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        max_age_days: Exclude jobs last observed older than this many days.
                      None uses config.DEFAULTS["max_job_age_days"]. 0 disables.
                      Unknown observation time is treated as stale (excluded) when
                      filter is active.
        limit: Maximum number of rows to return (0 = no limit).

    Returns:
        List of job dicts.
    """
    from applypilot.config import DEFAULTS
    from applypilot.discovery.admission import current_admission_sql, order_sql, select_jobs

    if type(limit) is not int or limit < 0:
        raise ValueError("limit must be a nonnegative integer (0 = unlimited)")
    stage = {"enrich": "pending_detail", "score": "pending_score",
             "tailor": "pending_tailor", "cover": "pending_cover",
             "brief": "pending_brief", "apply": "pending_apply",
             "evidence": "pending_evidence"}.get(stage, stage)

    if conn is None:
        conn = get_connection()

    if min_score is None:
        min_score = DEFAULTS["min_score"]
    if max_age_days is None:
        max_age_days = DEFAULTS["max_job_age_days"]

    conditions = {
        "discovered": "1=1",
        "pending_detail": (
            "detail_scraped_at IS NULL "
            "OR (detail_error_category IN ('transient','retriable','partial') AND "
            + enrich_retry_ready_sql() + ")"
        ),
        "enriched": "full_description IS NOT NULL",
        "pending_evidence": "1=1",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL",
        "rescore": "full_description IS NOT NULL",
        "scored": "fit_score IS NOT NULL",
        # Legacy NULL and model uncertainty never grant paid-stage admission.
        "pending_tailor": (
            "fit_score >= ? AND full_description IS NOT NULL "
            "AND tailored_resume_path IS NULL "
            "AND eligibility = 'eligible'"
        ),
        "pending_cover": (
            "fit_score >= ? AND tailored_resume_path IS NOT NULL "
            "AND full_description IS NOT NULL "
            "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
            "AND eligibility = 'eligible'"
        ),
        "pending_brief": interview_brief_pending_sql(),
        "tailored": "tailored_resume_path IS NOT NULL",
        "pending_apply": (
            "fit_score >= ? AND tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND application_url IS NOT NULL "
            "AND eligibility = 'eligible'"
        ),
        "applied": "applied_at IS NOT NULL",
    }

    from applypilot.work_context import stage_context
    temporal_owned = bool(stage_context().get("temporal_owned"))
    if temporal_owned:
        conditions["pending_detail"] = (
            "detail_scraped_at IS NULL OR detail_error_category IN ('transient','retriable','partial')")
        conditions["pending_score"] = "full_description IS NOT NULL AND fit_score IS NULL"

    if repair_only and stage == "pending_detail":
        # Source revalidation is a repair, not a claim that an old score grants
        # action permission. Reuse the normal scrape/publish/assessment path.
        conditions[stage] = "(" + conditions[stage] + ") OR (fit_score>=? " + (
            "AND " + enrich_retry_ready_sql() + " "
            "AND (discovery_assessment_revision IS NULL OR discovery_source_status='unknown' "
            "OR discovery_assessed_content_hash IS NULL "
            "OR EXISTS (SELECT 1 FROM discovery_active_context ac WHERE ac.singleton_id=1 "
            "AND ac.context_key IS DISTINCT FROM j.discovery_context_key) "
            "OR NOT EXISTS (SELECT 1 FROM application_routes ar WHERE ar.job_url=j.url) "
            "OR EXISTS (SELECT 1 FROM application_routes ar WHERE ar.job_url=j.url "
            "AND ar.route IN ('external','linkedin-easy-apply','email') AND NULLIF(TRIM(ar.target),'') IS NULL) "
            "OR (NULLIF(TRIM(application_url),'') IS NULL AND detail_error_category IS NULL) "
            "OR NOT EXISTS (SELECT 1 FROM discovery_postings fp WHERE fp.job_url=j.url "
            "AND fp.source_status='open' AND fp.content_hash=j.discovery_content_hash "
            "AND fp.source_revision=j.discovery_source_revision AND ap_day(fp.last_seen_at)>ap_day('now','-' || "
            "COALESCE((SELECT ap_json_number(dc.runtime_json,'$.buffer.revalidate_after_days') FROM discovery_contexts dc "
            "WHERE dc.context_key=j.discovery_context_key),7) || ' days'))))")
    if stage not in conditions:
        raise ValueError(f"Unknown job stage: {stage}")
    if include_deferred and stage == "pending_score":
        conditions[stage] = (
            "full_description IS NOT NULL AND fit_score IS NULL AND ("
            f"score_error IS NULL OR score_attempts < {MAX_SCORE_RETRIES})"
        )
    where = "(" + conditions[stage] + ")"
    params: list = []

    if "?" in where:
        params.append(min_score)

    if stage in ("scored", "tailored", "applied") and "fit_score" not in where:
        where += " AND fit_score >= ?"
        params.append(min_score)

    admission_stage = {"pending_detail": "detail", "pending_evidence": "evidence",
                       "pending_score": "score", "rescore": "score", "pending_tailor": "prepare",
                       "pending_cover": "prepare",
                       "pending_apply": "apply"}.get(stage)
    if admission_stage:
        where += " AND " + current_admission_sql("j", admission_stage)
        if admission_stage == "apply":
            from applypilot.integrations.research import posting_suitability_sql
            where += " AND " + posting_suitability_sql("j")
        if admission_stage == "detail":
            where += " AND COALESCE(j.site, '') NOT IN ('glassdoor','google','Workopolis')"
            where += (" AND (j.url NOT LIKE 'discovery:%'"
                      " OR NULLIF(TRIM(j.application_url),'') IS NOT NULL)")
        where += (" AND j.applied_at IS NULL" +
                  " AND (j.apply_status IS NULL OR j.apply_status = 'failed')"
                  " AND COALESCE(j.state, '') NOT IN ('applying','applied',"
                  "'verification_required','verification_quarantined','archived')"
                  " AND NOT EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url"
                  " AND aa.status IN ('prepared','side_effect_intent','verification_required',"
                  "'verification_quarantined','submitted'))")

    if repair_only:
        if stage == "pending_detail":
            # A source recheck can repair old high-fit evidence even when description
            # acquisition is stocked. Failed requests retain their ordinary cooldowns.
            # This does not authorize preparation/application or rewrite source dates.
            where += (" AND j.fit_score>=? AND COALESCE(j.eligibility,'unknown') IN ('eligible','unknown')"
                      " AND GREATEST(0,COALESCE(j.apply_attempts,0)-COALESCE(j.apply_recovery_credit,0))<? AND " + enrich_retry_ready_sql())
            params.extend([min_score, DEFAULTS["max_apply_attempts"]])
        else:
            raise ValueError("repair_only is supported only for enrichment")

    # Source rechecks deliberately ignore job age; ordinary queues use the configured limit.
    if max_age_days > 0 and not (repair_only and stage == "pending_detail"):
        from applypilot.discovery.admission import last_seen_sql
        where += " AND ap_day(" + last_seen_sql("j") + ") > ap_day('now', ?)"
        params.append(f"-{max_age_days} days")

    if stage in {"pending_tailor", "pending_cover", "pending_brief"}:
        checks = preparation_checks(stage, min_score, max_age_days)
        if temporal_owned:
            checks = [item for item in checks if item[0] not in {"worker_ownership", "application_attempt_limit"}
                      and not item[0].endswith("_retry_backoff")]
            checks = [(name, sql.replace("NULLIF(TRIM(j.tailored_resume_path),'') IS NULL",
                        "(NULLIF(TRIM(j.tailored_resume_path),'') IS NULL OR COALESCE(j.tailor_repair_pending,0)=1)")
                        .replace("NULLIF(TRIM(j.cover_letter_path),'') IS NULL",
                        "(NULLIF(TRIM(j.cover_letter_path),'') IS NULL OR COALESCE(j.cover_repair_pending,0)=1)"), values)
                      for name, sql, values in checks]
        if include_deferred:
            from applypilot.integrations.research import posting_suitability_sql
            checks = [check for check in checks if not check[0].endswith("_retry_backoff")]
            checks = [(name, "(" + sql + " OR NOT " + posting_suitability_sql("j", require_suitable=False) + ")", values)
                      if name == "company_posting_suitability" else (name, sql, values)
                      for name, sql, values in checks]
        where = " AND ".join("(" + sql + ")" for _, sql, _ in checks)
        params = [p for _, _, values in checks for p in values]

    from applypilot.work_context import stage_context
    context = stage_context()
    selected_urls = context.get('job_urls')
    if selected_urls is not None:
        selected_urls = tuple(dict.fromkeys(selected_urls))
        where += " AND j.url IN (SELECT value FROM jsonb_array_elements_text(CAST(? AS jsonb)) AS item(value))"
        params.append(json.dumps(selected_urls))
    selected = set(selected_urls or ())
    reserved = tuple(url for url in dict.fromkeys((*reserved_urls, *context.get('reserved_urls', ())))
                     if url not in selected)
    excluded = tuple(dict.fromkeys((*exclude_urls, *reserved)))
    if preferred_only:
        where += " AND j.discovery_disposition='ranked'"
    if excluded:
        where += " AND j.url NOT IN (SELECT value FROM jsonb_array_elements_text(CAST(? AS jsonb)) AS item(value))"
        params.append(json.dumps(excluded))
    query = f"SELECT j.* FROM jobs j WHERE {where}"
    if admission_stage == "apply" and not temporal_owned:
        from applypilot.apply.queue import company_policy_json
        query += (" AND ap_company_under_cap(j.company,j.site,j.strategy,"
                  "COALESCE((SELECT r.target FROM application_routes r WHERE r.job_url=j.url),"
                  "j.application_url),j.url,?)")
        params.append(company_policy_json())
    if stage in {"pending_tailor", "pending_cover"} and include_company_cap and DEFAULTS["max_tailored_per_company"] >= 0:
        company = "ap_company_key(j.company,j.site,j.strategy,j.application_url,j.url)"
        fresh = " AND ap_day(j.discovered_at) > ap_day('now', ?)" if max_age_days > 0 else ""
        existing_params = [f"-{max_age_days} days"] if max_age_days > 0 else []
        document = "tailored_resume_path" if stage == "pending_tailor" else "cover_letter_path"
        repair_column = ("tailor_repair_pending"
                         if stage == "pending_tailor" else "cover_repair_pending")
        occupied = (f"(NULLIF(TRIM(j.{document}),'') IS NOT NULL"
                    f" AND COALESCE(j.{repair_column},0)=0{fresh})")
        if reserved:
            occupied += " OR j.url IN (SELECT value FROM jsonb_array_elements_text(CAST(? AS jsonb)) AS item(value))"
            existing_params.append(json.dumps(reserved))
        query = (f"WITH occupied AS (SELECT {company} AS company_key, COUNT(*) AS n FROM jobs j "
                 f"WHERE {occupied} GROUP BY company_key), "
                 f"candidates AS (SELECT j.*, {company} AS company_key, ROW_NUMBER() OVER ("
                 f"PARTITION BY {company} ORDER BY {order_sql('j')}) AS company_rank FROM ({query}) j) "
                 "SELECT j.* FROM candidates j LEFT JOIN occupied o ON o.company_key=j.company_key "
                 "WHERE j.company_key IS NULL OR j.company_rank + COALESCE(o.n,0) <= ?")
        params = existing_params + params + [DEFAULTS["max_tailored_per_company"]]
    if query_only:
        # Read-only diagnostic construction: no selection, allocation-credit
        # mutation, hydration, or claim. UDFs are connection-local only.
        return query, params, admission_stage
    if metrics:
        from applypilot.discovery.repository import transaction
        from applypilot.discovery.admission import _allocation
        metric_query, metric_params = query, list(params)
        if admission_stage in {"prepare", "apply"}:
            allocation = _allocation(conn, None, "apply" if admission_stage == "apply" else "prepare")
            if allocation is None:
                return {"pending": 0, "oldest_runnable_age": None}
            key, share, transfer, _credit, fill = allocation
            lanes = []
            if share < 1 or transfer or fill:
                lanes.append("j.discovery_disposition='ranked'")
            if share > 0 or fill:
                lanes.append("j.discovery_disposition IN ('explore','off_portfolio')")
            if not lanes:
                return {"pending": 0, "oldest_runnable_age": None}
            metric_query = f"SELECT j.* FROM ({query}) j WHERE j.discovery_context_key=? AND (" + " OR ".join(lanes) + ")"
            metric_params.append(key)
        with transaction(conn, immediate=False):
            row = conn.execute("SELECT COUNT(*), MAX(GREATEST(0,(ap_day('now')-ap_day(j.discovered_at))*86400)) "
                               f"FROM ({metric_query}) j", metric_params).fetchone()
        return {"pending": row[0], "oldest_runnable_age": row[1]}
    if stage_context().get("temporal_owned") and admission_stage in {"score", "prepare", "apply"}:
        # Durable task matching replaces allocation-credit selection. Preserve
        # the business/evidence predicates already expressed by this query.
        if count:
            return conn.execute(f"SELECT COUNT(*) FROM ({query}) j", params).fetchone()[0]
        query = f"SELECT j.* FROM ({query}) j ORDER BY {order_sql('j')}"
        if limit:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
    elif admission_stage in {"score", "prepare", "apply"}:
        if count:
            from applypilot.discovery.admission import count_candidates
            return count_candidates(conn, query, params,
                lane="apply" if admission_stage == "apply" else "prepare", stage=admission_stage)
        # Public CLI zero is unlimited; select_jobs zero is a finite empty
        # budget. Translate using the actual candidate count, never a fallback.
        budget = limit or conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]
        rows = select_jobs(conn, query, params, limit=budget,
                           lane="apply" if admission_stage == "apply" else "prepare",
                           stage=admission_stage, consume=True)
    elif count:
        return conn.execute(f"SELECT COUNT(*) FROM ({query})", params).fetchone()[0]
    else:
        priority = ("CASE j.discovery_role_status WHEN 'relevant' THEN 0 ELSE 1 END, "
                    "COALESCE(j.enrich_attempts,0) ASC, "
                    if admission_stage in {"detail", "evidence"} else "")
        if repair_only:
            priority = "CASE WHEN NULLIF(j.tailored_resume_path,'') IS NOT NULL THEN 0 ELSE 1 END, " + priority
        query = f"SELECT j.* FROM ({query}) j ORDER BY {priority}{order_sql('j')}"
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)
        rows = conn.execute(query, params).fetchall()
    if rows and not isinstance(rows[0], dict):
        columns = rows[0].keys()
        rows = [dict(zip(columns, r)) for r in rows]
    from applypilot.discovery.repository import hydrate_job
    return [hydrate_job(conn, row) for row in rows]


def get_recovery_jobs(conn: postgres.Connection, min_score: int | None = None, limit: int = 2,
                      *, exclude_urls=(), stage: str = "enrich") -> list[dict]:
    """Bounded source recovery through the existing lifecycle guards.

    Reading an old source does not authorize preparing or applying. A successful
    source read must publish current evidence before the normal consumers run.
    """
    if stage != "enrich" or type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Recovery needs enrich and a limit between 1 and 128")
    return _jobs_by_stage(conn, "pending_detail",
                          min_score=min_score, max_age_days=0, limit=limit,
                          exclude_urls=exclude_urls, repair_only=True)


def count_recovery_jobs(conn: postgres.Connection, min_score: int | None = None, limit: int = 32,
                        *, stage: str = "enrich") -> int:
    """Capped, read-only inventory; never consumes discovery allocation credit."""
    if stage != "enrich" or type(limit) is not int or not 1 <= limit <= 128:
        raise ValueError("Recovery needs enrich and a limit between 1 and 128")
    query, params, _ = _jobs_by_stage(conn, "pending_detail",
                                    min_score=min_score, max_age_days=0, repair_only=True, query_only=True)
    return conn.execute(f"SELECT COUNT(*) FROM ({query} LIMIT ?) q", (*params, limit)).fetchone()[0]


def get_jobs_by_stage(conn: postgres.Connection | None = None,
                      stage: str = "discovered", min_score: int | None = None,
                      max_age_days: int | None = None, limit: int = 100, *, exclude_urls=(), reserved_urls=()) -> list[dict]:
    """List the common stage predicate/order; CLI limit=0 remains unlimited."""
    from applypilot.work_context import stage_context
    selected = stage_context().get('selected_jobs', {})
    if stage in selected:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer (0 = unlimited)")
        excluded = set(exclude_urls) | set(reserved_urls)
        rows = [row for row in selected[stage] if row['url'] not in excluded]
        return rows[:limit] if limit else rows
    return _jobs_by_stage(conn, stage, min_score, max_age_days, limit, exclude_urls=exclude_urls,
                          reserved_urls=reserved_urls)


def preview_jobs_by_stage(conn: postgres.Connection | None = None,
                          stage: str = "discovered", min_score: int | None = None,
                          max_age_days: int | None = None, limit: int = 100, *,
                          exclude_urls=(), reserved_urls=(), repair_only=False) -> list[dict]:
    """Read bounded graph candidates without spending preparation allocation."""
    if type(limit) is not int or limit < 1:
        raise ValueError("preview limit must be a positive integer")
    from applypilot.discovery.admission import order_sql, select_jobs
    conn = conn or get_connection()
    query, params, admission = _jobs_by_stage(
        conn, stage, min_score, max_age_days, limit, exclude_urls=exclude_urls,
        reserved_urls=reserved_urls, repair_only=repair_only, query_only=True)
    if admission in {'score', 'prepare', 'apply'}:
        rows = select_jobs(conn, query, params, limit=limit,
                           lane='apply' if admission == 'apply' else 'prepare',
                           stage=admission, consume=False)
    else:
        priority = ("CASE j.discovery_role_status WHEN 'relevant' THEN 0 ELSE 1 END, "
                    "COALESCE(j.enrich_attempts,0) ASC, "
                    if admission in {'detail', 'evidence'} else '')
        if repair_only:
            priority = "CASE WHEN NULLIF(j.tailored_resume_path,'') IS NOT NULL THEN 0 ELSE 1 END, " + priority
        rows = conn.execute(f"SELECT j.* FROM ({query}) j ORDER BY {priority}{order_sql('j')} LIMIT ?",
                            (*params, limit)).fetchall()
    return [dict(row) for row in rows]


def get_stage_queue_metrics(conn: postgres.Connection | None = None, stage: str = "discovered",
                            min_score: int | None = None, max_age_days: int | None = None) -> dict:
    """Shared runnable queue count and age of its oldest discovered job."""
    return _jobs_by_stage(conn, stage, min_score, max_age_days, metrics=True)


def count_jobs_by_stage(conn: postgres.Connection | None = None,
                        stage: str = "discovered",
                        min_score: int | None = None,
                        max_age_days: int | None = None) -> int:
    """Count eligible tasks awaiting service in enabled allocation lanes.

    Shares the admission/lifecycle predicate with ``get_jobs_by_stage`` but
    does not apply fractional allocation to the queue length. The worker must
    run to spend its actual batch budget and advance fractional carry; a
    read-only count cannot do that. Exhausted, blocked, and not-yet-due retry
    rows are excluded. Counts do not reserve work or promise a dispatch slot.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: Same stage names accepted by ``get_jobs_by_stage``.
        min_score: Minimum fit_score filter (scored+ stages only).
        max_age_days: Exclude jobs last observed older than this many days.
                      0 disables the filter.
    """
    return _jobs_by_stage(conn, stage, min_score, max_age_days, 0, count=True)


def preparation_stock_sql(alias="j"):
    """Unconsumed inventory survives temporary admission, age and retry holds."""
    from applypilot.discovery.admission import _alias
    j = _alias(alias)
    return (f"{j}.applied_at IS NULL "
            f"AND ({j}.apply_status IS NULL OR {j}.apply_status='failed') "
            f"AND COALESCE({j}.state,'') NOT IN ('applying','applied',"
            "'verification_required','verification_quarantined','archived') "
            f"AND COALESCE({j}.discovery_source_status,'unknown')!='closed' "
            f"AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_url={j}.url "
            "AND a.status IN ('prepared','side_effect_intent','verification_required',"
            "'verification_quarantined','submitted'))")


def preparation_inventory(conn, stage, *, min_score, max_age_days):
    """Count saved producer output separately from current consumer admission."""
    name = stage.removeprefix("pending_")
    predicates = {
        "score": "j.full_description IS NOT NULL AND j.fit_score IS NULL",
        "tailor": ("j.fit_score>=? AND (NULLIF(TRIM(j.tailored_resume_path),'') IS NULL "
                   "OR COALESCE(j.tailor_repair_pending,0)=1) AND COALESCE(NULLIF(j.application_contract_json,'')::jsonb "
                   "#>> '{documents,resume,status}','optional')!='prohibited'"),
        "cover": ("j.fit_score>=? AND (NULLIF(TRIM(j.tailored_resume_path),'') IS NOT NULL OR "
                  "NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,resume,status}'='prohibited') "
                  "AND NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,cover_letter,status}'='required' "
                  "AND (NULLIF(TRIM(j.cover_letter_path),'') IS NULL OR COALESCE(j.cover_repair_pending,0)=1)"),
    }
    query = f"SELECT j.* FROM jobs j WHERE ({preparation_stock_sql()}) AND ({predicates[name]})"
    params = () if name == "score" else (min_score,)
    route_check = "TRUE"
    if stage.removeprefix("pending_") in {"tailor", "cover"}:
        route_check = next(sql for name, sql, _ in preparation_checks(stage, min_score, max_age_days)
                           if name == "application_route_missing")
    rows = conn.execute(f"SELECT j.url, ({route_check}) FROM ({query}) j", params).fetchall()
    identities = {row[0] for row in rows}
    stock = len(identities)
    ready = count_jobs_by_stage(conn, stage, min_score, max_age_days)
    consumable = min(stock, _jobs_by_stage(conn, stage, min_score, max_age_days,
                                         count=True, include_deferred=True))
    return {"stock": stock, "ready_now": ready, "deferred": max(0, stock - ready),
            "held_stock": stock - consumable,
            "route_blocked": sum(not row[1] for row in rows),
            "_identities": identities}


# ---------------------------------------------------------------------------
# Tracking helpers
# ---------------------------------------------------------------------------


def record_application_outcome(
    job_url: str, event_type: str, *, occurred_at: str | None = None,
    source: str = "manual", notes: str = "", conn: postgres.Connection | None = None,
    submission_id: str | None = None, event_key: str | None = None,
    message_key: str | None = None, payload: dict | None = None,
    supersedes_id: int | None = None,
) -> dict:
    """Append an exact-submission event; never attach an old reply to the latest send."""
    from applypilot.tracking.feedback_store import backfill_submissions, record_event
    conn = conn or get_connection()
    backfill_submissions(conn)
    return record_event(conn, job_url, event_type, occurred_at=occurred_at, source=source,
                        notes=notes, submission_id=submission_id, event_key=event_key,
                        message_key=message_key, payload=payload, supersedes_id=supersedes_id)


def get_outcome_cohort_stats(min_age_days: int = 14, conn=None) -> list[dict]:
    """Compatible table projection of fixed-horizon, observation-covered cohorts."""
    from collections import defaultdict
    from applypilot.tracking.feedback_store import policy
    from applypilot.tracking.feedback_learning import dataset
    if type(min_age_days) is not int or min_age_days < 1:
        raise ValueError("min_age_days must be positive")
    conn = conn or get_connection()
    cfg = policy(conn) | {"horizon_days": min_age_days}
    data, _ = dataset(conn, cfg=cfg)
    groups = defaultdict(lambda: dict(mature_submissions=0,recruiter_responses=0,screens=0,technical_interviews=0,offers=0))
    for row in data:
        if not row["included"]:
            continue
        facts = row["features"]
        key = (facts.get("role_family","unknown"),facts.get("market","unknown"),facts.get("document_variant","unknown"))
        group = groups[key]
        events = {e["event_type"] for e in row["within_events"]}
        group["mature_submissions"] += 1
        group["recruiter_responses"] += int("recruiter_response" in events)
        group["screens"] += int("screen" in events)
        group["technical_interviews"] += int("technical_interview" in events)
        group["offers"] += int("offer" in events)
    return [dict(zip(("role_family","market","document_variant"),key)) | value for key,value in sorted(groups.items())]


def get_applied_jobs(conn: postgres.Connection | None = None) -> list[dict]:
    """Return all applied jobs with columns needed for email matching."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT url, title, company, application_url, applied_at, site, "
        "       tracking_status, last_email_at "
        "FROM jobs WHERE applied_at IS NOT NULL "
        "ORDER BY applied_at DESC"
    ).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_in_flight_by_company(conn: postgres.Connection | None = None,
                             max_window_days: int = 365) -> dict[str, list[str]]:
    """Return ``{company_key: [timestamp_iso, ...]}`` for in-flight jobs.

    Applied, active, and uncertain applications occupy company capacity.

    The company-key derivation MUST match what ``acquire_job`` uses at
    cap-check time, otherwise lookup misses. We delegate to
    :func:`applypilot.scoring.tailor.resolve_company_key`, which inspects
    (in order): explicit ``company`` column, ATS-tenant slug embedded in
    ``application_url`` (Greenhouse / Lever / Ashby / Workday), and finally
    ``site`` for direct-employer scrapers. Rows that resolve to None are
    skipped — caller handles NULL-company exemption separately.

    max_window_days bounds the query scan. Default 365d comfortably covers
    any reasonable ``window_days`` override in
    ``~/.applypilot/company_limits.yaml``; callers filter the returned
    lists by their own per-company window.
    """
    if conn is None:
        conn = get_connection()

    # Lazy import to avoid a circular cycle (tailor → database).
    from applypilot.scoring.tailor import resolve_company_key

    rows = conn.execute("""
        SELECT company, site, strategy, application_url, url,
               COALESCE(applied_at, last_attempted_at) AS ts
        FROM jobs
        WHERE (apply_status IN ('applied', 'in_progress')
               OR state IN ('verification_required','verification_quarantined'))
          AND COALESCE(applied_at, last_attempted_at) IS NOT NULL
          AND COALESCE(applied_at, last_attempted_at) > ap_datetime('now', ?)
    """, (f"-{max_window_days} days",)).fetchall()

    from collections import defaultdict
    out: dict[str, list[str]] = defaultdict(list)
    for r in rows:
        key = resolve_company_key(dict(r))
        if key:
            out[key].append(r["ts"])
    return dict(out)


def create_stub_job(email: dict, classification: str,
                    conn: postgres.Connection | None = None) -> str:
    """Create a minimal job entry from an unmatched application email.

    Used for manually applied jobs that aren't in the pipeline DB.
    The URL is synthesized as 'manual://{sender_domain}/{hash}'.

    Key strategy:
    - When a company name can be extracted from the subject or snippet,
      key on (domain_root, company_normalized) so all emails about the same
      company via the same ATS share one stub (Honor via Greenhouse,
      Honor re-confirmation, Honor security code, etc.).
    - Fall back to (sender:subject) when no company is extractable — keeps
      per-subject uniqueness for generic subjects like "We received your application".

    Args:
        email: Normalized email dict with sender, subject, snippet, date, etc.
        classification: The email classification (confirmation, rejection, etc.)

    Returns:
        The generated job URL (primary key).
    """
    import hashlib
    from applypilot.tracking.matcher import (
        extract_company_from_subject,
        _extract_company_from_snippet,
        normalize_company,
    )

    if conn is None:
        conn = get_connection()

    sender = email.get("sender", "")
    domain = sender.split("@")[-1] if "@" in sender else "unknown"
    domain_root = ".".join(domain.split(".")[-2:]) if "." in domain else domain

    subject = email.get("subject", "")
    snippet = email.get("snippet", "")

    # Try to extract a real company name from subject then snippet
    extracted_company = (
        extract_company_from_subject(subject)
        or _extract_company_from_snippet(snippet)
    )

    if extracted_company:
        # Key on (domain_root, company_normalized) — all emails for the same
        # company from the same ATS relay collapse to one stub
        company = extracted_company
        key = f"{domain_root}:{normalize_company(extracted_company)}"
    else:
        # Fallback: unique per sender+subject (safe for generic subjects)
        company = ""  # Unknown — don't use ATS domain name as company (would attract all emails from that ATS)
        key = f"{sender}:{subject}"

    url_hash = hashlib.md5(key.encode()).hexdigest()[:12]
    url = f"manual://{domain}/{url_hash}"

    # Check if already exists
    existing = conn.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone()
    if existing:
        return url

    # Infer a title from the subject line
    title = subject
    for prefix in ("Thank you for your application to ",
                   "Thank you for applying to ",
                   "Thank you for your interest in ",
                   "Your application to ",
                   "Application received: ",
                   "Re: "):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):]
            break

    now = datetime.now(timezone.utc).isoformat()
    email_date = email.get("date", now)

    conn.execute(
        "INSERT INTO jobs (url, title, company, site, applied_at, apply_status, "
        "                  discovered_at, tracking_status, tracking_updated_at, "
        "                  detail_error, detail_error_category) "
        "VALUES (?, ?, ?, 'manual', ?, 'applied', ?, ?, ?, "
        "        'manual:// stub — not a real job listing', 'permanent')",
        (url, title.strip() or extracted_company or "Unknown Position",
         company, email_date, now, classification, now),
    )
    commit_with_retry(conn)
    return url


_TRACKING_PRIORITY = {
    "ghosted": 1, "rejection": 2, "confirmation": 3,
    "follow_up": 4, "interview": 5, "offer": 6,
}

# Maps tracking_status (the email-derived classification) to the canonical
# state-machine state that should be set on the same job. Used by
# update_tracking_status to keep `state` in sync with `tracking_status`
# (decision #31 P0.5 leak (a) — was bypassing transition_state).
_TRACKING_TO_STATE = {
    "ghosted":      "ghosted",
    "rejection":    "rejected",
    "confirmation": "responded",
    "follow_up":    "responded",
    "interview":    "interview",
    "offer":        "offer",
}


def update_tracking_status(job_url: str, new_status: str,
                           conn: postgres.Connection | None = None) -> bool:
    """Update a job's tracking_status if the new status has higher priority.

    Returns True if the status was updated. Also threads the change through
    `transition_state` so the canonical `state` column stays in sync — fixes
    P0.5 leak (a) from CLAUDE.md decision #31.
    """
    if conn is None:
        conn = get_connection()
    row = conn.execute(
        "SELECT tracking_status FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()
    if row is None:
        return False

    current = row["tracking_status"]
    current_pri = _TRACKING_PRIORITY.get(current, 0)
    new_pri = _TRACKING_PRIORITY.get(new_status, 0)

    if new_pri > current_pri:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE jobs SET tracking_status = ?, tracking_updated_at = ? WHERE url = ?",
            (new_status, now, job_url),
        )
        commit_with_retry(conn)
        # Also emit a state-machine transition so the canonical `state`
        # column matches. transition_state validates against
        # VALID_TRANSITIONS — if the job isn't in a state that legally
        # moves to the target (e.g. still "applying"), the call returns
        # False and `tracking_status` is left as-is. We don't propagate
        # the failure since email-driven updates are advisory.
        target_state = _TRACKING_TO_STATE.get(new_status)
        if target_state:
            try:
                transition_state(
                    conn, job_url, target_state,
                    reason=f"tracking:{new_status}",
                )
                commit_with_retry(conn)
            except (ValueError, postgres.OperationalError):
                pass
        return True
    return False


def get_tracking_emails(job_url: str, conn: postgres.Connection | None = None) -> list[dict]:
    """Get all tracking emails for a job, ordered by received_at."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tracking_emails WHERE job_url = ? ORDER BY received_at ASC",
        (job_url,),
    ).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_tracking_people(job_url: str, conn: postgres.Connection | None = None) -> list[dict]:
    """Get all tracking contacts for a job."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM tracking_people WHERE job_url = ? ORDER BY first_seen_at ASC",
        (job_url,),
    ).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_action_items(conn: postgres.Connection | None = None) -> list[dict]:
    """Get all jobs with pending action items, sorted by deadline."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT url, title, company, tracking_status, next_action, next_action_due "
        "FROM jobs WHERE next_action IS NOT NULL "
        "ORDER BY CASE WHEN next_action_due IS NULL THEN 1 ELSE 0 END, next_action_due ASC"
    ).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def get_tracking_stats(conn: postgres.Connection | None = None) -> dict:
    """Return tracking status counts for the dashboard."""
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT tracking_status, COUNT(*) as cnt FROM jobs "
        "WHERE tracking_status IS NOT NULL "
        "GROUP BY tracking_status ORDER BY cnt DESC"
    ).fetchall()
    return {row["tracking_status"]: row["cnt"] for row in rows}


# ---------------------------------------------------------------------------
# Q&A Knowledge Base
# ---------------------------------------------------------------------------


def normalize_question(text: str) -> str:
    """Normalize a screening question for matching.

    Strips whitespace, punctuation, lowercases, collapses whitespace.
    """
    text = text.lower().strip()
    text = _re.sub(r'[^\w\s]', '', text)
    text = _re.sub(r'\s+', ' ', text)
    return text


def question_key(text: str) -> str:
    """Generate a stable hash key for a normalized question."""
    normalized = normalize_question(text)
    return hashlib.md5(normalized.encode()).hexdigest()


def store_qa(question: str, answer: str, source: str = "agent",
             field_type: str | None = None,
             options_json: str | None = None,
             ats_slug: str | None = None,
             job_url: str | None = None,
             conn: postgres.Connection | None = None) -> int | None:
    """Store a Q&A pair in the knowledge base (upsert by question_key + answer).

    Returns:
        The row ID of the inserted/existing row, or None on failure.
    """
    if conn is None:
        conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    key = question_key(question)
    try:
        conn.execute(
            "INSERT INTO qa_knowledge "
            "(question_text, question_key, answer_text, answer_source, "
            " field_type, options_json, ats_slug, job_url, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(question_key, answer_text) DO UPDATE SET "
            "  updated_at = ?, ats_slug = COALESCE(excluded.ats_slug, qa_knowledge.ats_slug)",
            (question, key, answer, source,
             field_type, options_json, ats_slug, job_url, now,
             now),
        )
        commit_with_retry(conn)
        row = conn.execute(
            "SELECT id FROM qa_knowledge WHERE question_key = ? AND answer_text = ?",
            (key, answer),
        ).fetchone()
        return row[0] if row else None
    except Exception:
        return None


def lookup_qa(question: str,
              conn: postgres.Connection | None = None) -> list[dict]:
    """Find known answers for a screening question.

    Returns answers sorted by outcome preference: accepted > unknown > rejected.
    """
    if conn is None:
        conn = get_connection()
    key = question_key(question)
    rows = conn.execute(
        "SELECT * FROM qa_knowledge WHERE question_key = ? "
        "ORDER BY CASE outcome "
        "  WHEN 'accepted' THEN 1 "
        "  WHEN 'unknown' THEN 2 "
        "  WHEN 'rejected' THEN 3 "
        "  ELSE 4 END, "
        "updated_at DESC NULLS LAST",
        (key,),
    ).fetchall()
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []


def _opposite_format_suffix(doc_format: str | None) -> str | None:
    """Return the file-extension pattern whose answers should be suppressed.

    When we render the agent prompt in DOCX mode, any historical Q&A answer
    that suggests a `.pdf` filename (e.g. "Jordan_Resume.pdf") is stale — the
    agent would try to upload a file that doesn't exist. The reverse applies
    in PDF mode.
    """
    if doc_format == "docx":
        return ".pdf"
    if doc_format == "pdf":
        return ".docx"
    return None


def _answer_mentions_suffix(answer: str, suffix: str) -> bool:
    """Return True if `answer` contains `suffix` as a file-extension token.

    Matches case-insensitively and only at an extension-boundary (so a URL
    fragment like 'mydocx' wouldn't match '.docx').
    """
    import re
    # \.pdf\b / \.docx\b — requires literal dot and word boundary after.
    pattern = re.escape(suffix) + r"\b"
    return re.search(pattern, answer, re.IGNORECASE) is not None


def get_qa(question: str, conn: postgres.Connection | None = None,
           doc_format: str | None = None) -> str | None:
    """Return the best known answer for a question, or None if not found.

    Looks up by normalized question key and returns the answer with the
    best outcome (accepted > unknown > rejected).

    When ``doc_format`` is "docx", answers that reference a ".pdf" filename
    are suppressed (and vice versa for "pdf"). This prevents stale
    historical Q&A rows from re-suggesting the wrong file format to the
    apply agent after a format switchover.
    """
    if conn is None:
        conn = get_connection()
    key = question_key(question)
    rows = conn.execute(
        "SELECT answer_text FROM qa_knowledge "
        "WHERE question_key = ? "
        "ORDER BY CASE outcome "
        "  WHEN 'accepted' THEN 1 "
        "  WHEN 'unknown' THEN 2 "
        "  WHEN 'rejected' THEN 3 "
        "  ELSE 4 END",
        (key,),
    ).fetchall()
    opposite = _opposite_format_suffix(doc_format)
    for row in rows:
        answer = row[0]
        if opposite and _answer_mentions_suffix(answer, opposite):
            continue
        return answer
    return None


def get_all_qa(conn: postgres.Connection | None = None,
               doc_format: str | None = None) -> list[dict]:
    """Return all Q&A pairs, grouped by question, best answer first.

    See ``get_qa`` for ``doc_format`` semantics — the same filter applies
    here to keep the KNOWN_ANSWERS prompt section format-consistent.
    """
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM qa_knowledge "
        "ORDER BY question_key, "
        "CASE outcome "
        "  WHEN 'accepted' THEN 1 "
        "  WHEN 'unknown' THEN 2 "
        "  WHEN 'rejected' THEN 3 "
        "  ELSE 4 END"
    ).fetchall()
    if not rows:
        return []
    columns = rows[0].keys()
    results = [dict(zip(columns, row)) for row in rows]
    opposite = _opposite_format_suffix(doc_format)
    if opposite:
        results = [
            r for r in results
            if not _answer_mentions_suffix(r["answer_text"], opposite)
        ]
    return results


def get_qa_stats(conn: postgres.Connection | None = None) -> dict:
    """Return Q&A knowledge base statistics."""
    if conn is None:
        conn = get_connection()
    stats: dict = {}
    stats["total"] = conn.execute("SELECT COUNT(*) FROM qa_knowledge").fetchone()[0]
    stats["unique_questions"] = conn.execute(
        "SELECT COUNT(DISTINCT question_key) FROM qa_knowledge"
    ).fetchone()[0]

    # By outcome
    rows = conn.execute(
        "SELECT outcome, COUNT(*) FROM qa_knowledge GROUP BY outcome"
    ).fetchall()
    stats["by_outcome"] = {row[0]: row[1] for row in rows}

    # By source
    rows = conn.execute(
        "SELECT answer_source, COUNT(*) FROM qa_knowledge GROUP BY answer_source"
    ).fetchall()
    stats["by_source"] = {row[0]: row[1] for row in rows}

    return stats


def export_qa_yaml(conn: postgres.Connection | None = None) -> str:
    """Export all Q&A pairs as YAML for user editing.

    Returns:
        YAML string with questions grouped and answers listed.
    """
    qa_list = get_all_qa(conn)
    if not qa_list:
        return "# No Q&A pairs yet.\n"

    lines = ["# ApplyPilot Q&A Knowledge Base", "# Edit answers below, then import with: applypilot qa import <file>", ""]
    current_key = None
    for qa in qa_list:
        if qa["question_key"] != current_key:
            current_key = qa["question_key"]
            lines.append(f"- question: \"{qa['question_text']}\"")
            if qa.get("field_type"):
                lines.append(f"  field_type: {qa['field_type']}")
            if qa.get("options_json"):
                lines.append(f"  options: {qa['options_json']}")
            lines.append("  answers:")
        outcome_tag = f" [{qa['outcome']}]" if qa["outcome"] != "unknown" else ""
        lines.append(f"    - text: \"{qa['answer_text']}\"{outcome_tag}")
    lines.append("")
    return "\n".join(lines)


# ── Impact evidence pack (2026-09-01, CLAUDE.md #65) ──────────────────────

def _has_column(conn: postgres.Connection, column: str) -> bool:
    """True when the jobs table already carries ``column`` (migration-safe read)."""
    try:
        cols = {row[1] for row in conn.table_info('jobs').fetchall()}
    except postgres.OperationalError:
        return False
    return column in cols


def get_selected_evidence(
    job_url: str, conn: postgres.Connection | None = None
) -> dict | None:
    """Return the immutable selected-evidence snapshot for a job, or None.

    The snapshot is JSON (SelectedEvidenceSnapshot.to_dict()). Never parses —
    the caller (matcher/pipeline) decodes it; a corrupt or un-migrated value
    returns None (pipeline fails open).
    """
    if conn is None:
        conn = get_connection()
    if not _has_column(conn, "selected_evidence"):
        return None
    row = conn.execute(
        "SELECT selected_evidence FROM jobs WHERE url = ?", (job_url,)
    ).fetchone()
    if not row or not row[0]:
        return None
    import json as _json
    try:
        data = _json.loads(row[0])
        return data if isinstance(data, dict) else None
    except (_json.JSONDecodeError, TypeError):
        return None


def set_selected_evidence(
    job_url: str, snapshot: dict, conn: postgres.Connection | None = None
) -> None:
    """Atomically persist the selected-evidence snapshot for a job.

    No-ops on a pre-migration DB (column absent) rather than raising — the
    pack is recomputed deterministically for the next stage anyway.
    """
    if conn is None:
        conn = get_connection()
    if not _has_column(conn, "selected_evidence"):
        return
    import json as _json
    conn.execute(
        "UPDATE jobs SET selected_evidence = ? WHERE url = ?",
        (_json.dumps(snapshot), job_url),
    )
    conn.commit()


# APPLYPILOT_DOCUMENT_RECOVERY_V4
# A recorded path is not completion when an explicit repair is outstanding.
_ALL_COLUMNS.update({
    "tailor_contract_version": "INTEGER NOT NULL DEFAULT 0",
    "tailor_repair_pending": "INTEGER NOT NULL DEFAULT 0",
    "cover_repair_pending": "INTEGER NOT NULL DEFAULT 0",
    "tailor_repair_attempts": "INTEGER NOT NULL DEFAULT 0",
    "cover_repair_attempts": "INTEGER NOT NULL DEFAULT 0",
})

_ap4_original_preparation_checks = preparation_checks

def preparation_checks(stage, min_score, max_age_days):
    checks = _ap4_original_preparation_checks(stage, min_score, max_age_days)
    result = []
    for name, sql, params in checks:
        if name == "already_has_resume":
            sql = "(" + sql + " OR COALESCE(j.tailor_repair_pending,0)=1)"
        elif name == "already_has_cover":
            sql = "(" + sql + " OR COALESCE(j.cover_repair_pending,0)=1)"
        result.append((name, sql, params))
    if stage.removeprefix("pending_") == "cover":
        result.append((
            "resume_repair_pending",
            "COALESCE(j.tailor_repair_pending,0)=0",
            (),
        ))
    return result

"""Durable discovery publication; no provider, profile-file or model I/O.

Activate an explicit business context, then publish_records for each fetched
page. Normalization/assessment/JSON compilation precede the short write lock.
Supplied connections are never closed. Nested operations use savepoints and do
not commit their caller's work. ensure_schema is additive and NEVER commits.
"""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from fractions import Fraction
import hashlib
import json
import math
import re
import time
from urllib.parse import parse_qsl, urlsplit, urlunsplit
from uuid import uuid4

from .models import IngestionResult
from . import payloads


JOB_COLUMNS = {
    "discovery_context_key": "TEXT",
    "discovery_assessment_revision": "TEXT",
    "discovery_content_hash": "TEXT",
    "discovery_assessed_content_hash": "TEXT",
    "discovery_intent_fingerprint": "TEXT",
    "discovery_profile_version": "TEXT",
    "discovery_rules_version": "TEXT",
    "discovery_role_status": "TEXT",
    "discovery_role_tier": "INTEGER",
    "discovery_eligibility": "TEXT",
    "discovery_disposition": "TEXT",
    "discovery_preference_id": "TEXT",
    "discovery_preference_rank": "INTEGER",
    "discovery_selected_arrangement": "TEXT",
    "discovery_prepare_allowed": "INTEGER NOT NULL DEFAULT 0",
    "discovery_apply_allowed": "INTEGER NOT NULL DEFAULT 0",
    "discovery_source_status": "TEXT",
    "discovery_source_revision": "INTEGER",
    "discovery_normalized_json": "TEXT",
    "discovery_assessment_json": "TEXT",
    "discovery_published_at": "TEXT",
    "discovery_assumptions_json": "TEXT",
    "discovery_role_evaluation_json": "TEXT",
}

# Enrichment may change these outside publication. Invalidate, never reset the
# application lifecycle, scoring artifacts, ownership, or submission attempts.
CONTENT_COLUMNS = (
    "url", "title", "company", "description", "full_description", "location",
    "salary", "application_url", "site", "posted_at", "salary_currency",
    "salary_period", "skills", "experience_range", "work_from_home_type",
)


@contextmanager
def transaction(conn, *, immediate=True, resource="discovery-state", shared=False):
    """Atomic operation, including explicit rollback on EVERY exception."""
    nested = conn.in_transaction
    savepoint = "discovery_" + uuid4().hex
    try:
        conn.execute(f"SAVEPOINT {savepoint}" if nested else
                     "BEGIN" if immediate else "BEGIN")
        if immediate:
            conn.advisory_lock(resource, shared=shared)
        yield
        if nested:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.commit()
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


def _connection(conn):
    if conn is None:
        from applypilot.database import get_connection
        return get_connection()
    return conn


def ensure_schema(conn):
    """Add discovery tables/projections to an initialized jobs DB; no commit."""
    caller_transaction = conn.in_transaction
    try:
        existing = {r[1] for r in conn.table_info('jobs')}
        if "url" not in existing:
            raise ValueError("ensure_schema requires the jobs table")
        for name, definition in JOB_COLUMNS.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        if "score_assessment_revision" not in existing:
            conn.execute("ALTER TABLE jobs ADD COLUMN score_assessment_revision TEXT")
        statements = (
            """CREATE TABLE IF NOT EXISTS discovery_contexts (
                context_key TEXT PRIMARY KEY, intent_fingerprint TEXT NOT NULL,
                profile_version TEXT NOT NULL, rules_version TEXT NOT NULL,
                intent_json TEXT NOT NULL, applicant_json TEXT NOT NULL,
                runtime_json TEXT NOT NULL, cycle_id TEXT NOT NULL,
                prepare_share TEXT NOT NULL, apply_share TEXT NOT NULL,
                allow_off_portfolio_prepare INTEGER NOT NULL DEFAULT 0,
                allow_off_portfolio_apply INTEGER NOT NULL DEFAULT 0,
                reallocate_unused_to_preferred INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS discovery_active_context (
                singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                context_key TEXT NOT NULL REFERENCES discovery_contexts(context_key),
                activated_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS discovery_postings (
                identity_key TEXT PRIMARY KEY, provider TEXT NOT NULL,
                tenant TEXT NOT NULL, api_region TEXT NOT NULL, posting_id TEXT,
                listing_url TEXT, job_url TEXT NOT NULL REFERENCES jobs(url),
                identity_json TEXT NOT NULL, content_hash TEXT NOT NULL,
                normalized_json TEXT NOT NULL, origins_json TEXT NOT NULL,
                source_status TEXT NOT NULL, source_revision INTEGER NOT NULL,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                last_task_id TEXT, last_generation INTEGER,
                UNIQUE(provider,tenant,api_region,posting_id))""",
            """CREATE TABLE IF NOT EXISTS discovery_observations (
                observation_id TEXT PRIMARY KEY, identity_key TEXT NOT NULL
                    REFERENCES discovery_postings(identity_key),
                source_revision INTEGER NOT NULL, content_hash TEXT NOT NULL,
                raw_json TEXT NOT NULL, normalized_json TEXT NOT NULL,
                origins_json TEXT NOT NULL, source_status TEXT NOT NULL,
                cycle_id TEXT NOT NULL, task_id TEXT, page_key TEXT,
                first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS discovery_assessments (
                revision TEXT PRIMARY KEY, identity_key TEXT NOT NULL
                    REFERENCES discovery_postings(identity_key),
                job_url TEXT NOT NULL REFERENCES jobs(url),
                context_key TEXT NOT NULL REFERENCES discovery_contexts(context_key),
                content_hash TEXT NOT NULL, source_revision INTEGER NOT NULL,
                assessment_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS idx_discovery_postings_job ON discovery_postings(job_url)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_postings_current ON discovery_postings(job_url,source_status,content_hash,source_revision,last_seen_at)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_scope ON discovery_postings(provider,tenant,api_region,source_status)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_observations_identity ON discovery_observations(identity_key,source_revision)",
            "CREATE INDEX IF NOT EXISTS idx_discovery_assessments_job ON discovery_assessments(job_url,context_key,content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_discovery_prepare ON jobs(discovery_context_key,discovery_prepare_allowed,discovery_disposition,discovery_preference_rank,discovery_role_tier,fit_score,discovered_at,url)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_discovery_apply ON jobs(discovery_context_key,discovery_apply_allowed,discovery_disposition,discovery_preference_rank,discovery_role_tier,fit_score,discovered_at,url)",
        )
        for statement in statements:
            conn.execute(statement)
        payloads.ensure_schema(conn)
        posting_columns = {r[1] for r in conn.table_info('discovery_postings')}
        if "source_content_hash" not in posting_columns:
            conn.execute("ALTER TABLE discovery_postings ADD COLUMN source_content_hash TEXT")
        observation_columns = {r[1] for r in conn.table_info('discovery_observations')}
        if "inference" not in observation_columns:
            conn.execute("ALTER TABLE discovery_observations ADD COLUMN inference TEXT")
            conn.execute("UPDATE discovery_observations SET inference=ap_json_text(raw_json,'$.inference') "
                         "WHERE ap_json_text(raw_json,'$.inference') IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_retention ON discovery_observations(last_seen_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_assessment_retention ON discovery_assessments(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_observation_source ON discovery_observations(identity_key,inference,source_revision DESC,last_seen_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_assessment ON jobs(discovery_assessment_revision)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attempt_assessment ON application_attempts(assessment_revision)")
        watched = [c for c in CONTENT_COLUMNS if c in existing]
        changed = " OR ".join(f"OLD.{c} IS DISTINCT FROM NEW.{c}" for c in watched)
        conn.execute("""CREATE OR REPLACE FUNCTION ap_discovery_invalidate_content()
            RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
            BEGIN
                UPDATE jobs SET discovery_assessed_content_hash=NULL,
                    discovery_prepare_allowed=0, discovery_apply_allowed=0
                WHERE url=NEW.url;
                RETURN NULL;
            END $$""")
        conn.execute(f"""CREATE OR REPLACE TRIGGER discovery_invalidate_content
            AFTER UPDATE OF {','.join(watched)} ON jobs
            FOR EACH ROW WHEN ({changed})
            EXECUTE FUNCTION ap_discovery_invalidate_content()""")
    except BaseException:
        if not caller_transaction:
            conn.rollback()
        raise


def _plain(value, *, raw=False):
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {f.name: _plain(getattr(value, f.name), raw=raw) for f in fields(value)}
    if isinstance(value, Mapping):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON object keys must be strings")
        return {k: _plain(v, raw=raw) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v, raw=raw) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        if raw:
            return {"$provider_scalar": repr(value), "$type": "float"}
        raise ValueError("normalized facts must be finite JSON")
    if isinstance(value, str):
        return str(value)
    if value is None or type(value) in (int, float, bool):
        return value
    if raw:
        # Retain provider sentinels/dates without turning their strings into facts.
        return {"$provider_scalar": str(value), "$type": type(value).__module__ + "." + type(value).__name__}
    raise ValueError(f"not a normalized JSON value: {type(value).__name__}")


def _json(value, *, raw=False):
    return json.dumps(_plain(value, raw=raw), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def read_job_document(job, field, conn=None):
    """Read a current or legacy discovery projection as a decoded document."""
    value = job.get(field)
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return {}
    if payloads.reference_hash(value):
        value = payloads.resolve_json(_connection(conn), value)
    return json.loads(value)


def hydrate_job(conn, job):
    """Keep public job dictionaries compatible with their original JSON fields."""
    job = dict(job)
    for field in ("discovery_normalized_json", "discovery_assessment_json"):
        if job.get(field):
            job[field] = payloads.resolve_json(conn, job[field])
    return job


def _context(context):
    from .matching import RULES_VERSION

    intent = context.intent.fingerprint()
    profile = context.applicant.fingerprint()
    runtime = _plain(context.runtime)
    if not isinstance(runtime, dict):
        raise ValueError("context.runtime must be a mapping")
    policy = runtime.get("admission", {})
    if not isinstance(policy, dict):
        raise ValueError("runtime.admission must be a mapping")
    flags = []
    for name in ("allow_off_portfolio_prepare", "allow_off_portfolio_apply", "reallocate_unused_to_preferred"):
        value = policy.get(name, False)
        if type(value) is not bool:
            raise ValueError(f"runtime.admission.{name} must be boolean")
        flags.append(int(value))
    shares = []
    for name in ("discovery_share", "application_share"):
        value = getattr(context.intent.exploration, name)
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"invalid {name}")
        shares.append(str(Fraction(str(value))))
    if not all(isinstance(v, str) and v for v in (intent, profile, RULES_VERSION)):
        raise ValueError("context revisions must be nonempty strings")
    # Execution knobs (workers, retries, budgets, credentials) do not change an
    # opportunity assessment. Only admission policy participates in this key.
    # The full runtime remains persisted and is refreshed on each activation.
    key = _hash((intent, profile, RULES_VERSION, policy))
    return (key, intent, profile, RULES_VERSION, _json(context.intent),
            _json(context.applicant), _json(runtime), str(context.cycle_id),
            *shares, *flags)


def activate_context(context, conn=None, *, refresh_limit=None,
                     schema_initialized=False):
    """Persist and activate the global intent/profile/rules/policy revision.

    Returns context_key. Never activate implicitly from a returning worker:
    publication for a superseded context raises rather than switching it back.
    Callers that already ran init_db/ensure_schema skip repeated runtime DDL.
    """
    values = _context(context)
    conn = _connection(conn)
    with transaction(conn):
        conn.execute(
            "INSERT INTO discovery_contexts(context_key,intent_fingerprint,profile_version,rules_version,"
            "intent_json,applicant_json,runtime_json,cycle_id,prepare_share,apply_share,"
            "allow_off_portfolio_prepare,allow_off_portfolio_apply,reallocate_unused_to_preferred,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(context_key) DO UPDATE SET "
            "runtime_json=excluded.runtime_json,cycle_id=excluded.cycle_id,"
            "prepare_share=excluded.prepare_share,apply_share=excluded.apply_share,"
            "allow_off_portfolio_prepare=excluded.allow_off_portfolio_prepare,"
            "allow_off_portfolio_apply=excluded.allow_off_portfolio_apply,"
            "reallocate_unused_to_preferred=excluded.reallocate_unused_to_preferred",
            (*values, _now()),
        )
        conn.execute(
            "INSERT INTO discovery_active_context(singleton_id,context_key,activated_at) VALUES(1,?,?) "
            "ON CONFLICT(singleton_id) DO UPDATE SET context_key=excluded.context_key,activated_at=excluded.activated_at",
            (values[0], _now()),
        )
    refresh_current_assessments(context, conn, limit=refresh_limit)
    return values[0]


def ensure_context(context, conn=None):
    """Ensure schema and an initial context; never reactivate a stale worker.

    Call activate_context explicitly to change intent/profile/policy. Returns
    the same context-key shape as activate_context.
    """
    conn = _connection(conn)
    key = _context(context)[0]
    # init_db/activate_context own additive migrations. Routine scoring and
    # enrichment must not repeat DDL or queue for the global writer lock.
    if conn.execute("SELECT to_regclass('discovery_active_context')").fetchone()[0] is not None:
        active = conn.execute("SELECT context_key FROM discovery_active_context WHERE singleton_id=1").fetchone()
        if active is not None:
            if active[0] != key:
                raise ValueError("discovery context is not active; activate at cycle start")
            return key
    needs_activation = False
    with transaction(conn):
        if conn.execute("SELECT 1 FROM discovery_active_context WHERE singleton_id=1").fetchone() is None:
            needs_activation = True
        else:
            _assert_context(conn, key)
    if needs_activation:
        return activate_context(context, conn, schema_initialized=True)
    return key


def refresh_current_assessments(context, conn=None, limit=None):
    """Reassess persisted observations after activation or content invalidation.

    No provider/profile IO. None means all stale postings; an explicit zero is
    no work. Rows without raw observations remain unknown. Lifecycle, artifacts
    and immutable job URLs are never reset. Returns an IngestionResult.
    """
    from applypilot.limits import work_limit
    if limit is not None:
        work_limit(limit, "refresh limit")
    if limit == -1:
        limit = None
    conn = _connection(conn)
    key = _context(context)[0]
    total = IngestionResult()
    _assert_context(conn, key)
    query = ("SELECT j.url FROM jobs j WHERE EXISTS ("
             "SELECT 1 FROM discovery_postings dp WHERE dp.job_url=j.url "
             "AND dp.source_status='open' AND (j.discovery_context_key IS DISTINCT FROM ? "
             "OR j.discovery_assessed_content_hash IS NULL "
             "OR j.discovery_content_hash IS DISTINCT FROM dp.content_hash "
             "OR j.discovery_source_revision IS DISTINCT FROM dp.source_revision)) "
             "ORDER BY j.fit_score DESC NULLS LAST, (j.discovery_role_status='relevant') DESC, "
             "j.discovered_at DESC, j.url")
    params = [key]
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    for row in conn.execute(query, params).fetchall():
        total = total.merge(reassess_job(context, conn, row[0]))
    return total


def reassess_job(context, conn, job_url, *, source_status=None, role_evaluation=None, source_verified=False,
                 expected_assessment_revision=None):
    """Recompile the same persisted provider facts with enriched text/target.

    The caller can include its content UPDATE in this transaction. Never
    reconstruct evidence from a lossy legacy jobs row or a model score.
    """
    if source_status is not None and source_status not in {"open", "closed", "unknown"}:
        raise ValueError("invalid source status")
    with transaction(conn, immediate=False):
        _assert_context(conn, _context(context)[0])
        cursor = conn.execute(
            "SELECT dp.*, j.title AS enriched_title, j.company AS enriched_company, "
            "j.full_description AS enriched_description, j.application_url AS enriched_target, "
            "j.discovery_assessment_revision AS expected_projection "
            "FROM discovery_postings dp JOIN jobs j ON j.url=dp.job_url WHERE j.url=?",
            (job_url,),
        )
        row = cursor.fetchone()
        if row is None:
            if not source_verified:
                return IngestionResult()
            legacy = conn.execute("SELECT * FROM jobs WHERE url=?", (job_url,)).fetchone()
            if legacy is None:
                return IngestionResult()
            legacy = dict(legacy)
            if not legacy.get("full_description") or not _specific_listing(legacy.get("application_url") or job_url):
                return IngestionResult()
            # A successful source read can establish a new observation for a
            # legacy row. Stored legacy prose alone never refreshes freshness.
            record = {k: legacy.get(k) for k in ("title", "company", "full_description", "location", "application_url")}
            record.update(url=job_url, source_status=source_status or "open")
            return publish_records([record], legacy.get("site") or "legacy", context=context, conn=conn,
                                   _role_evaluation=role_evaluation)
        posting = dict(zip((d[0] for d in cursor.description), row))
        if expected_assessment_revision is not None and posting["expected_projection"] != expected_assessment_revision:
            return IngestionResult()
        observation = conn.execute(
            "SELECT raw_json FROM discovery_observations WHERE identity_key=? "
            "AND inference IS NULL "
            "ORDER BY source_revision DESC,last_seen_at DESC,observation_id DESC LIMIT 1",
            (posting["identity_key"],),
        ).fetchone()
        if observation is None:
            return IngestionResult()
        record = json.loads(payloads.resolve_json(conn, observation[0]))
        from .models import WorkArrangement

        normalized = json.loads(payloads.resolve_json(conn, posting["normalized_json"]))
        defaults = _plain(WorkArrangement())
        arrangements = normalized.get("work_arrangements", [])
        # Freeze existing scoped facts before replacing prose. Re-normalizing
        # just the new description could erase a supported legal blocker.
        record["work_arrangements"] = [
            {name: value for name, value in arrangement.items()
             if name in {"id", "evidence_ids"} or value != defaults.get(name)}
            for arrangement in arrangements
        ]
        references = {eid for arrangement in arrangements for eid in arrangement.get("evidence_ids", [])}
        supplied = record.get("evidence", record.get("facts", []))
        if isinstance(supplied, list):
            references.update(f.get("id") for f in supplied if isinstance(f, dict))
        record["evidence"] = [f for f in normalized.get("facts", [])
                              if f.get("id") in references or f.get("kind") == "normalization_issue"]
        record["inference"] = "local_reassessment"
        record.update(title=posting["enriched_title"], company=posting["enriched_company"],
                      full_description=posting["enriched_description"],
                      application_url=posting["enriched_target"],
                      canonical_target=posting["enriched_target"],
                      source_status=source_status if source_status is not None else posting["source_status"])
        return publish_records([record], posting["provider"], context=context, conn=conn,
                               tenant=posting["tenant"] or None, api_region=posting["api_region"] or None,
                               origins=json.loads(posting["origins_json"]), _preserve_content=True,
                               _role_evaluation=role_evaluation, _source_verified=source_verified,
                               _expected_assessment=(job_url, posting["expected_projection"]))


def apply_role_evaluation(context, conn, job_url, expected_assessment_revision, role):
    """Attach a model role judgment to the exact source/profile/policy revision."""
    if role not in {"relevant", "irrelevant"} or context is None:
        return None
    with transaction(conn, immediate=False):
        row = conn.execute("SELECT discovery_assessment_revision FROM jobs WHERE url=?", (job_url,)).fetchone()
        if row is None or row[0] != expected_assessment_revision:
            return None
        result = reassess_job(context, conn, job_url, role_evaluation=role,
                              expected_assessment_revision=expected_assessment_revision)
        if not result.valid:
            return None
        return conn.execute("SELECT discovery_assessment_revision FROM jobs WHERE url=?", (job_url,)).fetchone()[0]


def _specific_listing(url):
    """Conservative identity fallback, never email/homepage/generic careers."""
    if not isinstance(url, str):
        return None
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    query = dict(parse_qsl(parsed.query))
    listing_query = any(query.get(k) for k in ("gh_jid", "job_id", "jobId", "jobid", "jk", "requisitionId"))
    markers = {"job", "jobs", "careers", "career", "positions", "posting", "postings", "requisition", "vacancies"}
    generic = markers | {"search", "all", "open", "openings", "apply", "index", "index.html", "index.php"}
    marked = any(p.lower() in markers for p in parts[:-1])
    specific_tail = bool(parts and parts[-1].lower() not in generic and
                         (re.search(r"\d", parts[-1]) or len(parts[-1].split("-")) >= 3))
    ats = parsed.hostname.lower() in {"jobs.lever.co", "jobs.eu.lever.co", "jobs.ashbyhq.com"}
    if not (listing_query or (specific_tail and (marked or ats))):
        return None
    # Keep all query parameters: over-deduplication is worse than extra sightings.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _identity(candidate, provider, tenant, api_region):
    identity = candidate.identity
    if identity is None or identity.provider != provider:
        raise ValueError("candidate must have the requested strict provider identity")
    if tenant is not None and identity.tenant != tenant:
        raise ValueError("candidate tenant differs from publication scope")
    if api_region is not None and identity.api_region != api_region:
        raise ValueError("candidate API region differs from publication scope")
    scope = (identity.provider, identity.tenant or "", identity.api_region or "")
    posting_id = identity.posting_id
    if posting_id is not None and (not isinstance(posting_id, str) or not posting_id.strip()):
        raise ValueError("posting_id must be a nonempty string")
    listing = _specific_listing(identity.listing_url)
    if posting_id is None and listing is None:
        raise ValueError("posting needs a strict source ID or a specific listing URL")
    key = _hash((*scope, "posting_id" if posting_id is not None else "listing_url", posting_id or listing))
    # ID-only signals are stored without inventing a live application URL.
    return key, scope, posting_id, listing, listing or "discovery:" + key




def _assert_context(conn, key):
    if conn.in_transaction:
        conn.advisory_lock("discovery-state", shared=True)
    row = conn.execute("SELECT context_key FROM discovery_active_context WHERE singleton_id=1").fetchone()
    if row is None or row[0] != key:
        raise ValueError("discovery context is not active; activate at cycle start")














def _publication_locks(conn, compiled, task, page):
    scopes = {item["scope"] for item in compiled}
    for scope in sorted(scopes):
        conn.advisory_lock("discovery-scope:" + _json(scope), shared=True)
    identities = {item["identity_key"] for item in compiled}
    urls = {url for item in compiled for url in (item["job_url"], "discovery:" + item["identity_key"])}
    for identity in sorted(identities):
        conn.advisory_lock("discovery-posting:" + identity)
    for url in sorted(urls):
        conn.advisory_lock("discovery-job:" + url)
    if urls:
        conn.execute("SELECT url FROM jobs WHERE url=ANY(?) ORDER BY url FOR UPDATE", (sorted(urls),)).fetchall()


def lock_job_publication(conn, job_url):
    """Acquire publication fences before a caller changes the job's content."""
    if not conn.in_transaction:
        raise ValueError("job publication locking requires a transaction")
    postings = conn.execute(
        "SELECT identity_key,provider,tenant,api_region FROM discovery_postings WHERE job_url=?",
        (job_url,)).fetchall()
    if not postings:
        conn.advisory_lock("discovery-state")
        conn.execute("SELECT url FROM jobs WHERE url=? FOR UPDATE", (job_url,)).fetchone()
        return
    conn.advisory_lock("discovery-state", shared=True)
    _publication_locks(conn, [{"identity_key": row[0], "scope": tuple(row)[1:], "job_url": job_url}
                              for row in postings], None, None)


def publish_records(records, provider, *, context, tenant=None, api_region=None,
                    origins=(), conn=None,
                    _preserve_content=False, _role_evaluation=None, _source_verified=False,
                    _expected_assessment=None) -> IngestionResult:
    """Normalize dict records, compile assessments, atomically publish a page.

    Invalid records fail the entire page (no silently successful partial crawl).
    lease and page must be supplied together. After success, task_checkpoint
    supplies the next cursor-fenced lease. Same-page replay returns the original
    result without resetting lifecycle or advancing the cursor twice.
    """
    from .matching import assess_candidate
    from .normalization import normalize_record, scalar_bool, scalar_text

    values = _context(context)
    key = values[0]
    records = tuple(records)
    origins = tuple(origins)
    conn = _connection(conn)
    # Schema/context setup belongs to the cycle entry point, not every record
    # in an enrichment refresh. Legacy initial publication may bootstrap once.
    if conn.execute("SELECT 1 FROM discovery_active_context WHERE singleton_id=1").fetchone() is None:
        ensure_context(context, conn)
    else:
        _assert_context(conn, key)
    compiled = []
    # Previous origins are provenance, not facts. Optimistic source-revision
    # checks under the lock prevent racing publications from losing provenance.
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("publish_records consumes dictionary records")
        candidate = normalize_record(record, provider, tenant=tenant, api_region=api_region)
        identity_key, scope, posting_id, listing, job_url = _identity(candidate, provider, tenant, api_region)
        old = conn.execute("SELECT p.source_revision,p.origins_json,p.source_content_hash,p.normalized_json,p.job_url,"
                           "j.discovery_assessment_revision FROM discovery_postings p JOIN jobs j ON j.url=p.job_url "
                           "WHERE p.identity_key=?", (identity_key,)).fetchone()
        all_origins = tuple(dict.fromkeys((*json.loads(old[1]), *origins))) if old else origins
        source_content_hash = candidate.content_hash()
        preserve_projection = False
        if old and not _preserve_content and old[2] == source_content_hash:
            # An unchanged provider snapshot cannot overwrite a richer local
            # extraction or throw away a current model role judgment.
            candidate = payloads.candidate_from_json(payloads.resolve_json(conn, old[3]))
            preserve_projection = True
        if old:
            job_url = old[4]
        content_hash = candidate.content_hash()
        role_evaluation = _role_evaluation
        saved_role = conn.execute("SELECT discovery_role_evaluation_json FROM jobs WHERE url=?", (job_url,)).fetchone()
        previous_role = json.loads(saved_role[0]) if saved_role and saved_role[0] else {}
        if role_evaluation is None and previous_role.get("content_hash") == content_hash and previous_role.get("context_key") == key:
            role_evaluation = previous_role.get("role")
        assessment = assess_candidate(candidate, context.intent, context.applicant, origins=all_origins,
                                      admission_policy=context.runtime.get("admission", {}),
                                      role_evaluation=role_evaluation)
        if (assessment.content_hash != content_hash or assessment.intent_fingerprint != values[1]
                or assessment.profile_version != values[2] or assessment.rules_version != values[3]):
            raise ValueError("assessment revisions do not match compiled candidate/context")
        preference = assessment.preference
        if preference is None:
            raise ValueError("assessment must include an explicit preference decision")
        status = preference.status
        if status not in {"ranked", "explore", "off_portfolio", "needs_evidence", "blocked"}:
            raise ValueError("invalid preference disposition")
        eligible = [a for a in assessment.arrangement_results
                    if a.role == "relevant" and a.eligibility == "eligible"
                    and a.evidence_for("role") and a.evidence_for("eligibility")]
        selected = next((a for a in eligible if a.id == preference.arrangement_id), None)
        if selected is None and preference.arrangement_id is None and eligible and status in {"explore", "off_portfolio"}:
            selected = min(eligible, key=lambda a: a.id)
        rank = preference.priority
        if status == "ranked" and (type(rank) is not int or rank < 1 or not preference.preference_id or selected is None):
            raise ValueError("ranked decision needs rank, preference and eligible selected arrangement")
        source_status = scalar_text(record.get("source_status", record.get("status")))
        source_status = source_status.lower() if source_status else "open"
        source_status = {"active": "open", "published": "open", "expired": "closed", "inactive": "closed",
                         "filled": "closed", "deleted": "closed", "withdrawn": "closed"}.get(source_status, source_status)
        if scalar_bool(record.get("is_active")) is False:
            source_status = "closed"
        if source_status not in {"open", "closed"}:
            source_status = "unknown"
        role = assessment.role.value if isinstance(assessment.role, Enum) else assessment.role
        gate = selected is not None and role == "relevant" and source_status == "open"
        live = context.runtime.get("rollout_mode") == "live"
        prepare = live and gate and assessment.prepare_allowed is True
        apply = live and gate and assessment.apply_allowed is True
        if status == "off_portfolio":
            fill = context.runtime.get("admission", {}).get("fill_idle_capacity") is True
            prepare = prepare and (values[10] == 1 or fill)
            apply = apply and (values[11] == 1 or fill)
        elif status not in {"ranked", "explore"}:
            prepare = apply = False
        accepted_queries = {reason.removeprefix("accepted query:")
                            for reason in assessment.role_reasons if reason.startswith("accepted query:")}
        tiers = [q.tier for q in context.intent.active_queries() if q.effective_id() in accepted_queries]
        compiled.append(dict(
            identity_key=identity_key, scope=scope, posting_id=posting_id, listing=listing,
            job_url=job_url, candidate=candidate, content_hash=content_hash,
            normalized_json=_json(candidate), raw_json=_json(record, raw=True),
            identity_json=_json(candidate.identity), origins_json=_json(all_origins),
            assessment_json=_json(assessment), assessment=assessment, status=status,
            source_status=source_status, selected=selected.id if selected else None,
            prepare=int(prepare), apply=int(apply), role_tier=min(tiers) if tiers else None,
            previous_revision=old[0] if old else None,
            previous_assessment=old[5] if old else None,
            previous_origins=old[1] if old else None,
            raw_payload=payloads.prepare(_json(payloads.source_record(record), raw=True)),
            normalized_payload=payloads.prepare(_json(candidate)),
            assessment_payload=payloads.prepare(_json(assessment)),
            inference=record.get("inference"),
            role_evaluation_json=_json({"role": role_evaluation, "content_hash": content_hash, "context_key": key}) if role_evaluation else None,
            source_content_hash=old[2] if _preserve_content and old else source_content_hash,
            preserve_projection=preserve_projection,
        ))
    counts = dict(fetched=len(records), valid=len(compiled), new=0, updated=0, strict_duplicates=0,
                  compatible=0, unresolved=0, off_portfolio=0, blocked=0, admitted=0, checkpoint_advanced=False)
    with transaction(conn, shared=True):
        _assert_context(conn, key)
        now, page_key = _now(), None
        _publication_locks(conn, compiled, None, None)
        if _expected_assessment is not None:
            expected_url, expected_revision = _expected_assessment
            current = conn.execute("SELECT discovery_assessment_revision FROM jobs WHERE url=?",
                                   (expected_url,)).fetchone()
            if current is None or current[0] != expected_revision:
                return IngestionResult()
            if _role_evaluation is not None and conn.execute(
                    "SELECT 1 FROM jobs j WHERE j.url=? AND ("
                    "(j.apply_status IS NOT NULL AND j.apply_status!='failed') "
                    "OR EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_url=j.url "
                    "AND a.status IN ('prepared','side_effect_intent','verification_required',"
                    "'verification_quarantined','submitted')))", (expected_url,)).fetchone():
                return IngestionResult()
        prepared = {value.digest: value for item in compiled
                    for value in (item["raw_payload"], item["normalized_payload"], item["assessment_payload"])}
        saved_payloads = {digest: payloads.save(conn, prepared[digest], now) for digest in sorted(prepared)}
        published, observation_links, published_projections = {}, {}, {}
        for item in compiled:
            identity_key = item["identity_key"]
            old_cursor = conn.execute("SELECT * FROM discovery_postings WHERE identity_key=?", (identity_key,))
            old_row = old_cursor.fetchone()
            old = dict(zip((d[0] for d in old_cursor.description), old_row)) if old_row else None
            expected = published.get(identity_key, item["previous_revision"])
            if (old["source_revision"] if old else None) != expected:
                raise ValueError("posting changed while compiling assessment; retry publication")
            if old:
                expected_assessment, expected_origins = published_projections.get(
                    identity_key, (item["previous_assessment"], item["previous_origins"]))
                current_assessment = conn.execute("SELECT discovery_assessment_revision FROM jobs WHERE url=?",
                                                  (old["job_url"],)).fetchone()[0]
                if current_assessment != expected_assessment or old["origins_json"] != expected_origins:
                    raise ValueError("posting projection changed while compiling assessment; retry publication")
            job_url = old["job_url"] if old else item["job_url"]
            if not old:
                collision = conn.execute("SELECT identity_key FROM discovery_postings WHERE job_url=?", (job_url,)).fetchone()
                if collision and collision[0] != identity_key:
                    # Distinct tenant/region/posting IDs must not merge at a reused
                    # listing URL. Keep the real route in normalized facts.
                    job_url = "discovery:" + identity_key
            changed = old is None or any(old[name] != item[name] for name in ("content_hash", "source_status"))
            revision = (old["source_revision"] + int(changed)) if old else 1
            published[identity_key] = revision
            counts["new" if old is None else "updated" if changed else "strict_duplicates"] += 1
            candidate, assessment = item["candidate"], item["assessment"]
            locations = tuple(dict.fromkeys(loc for a in candidate.work_arrangements for loc in a.office_locations))
            salary = "; ".join(c.original_text for c in candidate.compensation if c.original_text) or None
            conn.execute("INSERT INTO jobs(url,discovered_at) VALUES(?,?) ON CONFLICT(url) DO NOTHING", (job_url, now))
            # This fires the enrichment invalidation trigger. Publish the new
            # projection only AFTER updating all normalized content/route fields.
            if not _preserve_content and not item["preserve_projection"]:
                conn.execute(
                    "UPDATE jobs SET title=COALESCE(?,title),company=COALESCE(?,company),"
                    "description=COALESCE(?,description),full_description=COALESCE(?,full_description),"
                    "location=COALESCE(?,location),salary=COALESCE(?,salary),"
                    "application_url=COALESCE(?,application_url),site=?,posted_at=COALESCE(?,posted_at) WHERE url=?",
                    (candidate.title, candidate.company, candidate.description, candidate.description,
                     ", ".join(locations) or None, salary, candidate.application_url,
                     provider, candidate.source_posted_at, job_url),
                )
            observed_at = old["last_seen_at"] if _preserve_content and old and not _source_verified else now
            observed_task = old["last_task_id"] if _preserve_content and old else None
            observed_generation = old["last_generation"] if _preserve_content and old else None
            normalized_ref = saved_payloads[item["normalized_payload"].digest]
            assessment_ref = saved_payloads[item["assessment_payload"].digest]
            raw_ref = saved_payloads[item["raw_payload"].digest]
            conn.execute(
                "INSERT INTO discovery_postings(identity_key,provider,tenant,api_region,posting_id,listing_url,"
                "job_url,identity_json,content_hash,normalized_json,origins_json,source_status,source_revision,"
                "first_seen_at,last_seen_at,last_task_id,last_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(identity_key) DO UPDATE SET listing_url=excluded.listing_url,identity_json=excluded.identity_json,"
                "content_hash=excluded.content_hash,normalized_json=excluded.normalized_json,origins_json=excluded.origins_json,"
                "source_status=excluded.source_status,source_revision=excluded.source_revision,last_seen_at=excluded.last_seen_at,"
                "last_task_id=excluded.last_task_id,last_generation=excluded.last_generation",
                (identity_key, *item["scope"], item["posting_id"], item["listing"], job_url,
                 item["identity_json"], item["content_hash"], normalized_ref, item["origins_json"],
                 item["source_status"], revision, now, observed_at, observed_task, observed_generation),
            )
            conn.execute("UPDATE discovery_postings SET source_content_hash=? WHERE identity_key=?",
                         (item["source_content_hash"], identity_key))
            observation_id = _hash((identity_key, revision, item["content_hash"], item["inference"]))
            observation_links[identity_key] = observation_id
            conn.execute(
                "INSERT INTO discovery_observations(observation_id,identity_key,source_revision,content_hash,raw_json,"
                "normalized_json,origins_json,source_status,cycle_id,task_id,page_key,first_seen_at,last_seen_at,inference) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(observation_id) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at,origins_json=excluded.origins_json,"
                "raw_json=excluded.raw_json,normalized_json=excluded.normalized_json",
                (observation_id, identity_key, revision, item["content_hash"], raw_ref, normalized_ref,
                 item["origins_json"], item["source_status"], str(context.cycle_id), None, page_key, now, now, item["inference"]),
            )
            assessment_revision = _hash((identity_key, key, revision, item["content_hash"], item["assessment_json"]))
            published_projections[identity_key] = (assessment_revision, item["origins_json"])
            conn.execute(
                "INSERT INTO discovery_assessments(revision,identity_key,job_url,context_key,content_hash,source_revision,assessment_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(revision) DO NOTHING",
                (assessment_revision, identity_key, job_url, key, item["content_hash"], revision, assessment_ref, now),
            )
            role = assessment.role.value if isinstance(assessment.role, Enum) else assessment.role
            projection = (key, assessment_revision, item["content_hash"], item["content_hash"], *values[1:4],
                          role, item["role_tier"], "eligible" if item["selected"] else "unknown", item["status"],
                          assessment.preference.preference_id, assessment.preference.priority, item["selected"],
                          item["prepare"], item["apply"], item["source_status"], revision,
                          normalized_ref, assessment_ref, now, _json(assessment.assumptions), item["role_evaluation_json"])
            conn.execute("UPDATE jobs SET " + ",".join(f"{col}=?" for col in JOB_COLUMNS) + " WHERE url=?", (*projection, job_url))
            for owner, slot, value in (("posting:"+identity_key, "normalized", normalized_ref),
                                      ("observation:"+observation_id, "raw", raw_ref),
                                      ("observation:"+observation_id, "normalized", normalized_ref),
                                      ("assessment:"+assessment_revision, "assessment", assessment_ref),
                                      ("job:"+job_url, "normalized", normalized_ref),
                                      ("job:"+job_url, "assessment", assessment_ref)):
                payloads.reference(conn, owner, slot, value)
            counts["compatible"] += int(item["selected"] is not None)
            counts["unresolved"] += int(item["status"] == "needs_evidence")
            counts["off_portfolio"] += int(item["status"] == "off_portfolio")
            counts["blocked"] += int(item["status"] == "blocked")
            counts["admitted"] += item["prepare"]
            from applypilot.targets.intent import link
            link(conn, candidate, context.intent, origins, job_url)
        return IngestionResult(**counts)

"""Revision-aware application selection. Listing never reserves allocation."""

from __future__ import annotations

from applypilot import postgres
from datetime import UTC, datetime, timedelta

from applypilot import config
from applypilot.database import get_connection
from applypilot.discovery.admission import current_admission_sql, select_jobs
from applypilot.scoring.tailor import resolve_company_key


ACTIVE_ATTEMPTS = (
    "'prepared','side_effect_intent','verification_required',"
    "'verification_quarantined','submitted'"
)


def company_policy_json() -> str:
    import json
    configured = config._load_company_limits()
    default_cap, default_window = config.get_company_limit('')
    overrides = {}
    for company in configured.get('overrides', {}):
        cap, window = config.get_company_limit(company)
        overrides[company.strip().lower()] = {'cap': cap, 'window': window}
    return json.dumps({'default_cap': default_cap, 'default_window': default_window, 'overrides': overrides})


def company_capacity(conn):
    policy = company_policy_json()
    def under_cap(company, site, strategy, target, job_url):
        return bool(conn.execute('SELECT ap_company_under_cap(?,?,?,?,?,?)',
                    (company, site, strategy, target, job_url, policy)).fetchone()[0])
    return under_cap


def queue_checks(min_score, max_score, max_age_days, route_kind, *, conn=None):
    """Business eligibility, evidence freshness, consent and duplicate protection."""
    min_score = config.DEFAULTS["min_score"] if min_score is None else min_score
    max_age_days = config.DEFAULTS["max_job_age_days"] if max_age_days is None else max_age_days
    checks = [
        ("company_capacity", "ap_company_under_cap(j.company,j.site,j.strategy,r.target,j.url,?)", (company_policy_json(),)),
        ("current_discovery_evidence", current_admission_sql(stage="apply"), ()),
        ("score_eligibility", "j.eligibility='eligible'", ()),
        ("application_status", "(j.apply_status IS NULL OR j.apply_status='failed')", ()),
        ("protected_lifecycle", "COALESCE(j.state,'') NOT IN ('applying','applied','verification_required',"
         "'verification_quarantined','archived','manual_only')", ()),
        ("minimum_score", "j.fit_score>=?", (min_score,)),
        ("score_evidence", "j.scored_at IS NOT NULL", ()),
        ("job_identity", "j.url IS NOT NULL AND j.url NOT IN ('','None')", ()),
        ("supported_route", "r.route IN ('external','linkedin-easy-apply','email')", ()),
        ("route_target", "r.target IS NOT NULL AND r.target!=''", ()),
        ("protected_attempt", "NOT EXISTS (SELECT 1 FROM application_attempts aa WHERE "
         "(aa.job_url=j.url OR (r.route!='email' AND aa.channel!='email' AND aa.target=r.target)) "
         f"AND aa.status IN ({ACTIVE_ATTEMPTS}))", ()),
        ("already_applied", "j.applied_at IS NULL", ()),
        ("duplicate_target", "(r.route='email' OR (NOT EXISTS (SELECT 1 FROM application_routes r2 "
         "JOIN jobs j2 ON j2.url=r2.job_url WHERE r2.target=r.target AND j2.url!=j.url "
         "AND (j2.applied_at IS NOT NULL OR j2.state IN ('applying','applied','verification_required','verification_quarantined'))) "
         "AND NOT EXISTS (SELECT 1 FROM jobs j2 WHERE j2.application_url=r.target AND j2.url!=j.url "
         "AND (j2.applied_at IS NOT NULL OR j2.state IN ('applying','applied','verification_required','verification_quarantined')))))", ()),
        ("document_approval", "(COALESCE(j.tailor_repair_pending,0)=0 OR "
         "NULLIF(j.application_contract_json,'')::jsonb #>> '{documents,resume,status}'='prohibited') AND "
         "(COALESCE(j.cover_repair_pending,0)=0 OR COALESCE(NULLIF(j.application_contract_json,'')::jsonb "
         "#>> '{documents,cover_letter,status}','optional')!='required')", ()),
    ]
    if max_score is not None:
        checks.append(("maximum_score", "j.fit_score<=?", (max_score,)))
    if max_age_days and max_age_days > 0:
        from applypilot.discovery.admission import last_seen_sql
        checks.append(("source_age", "ap_day(" + last_seen_sql("j") + ")>ap_day('now',?)", (f"-{max_age_days} days",)))
    if route_kind:
        checks.append(("route_kind", "r.route=?", (route_kind,)))
    sites, patterns = config.load_blocked_sites()
    checks.extend(("blocked_source", "COALESCE(j.site,'')!=?", (site,)) for site in sites)
    for pattern in patterns:
        checks.extend((("blocked_job_url", "j.url NOT LIKE ?", (pattern,)),
                       ("blocked_route_url", "r.target NOT LIKE ?", (pattern,))))
    from applypilot.integrations.research import posting_suitability_sql
    checks.append(("company_posting_suitability", posting_suitability_sql("j"), ()))
    from applypilot.referrals.store import hold_sql
    checks.append(("referral_hold", hold_sql("j"), ()))
    return checks


def _queue_predicate(min_score, max_score, max_age_days, route_kind, *, conn=None):
    checks = queue_checks(min_score, max_score, max_age_days, route_kind, conn=conn)
    return [sql for _, sql, _ in checks], [p for _, _, params in checks for p in params]






def delivery_blocker(conn: postgres.Connection, doc_format: str = "pdf", *, check_suitability: bool = True):
    """Check business readiness, not whether any worker is idle or a timer is due."""
    from applypilot.apply.guard import (application_conflict_snapshot, find_application_conflict,
        submission_identity, upload_artifact_paths)
    from applypilot.apply.routes import application_route_blocker
    conflicts = application_conflict_snapshot(conn)
    def reason(row):
        job = dict(row)
        from applypilot.apply.application_contract import missing_requirements, selected_documents
        if missing_requirements(job, for_dispatch=True):
            return "application_requirements"
        if route_reason := application_route_blocker(job, check_suitability=check_suitability):
            return route_reason
        if find_application_conflict(conn, job_url=job["url"], target=job["application_url"],
                                     identity=submission_identity(job), snapshot=conflicts):
            return "submission_identity_conflict"
        try:
            documents = selected_documents(job)
            upload_artifact_paths(documents["resume"], documents["cover_letter"], doc_format, require_resume=False)
        except (OSError, ValueError):
            return "document_files"
        return None
    return reason


def list_actionable_jobs(conn: postgres.Connection, *, min_score=None, max_score=None, max_age_days=None,
                         route_kind=None, limit: int | None = None, target_url: str | None = None,
                         doc_format: str = "pdf", after_url: str = "", job_url: str | None = None) -> list[dict]:
    """Read candidates without claiming them. A page size never limits total work."""
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a nonnegative page size or None")
    if limit == 0:
        return []
    from applypilot.apply.guard import artifact_snapshot, attempt_snapshot
    from applypilot.discovery.repository import hydrate_job
    clauses, params = _queue_predicate(min_score, max_score, max_age_days, route_kind, conn=conn)
    if job_url is not None:
        clauses.append("j.url=?")
        params.append(job_url)
    if target_url is not None:
        clauses.append("(j.url=? OR r.target=?)")
        params.extend((target_url, target_url))
    clauses.append("j.url>?")
    params.append(after_url)
    sql = ("SELECT r.target AS application_url,j.*,r.route AS application_route,"
           "r.evidence AS route_evidence,r.version AS route_version FROM jobs j "
           "JOIN application_routes r ON r.job_url=j.url WHERE " + " AND ".join(clauses) + " ORDER BY j.url")
    # Filter the entire ordered business set, not a first-N truncated candidate
    # set. Dispatch uses its own keyset cursor and independent child workflows.
    blocker = delivery_blocker(conn, doc_format)
    jobs = []
    for row in conn.execute(sql, params):
        if blocker(row) is not None:
            continue
        job = hydrate_job(conn, row)
        try:
            from applypilot.apply.application_contract import selected_documents
            documents = selected_documents(job)
            job["artifact_snapshot"] = artifact_snapshot(documents["resume"], documents["cover_letter"], doc_format,
                                                         require_resume=False, job_url=job["url"])
        except (OSError, ValueError):
            continue  # Immutable approval disappeared; preparation must repair it.
        from applypilot.referrals.store import for_application
        job["networking"] = for_application(conn, job["url"])
        job["_attempt_snapshot"] = attempt_snapshot(job)
        job["_queue_policy"] = dict(min_score=min_score, max_score=max_score,
                                    max_age_days=max_age_days, route_kind=route_kind)
        job["_doc_format"] = doc_format
        from applypilot.tracking.feedback_learning import decision
        job["_feedback_choice"] = decision(conn, job)
        jobs.append(job)
        if limit is not None and len(jobs) >= limit:
            break
    return jobs


def count_actionable_jobs(*, min_score=None, max_score=None, max_age_days=None, route_kind=None) -> int:
    from applypilot.database import close_connection
    try:
        return len(list_actionable_jobs(get_connection(), min_score=min_score, max_score=max_score,
                                       max_age_days=max_age_days, route_kind=route_kind))
    finally:
        close_connection()

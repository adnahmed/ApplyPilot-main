"""Finite job business operations; no stage runner, allocation or retry worker.

Each invocation reads one posting, performs one semantic operation, and publishes
against its input revision. PostgreSQL transactions protect business evidence,
not worker ownership. Transient errors leave history and propagate to Temporal.
"""
from __future__ import annotations

from datetime import datetime, timezone
from .contracts import Observation

STAGES = {"enrich": "pending_detail", "score": "pending_score",
          "tailor": "pending_tailor", "cover": "pending_cover"}


def query_for(conn, phase, cfg):
    from applypilot.database import _jobs_by_stage, preparation_stock_sql, interview_brief_pending_sql
    from applypilot.discovery.admission import current_admission_sql
    from applypilot.integrations.research import posting_suitability_sql
    from applypilot.work_context import stage_scope
    if phase in STAGES:
        # query_only returns before allocation, metrics, hydration or selection.
        with stage_scope(temporal_owned=True, job_urls=None, reserved_urls=()):
            query, params, _ = _jobs_by_stage(conn, STAGES[phase], query_only=True,
                min_score=cfg["min_score"], max_age_days=cfg["max_age_days"])
        return query, params
    if phase == "company_research":
        return ("SELECT j.* FROM jobs j WHERE " + preparation_stock_sql("j")
                + " AND " + current_admission_sql("j", "score")
                + " AND NULLIF(TRIM(j.company),'') IS NOT NULL AND j.full_description IS NOT NULL"
                + " AND NOT " + posting_suitability_sql("j", require_suitable=False), [])
    if phase == "brief":
        return ("SELECT j.* FROM jobs j WHERE COALESCE(j.state,'')!='archived' AND "
                + interview_brief_pending_sql("j"), [])
    if phase == "pdf":
        # File inspection/rendering belongs to each document child, not a batch
        # converter hidden in the dispatcher. Existing immutable renders are reused.
        return ("SELECT j.* FROM jobs j WHERE " + preparation_stock_sql("j")
                + " AND (NULLIF(j.tailored_resume_path,'') IS NOT NULL OR "
                  "NULLIF(j.cover_letter_path,'') IS NOT NULL)", [])
    if phase == "apply":
        from applypilot.apply.queue import _queue_predicate
        clauses, params = _queue_predicate(cfg["min_score"], cfg.get("max_score"), cfg["max_age_days"], None)
        return ("SELECT j.* FROM jobs j LEFT JOIN application_routes r ON r.job_url=j.url WHERE "
                "(" + " AND ".join("(" + sql + ")" for sql in clauses) + ") OR "
                "(j.applied_at IS NULL AND EXISTS (SELECT 1 FROM application_attempts a "
                "WHERE a.job_url=j.url AND a.status IN ('prepared','side_effect_intent',"
                "'verification_required','verification_quarantined')))" , params)
    raise ValueError("Unknown core phase: " + phase)


def _selected(conn, phase, url, cfg):
    from applypilot.discovery.repository import hydrate_job
    query, params = query_for(conn, phase, cfg)
    row = conn.execute("SELECT j.* FROM (" + query + ") j WHERE j.url=?", (*params, url)).fetchone()
    return hydrate_job(conn, row) if row is not None else None


def _revision(job):
    return {key: job.get(key) for key in ("discovery_assessment_revision",
        "discovery_content_hash", "discovery_source_revision", "discovery_context_key",
        "fit_score", "scored_at", "detail_scraped_at", "application_url")}


def execute_job(phase, value, root, cfg):
    from applypilot import operations as ops
    from applypilot.database import get_connection, close_connection
    from applypilot.execution import check_cancelled
    from applypilot.work_context import stage_scope
    url = value["job_url"]
    try:
        job = _selected(get_connection(), phase, url, cfg)
    finally:
        close_connection()
    if job is None:
        return Observation("not_applicable")
    check_cancelled()
    with (ops.scope(job_title=job.get("title", ""), job_company=job.get("company", ""),
                    job_source=job.get("site", "")),
          stage_scope(temporal_owned=True, job_urls=(url,), stage=phase,
                      stop_event=cfg["stop_event"], defer_document_rendering=True)):
        try:
            if phase == "enrich":
                return _enrich(job, cfg)
            if phase == "score":
                return _score(job, root, cfg)
            if phase in {"tailor", "cover"}:
                return _document_source(phase, job, root, cfg)
            if phase == "brief":
                from applypilot.career_ops.brief_operation import generate_one
                return Observation(generate_one(url, value["job_id"]))
            raise ValueError("Unsupported job operation")
        finally:
            close_connection()


def _enrich(job, cfg):
    from applypilot.enrichment.detail import scrape_detail_page, _mark_enrich_result, resolve_url
    from applypilot.database import get_connection
    from applypilot.discovery.repository import transaction
    from applypilot.execution import check_cancelled
    url = (resolve_url(job["url"], job.get("site", ""))
           or resolve_url(job.get("application_url") or "", job.get("site", "")))
    result = (scrape_detail_page(None, url, application_url=job.get("application_url")) if url else
              {"status": "error", "error": "unresolved listing URL"})
    check_cancelled()
    conn = get_connection()
    with transaction(conn):
        current = conn.execute("SELECT j.* FROM jobs j WHERE j.url=? FOR UPDATE", (job["url"],)).fetchone()
        if current is None or _revision(dict(current)) != _revision(job) or current["applied_at"]:
            return Observation("superseded")
        from applypilot.database import preparation_stock_sql
        if not conn.execute("SELECT 1 FROM jobs j WHERE j.url=? AND " + preparation_stock_sql("j"),
                            (job["url"],)).fetchone():
            return Observation("not_applicable")
        _mark_enrich_result(conn, job["url"], status=result["status"],
            full_description=result.get("full_description"), application_url=result.get("application_url"),
            error=result.get("error") or result.get("reason_code"), tier=result.get("tier_used"), context=cfg["context"])
    if result.get("full_description"):
        return Observation("complete" if result.get("application_url") else "partial")
    if result.get("source_status") == "closed":
        return Observation("not_applicable")
    from applypilot.operations.runtime import sanitize
    reason = result.get("error") or result.get("reason_code") or "No job description was extracted"
    raise RuntimeError("Description extraction remains unresolved: " + sanitize(reason, 600))


def _score(job, root, cfg):
    from applypilot.scoring.scorer import score_job, persist_score
    from applypilot.database import get_connection
    from applypilot.discovery.repository import transaction
    from applypilot.execution import check_cancelled
    if not root.get("resume_text", "").strip():
        raise ValueError("A resume snapshot is required for scoring")
    result = score_job(root["resume_text"], job, root["profile"],
        admission_policy=cfg["context"].runtime.get("admission", {}))
    result.update(url=job["url"], _source_snapshot=_revision(job))
    check_cancelled()
    with transaction(get_connection()):
        accepted = persist_score(get_connection(), result, datetime.now(timezone.utc).isoformat(), cfg["context"])
    if not accepted:
        return Observation("superseded")
    if result["score"] is None:
        raise RuntimeError("Scoring provider did not produce a valid score")
    return Observation("complete")


def _document_source(phase, job, root, cfg):
    from applypilot.database import get_connection
    from applypilot.discovery.repository import transaction
    from applypilot.preparation_guard import source_snapshot, result_is_current
    from applypilot.execution import check_cancelled
    from applypilot.apply.application_contract import ensure_contract
    contract = ensure_contract(get_connection(), job, profile=root.get("profile"))
    rule = contract["documents"]["cover_letter" if phase == "cover" else "resume"]["status"]
    if rule == "prohibited" or (phase == "cover" and rule != "required"):
        return Observation("not_applicable")
    if not root.get("resume_text", "").strip():
        raise ValueError("A resume snapshot is required for document preparation")
    snapshot = source_snapshot(job, phase)
    if phase == "tailor":
        from applypilot.scoring.tailor import _tailor_one_job, _mark_tailor_result
        result = _tailor_one_job(job, root["resume_text"], root["profile"], cfg["doc_format"])
        approved = result["status"] == "approved"
    else:
        from applypilot.scoring.cover_letter import _cover_one_job, _mark_cover_result
        result = _cover_one_job(job, root["resume_text"], root["profile"], cfg["doc_format"])
        approved = bool(result.get("approved") and result.get("path"))
    result["_source_snapshot"] = snapshot
    check_cancelled()
    conn = get_connection()
    with transaction(conn):
        if not result_is_current(conn, result):
            return Observation("superseded")
        diagnostics = {key: result[key] for key in ("validation_report", "validation_errors", "error_origin",
            "validation_mode", "factual_review") if key in result}
        if phase == "tailor":
            _mark_tailor_result(conn, job["url"], result["status"], result.get("path") if approved else None,
                attempts=result.get("attempts"), document_variant=result.get("document_variant"), diagnostics=diagnostics)
            if approved and job.get("cover_letter_path"):
                conn.execute("UPDATE jobs SET cover_repair_pending=1 WHERE url=?", (job["url"],))
        else:
            _mark_cover_result(conn, job["url"], result.get("path") if approved else None,
                error=result.get("error"), diagnostics=diagnostics)
    if not approved:
        raise RuntimeError("Generated document did not pass its existing approval contract")
    return Observation("complete")


def maintain_business_evidence(cfg, cursor):
    """One bounded evidence page, not recovery dispatch or a task/credit engine."""
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import refresh_current_assessments
    from applypilot.discovery.payloads import maintain_history
    from applypilot.document_recovery import queue_document_repairs
    try:
        conn = get_connection()
        refreshed = refresh_current_assessments(cfg["context"], conn, limit=128)
        result = queue_document_repairs(conn, min_score=cfg["min_score"], max_age_days=cfg["max_age_days"],
            doc_format=cfg["doc_format"], after_url=cursor or "")
        maintain_history(conn)
        conn.commit()
        return Observation("complete", {"reassessed": refreshed.updated + refreshed.new, "repairs": result["queued"]},
                           result["next_cursor"] or None)
    finally:
        close_connection()

"""Single-job semantic operations. No selection, worker dispatch or retries.

Each method releases its read connection before browser/model work. A result is
published only while its source and document evidence are still current.
"""
from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path


def load_job(url: str) -> dict | None:
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import hydrate_job
    try:
        conn = get_connection()
        row = conn.execute("SELECT * FROM jobs WHERE url=?", (url,)).fetchone()
        return hydrate_job(conn, row) if row is not None else None
    finally:
        close_connection()


def _eligible(job: dict | None) -> bool:
    return bool(job and job.get("state") != "archived" and not job.get("applied_at"))


def enrich(url: str, root: dict) -> dict:
    from applypilot.enrichment.detail import (scrape_detail_page, resolve_url,
        _mark_enrich_result, classify_detail_error)
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import transaction
    from applypilot.orchestration.store import context_from
    job = load_job(url)
    if not _eligible(job):
        return {"state": "not_applicable"}
    if job.get("full_description") and job.get("application_url"):
        return {"state": "enriched"}
    target = resolve_url(url, job.get("site")) or job.get("application_url")
    if not target:
        raise ValueError("Job has no resolvable listing URL")
    result = scrape_detail_page(None, target, application_url=job.get("application_url"))
    try:
        conn = get_connection()
        with transaction(conn, resource="job-evidence:" + url):
            if result.get("structured_data"):
                from applypilot.enrichment.facts import store_document
                store_document(conn, url, {"url": result.get("source_url") or target,
                    "structured_data": result["structured_data"]})
            _mark_enrich_result(conn, url, status=result["status"],
                full_description=result.get("full_description"), application_url=result.get("application_url"),
                error=result.get("error") or result.get("message"), tier=result.get("tier_used"),
                context=context_from(root))
    finally:
        close_connection()
    if result["status"] == "error":
        kind = classify_detail_error(result.get("error") or "unknown")
        if kind in {"expired", "permanent"}:
            return {"state": "closed" if kind == "expired" else "invalid_listing"}
        raise RuntimeError("Job enrichment did not produce usable evidence")
    return {"state": "enriched" if result["status"] == "ok" else "partial_evidence"}


def score(url: str, root: dict) -> dict:
    from applypilot.scoring.scorer import score_job, _flush_score_batch
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import transaction
    from applypilot.orchestration.store import context_from
    job = load_job(url)
    if not _eligible(job):
        return {"state": "not_applicable"}
    if not job.get("full_description"):
        raise ValueError("Scoring requires a job description")
    context = context_from(root)
    if job.get("fit_score") is not None and job.get("score_assessment_revision") == job.get("discovery_assessment_revision"):
        return {"state": "scored"}
    from applypilot.integrations.research import research_settings
    result = score_job(root.get("resume_text", ""), job, profile=root["profile"],
                       admission_policy=context.runtime.get("admission", {}),
                       research_policy=research_settings(context.runtime))
    result["url"] = url
    result["_source_snapshot"] = {key: value for key, value in job.items()
        if (key.startswith("discovery_") and key not in {
            "discovery_normalized_json", "discovery_assessment_json", "discovery_assumptions_json", "discovery_published_at"})
        or key in {"title", "company", "location", "full_description", "application_url", "apply_status",
                   "state", "applied_at", "score_assessment_revision"}}
    try:
        conn = get_connection()
        with transaction(conn, resource="score:" + url):
            _flush_score_batch(conn, [result], datetime.now(timezone.utc).isoformat(), context)
    finally:
        close_connection()
    if result.get("score") is None:
        if result.get("provider_reason") == "provider_auth":
            raise ValueError("Scoring provider authentication requires configuration")
        raise RuntimeError("Scoring did not produce a valid score")
    current = load_job(url)
    return {"state": "scored" if current and current.get("fit_score") == result["score"] else "superseded"}


def generate_document(url: str, root: dict, stage: str) -> dict:
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import transaction
    from applypilot.preparation_guard import source_snapshot, result_is_current
    from applypilot.work_context import stage_scope
    from applypilot.scoring.artifact_store import approved_source
    job = load_job(url)
    if not _eligible(job):
        return {"state": "not_applicable"}
    if stage not in {"tailor", "cover"}:
        raise ValueError("Unknown document stage")
    column = "tailored_resume_path" if stage == "tailor" else "cover_letter_path"
    existing = job.get(column)
    if existing:
        try:
            if approved_source(Path(existing).with_suffix(".txt")):
                return {"state": "prepared"}
        finally:
            close_connection()
    resume = root.get("resume_text", "")
    if not resume.strip():
        raise ValueError("Resume source is required")
    if stage == "tailor":
        from applypilot.scoring.tailor import _tailor_one_job as generate, _mark_tailor_result as publish
    else:
        from applypilot.scoring.cover_letter import _cover_one_job as generate, _mark_cover_result as publish
    with stage_scope(defer_document_rendering=True):
        result = generate(job, resume, root["profile"], root["options"].get("doc_format", "pdf"))
    result["_source_snapshot"] = source_snapshot(job, stage)
    approved = result.get("status") == "approved" if stage == "tailor" else bool(result.get("approved"))
    try:
        conn = get_connection()
        with transaction(conn, resource="document-source:" + url):
            if not result_is_current(conn, result):
                return {"state": "superseded"}
            if stage == "tailor":
                publish(conn, url, result["status"], result.get("path") if approved else None,
                        attempts=result.get("attempts"), document_variant=result.get("document_variant"),
                        diagnostics={key: result[key] for key in ("validation_errors", "validation_mode", "factual_review") if key in result})
            else:
                publish(conn, url, result.get("path") if approved else None, error=result.get("error"))
    finally:
        close_connection()
    if not approved:
        raise RuntimeError("Generated document did not satisfy its publication contract")
    return {"state": "prepared"}


def tailor(url: str, root: dict) -> dict:
    return generate_document(url, root, "tailor")


def cover(url: str, root: dict) -> dict:
    return generate_document(url, root, "cover")


def research_company(url: str, root: dict) -> dict:
    from applypilot.integrations.research import research_job
    from applypilot.orchestration.store import context_from
    from applypilot.database import close_connection
    job = load_job(url)
    if not _eligible(job):
        return {"state": "not_applicable"}
    try:
        result = research_job(url, discovery_context=context_from(root))
    finally:
        close_connection()
    if result.get("status") in {"error", "failed"}:
        raise RuntimeError("Company research failed")
    return {"state": str(result.get("status") or "researched")}

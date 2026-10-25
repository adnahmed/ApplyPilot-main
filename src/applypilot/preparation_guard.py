"""Keep document publication from racing active applications or artifact owners."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_SOURCE_FIELDS = (
    "discovery_assessment_revision", "discovery_content_hash", "discovery_context_key",
    "score_assessment_revision", "eligibility", "fit_score", "scored_at", "score_error",
)
_ARTIFACT_FIELDS = {
    "tailor": ("tailored_resume_path",),
    "cover": ("tailored_resume_path", "cover_letter_path"),
    "brief": ("interview_brief_path",),
}


def source_snapshot(job, stage):
    """Capture stored scalar identities from the job supplied to generation."""
    return {field: job.get(field) for field in (*_SOURCE_FIELDS, *_ARTIFACT_FIELDS[stage])}


def artifact_snapshot_matches(job, snapshot):
    """Only artifact ownership changes invalidate work; rescoring is diagnostic."""
    if not isinstance(snapshot, dict):
        return False
    fields = {field for values in _ARTIFACT_FIELDS.values() for field in values}
    return all(job.get(field) == snapshot[field] for field in fields if field in snapshot)


def result_is_current(conn, result):
    """Check admission and ownership while holding the result's write transaction."""
    from applypilot.discovery.admission import current_admission_sql

    if not conn.in_transaction:
        conn.execute("BEGIN")
    row = conn.execute(
        f"SELECT j.* FROM jobs j WHERE j.url=? AND {current_admission_sql('j', 'prepare')} "
        "AND j.applied_at IS NULL "
        "AND (j.apply_status IS NULL OR j.apply_status='failed') "
        "AND COALESCE(j.state,'') NOT IN ('applying','applied',"
        "'verification_required','verification_quarantined','archived') "
        "AND NOT EXISTS (SELECT 1 FROM application_attempts aa WHERE aa.job_url=j.url "
        "AND aa.status IN ('prepared','side_effect_intent','verification_required',"
        "'verification_quarantined','submitted')) FOR UPDATE OF j", (result["url"],),
    ).fetchone()
    current = dict(row) if row is not None else None
    snapshot = result.get("_source_snapshot")
    accepted = bool(current is not None and snapshot and artifact_snapshot_matches(current, snapshot))
    if accepted:
        from applypilot.career.documents import revoked_document_sources
        revoked = revoked_document_sources(result.get("_document_context"))
        if revoked:
            log.info("Discarding document using withdrawn facts for %s: %s", result["url"], ", ".join(revoked))
            accepted = False
    result["_discarded"] = not accepted
    if not accepted:
        log.info("Discarding changed or application-owned document result for %s", result["url"])
    return accepted

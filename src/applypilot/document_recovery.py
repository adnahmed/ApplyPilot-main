"""Recover application documents that have drifted away from recorded DB paths.

Application selection must fail closed when a resume/cover path no longer resolves to
an uploadable artifact. This module bridges that failure back into the ordinary
preparation pipeline: approved text is left for the converter, while artifacts that
must be regenerated are marked for guarded repair.
"""
from __future__ import annotations

from pathlib import Path
from applypilot import postgres

REPAIR_MAX_ATTEMPTS = 3  # retained for compatibility and historical reporting only
SCAN_LIMIT = 64


def queue_contract_repairs(conn: postgres.Connection, *, min_score: int, max_age_days: int | None = None,
                          limit: int = 2) -> int:
    """Retain the legacy entry point without scheduling semantic repairs.

    Existing rejected artifacts lack current approval hashes and remain history.
    Ordinary eligible jobs are selected by the document queues without an attempt
    cap, so contract-version promotion is no longer needed.
    """
    if type(limit) is not int or not 1 <= limit <= 64:
        raise ValueError("Contract recovery limit must be between 1 and 64")
    return 0


def _has_upload(value: str | None, doc_format: str, conn=None) -> bool:
    if not value:
        return False
    from applypilot.apply.guard import upload_artifact_paths
    from applypilot.scoring.artifact_store import approved_source
    try:
        upload_artifact_paths(value, None, doc_format)
        return approved_source(Path(value).with_suffix(".txt"), conn)
    except (OSError, ValueError):
        return False


def queue_document_repairs(
    conn: postgres.Connection,
    *,
    min_score: int,
    max_age_days: int | None = None,
    doc_format: str = "pdf",
    after_url: str = "",
    limit: int = SCAN_LIMIT,
    job_url: str | None = None,
) -> dict:
    """Queue broken application artifacts through the normal guarded generators.

    Only jobs that still pass the production application/admission predicates are
    considered. A missing rendered file is *not* regenerated when an approved text
    source is still current; the normal PDF/DOCX converter owns that case.
    """
    if doc_format not in {"pdf", "docx"}:
        raise ValueError("Unsupported document format")
    if type(limit) is not int or not 1 <= limit <= 256:
        raise ValueError("Document scan limit must be between 1 and 256")

    from applypilot import config
    from applypilot.apply.queue import queue_checks
    from applypilot.discovery.admission import current_admission_sql
    from applypilot.discovery.repository import transaction
    from applypilot.preparation_guard import _SOURCE_FIELDS
    from applypilot.scoring.artifact_store import approved_source

    # The repair flag itself is intentionally omitted: it is the state this scan
    # creates. Every other application predicate remains authoritative.
    checks = [item for item in queue_checks(min_score, None, max_age_days, None)
              if item[0] != "document_approval"]
    clauses = [sql for _, sql, _ in checks]
    params = [p for _, _, values in checks for p in values]
    clauses.append(current_admission_sql("j", "prepare"))
    if job_url is not None:
        clauses.append("j.url=?")
        params.append(job_url)
    predicate = " AND ".join(f"({sql})" for sql in clauses)
    base_query = (
        "SELECT j.* FROM jobs j JOIN application_routes r ON r.job_url=j.url "
        f"WHERE {predicate}"
    )
    rows = conn.execute(
        base_query + " AND j.url>? ORDER BY j.url LIMIT ?",
        (*params, after_url, limit),
    ).fetchall()

    queued = 0
    for row in rows:
        job = dict(row)
        updates: dict[str, int] = {}
        for stage, column, directory in (
            ("tailor", "tailored_resume_path", config.TAILORED_DIR),
            ("cover", "cover_letter_path", config.COVER_LETTER_DIR),
        ):
            value = job.get(column)
            # An absent resume is already ordinary tailoring work. An absent
            # cover is valid because applications may proceed without one.
            if not value or job.get(f"{stage}_repair_pending") or _has_upload(value, doc_format, conn):
                continue

            source = Path(value).with_suffix(".txt")
            output = source.with_suffix(f".{doc_format}")
            try:
                convertible = (
                    source.parent.resolve() == Path(directory).resolve()
                    and not output.exists()
                    and approved_source(source, conn)
                )
            except OSError:
                convertible = False
            if convertible:
                continue

            updates[f"{stage}_repair_pending"] = 1
            updates[f"{stage}_repair_attempts"] = 0
            if stage == "tailor" and job.get("cover_letter_path"):
                # Cover approval is bound to the previous resume snapshot.
                updates["cover_repair_pending"] = 1
                updates["cover_repair_attempts"] = 0

        if not updates:
            continue

        # Filesystem checks happen outside the write transaction. Revalidate the
        # source identity and all selection guards immediately before mutation.
        fields = (
            *_SOURCE_FIELDS,
            "tailored_resume_path",
            "cover_letter_path",
            "tailor_repair_pending",
            "cover_repair_pending",
            "tailor_repair_attempts",
            "cover_repair_attempts",
        )
        with transaction(conn):
            current = conn.execute(base_query + " AND j.url=? FOR UPDATE OF j", (*params, job["url"])).fetchone()
            if current is None or any(current[k] != job.get(k) for k in fields):
                continue

            # A converter may have completed after the first filesystem check.
            if "tailor_repair_pending" in updates and _has_upload(current["tailored_resume_path"], doc_format, conn):
                updates.pop("tailor_repair_pending", None)
                updates.pop("tailor_repair_attempts", None)
                # Do not clear a separately discovered cover repair here.
                if _has_upload(current["cover_letter_path"], doc_format, conn):
                    updates.pop("cover_repair_pending", None)
                    updates.pop("cover_repair_attempts", None)
            if "cover_repair_pending" in updates and _has_upload(current["cover_letter_path"], doc_format, conn):
                updates.pop("cover_repair_pending", None)
                updates.pop("cover_repair_attempts", None)
            if not updates:
                continue

            assignments = ", ".join(f"{key}=?" for key in updates)
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE url=?",
                (*updates.values(), job["url"]),
            )
            queued += 1

    return {
        "examined": len(rows),
        "queued": queued,
        "next_cursor": rows[-1]["url"] if len(rows) == limit else "",
    }


def inspect_job_documents(job_url, *, min_score, max_age_days=None, doc_format="pdf"):
    from applypilot.database import get_connection, close_connection
    try:
        result = queue_document_repairs(get_connection(), min_score=min_score,
            max_age_days=max_age_days, doc_format=doc_format, job_url=job_url, limit=1)
        get_connection().commit()
        return result
    finally:
        close_connection()

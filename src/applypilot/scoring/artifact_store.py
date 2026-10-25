"""Immutable, content-bound document approvals for autonomous conversion.

A manifest alone does not authorize use: its content hash and accepted database
pointer must match. Explicitly withdrawn facts cannot enter the converter queue.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


def atomic_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def generation_name(prefix: str) -> str:
    from applypilot.execution import current_task
    task = current_task()
    operation = (str(task["key"]) + ":" + str(task.get("attempt", 1))) if task.get("key") else None
    suffix = hashlib.sha256(operation.encode()).hexdigest()[:20] if operation else uuid.uuid4().hex
    return f"{prefix}_{suffix}"


def manifest_path(source: Path) -> Path:
    return Path(source).with_suffix(".approval.json")


def write_approval(source: Path, job: dict, stage: str, *, layout: dict | None = None,
                   document_context: dict | None = None) -> None:
    from applypilot.preparation_guard import source_snapshot
    if stage not in {"tailor", "cover"}:
        raise ValueError("Unsupported approved artifact stage")
    snapshot = source_snapshot(job, stage)
    snapshot.pop("tailored_resume_path" if stage == "tailor" else "cover_letter_path", None)
    manifest = {"version": 2, "status": "approved", "stage": stage, "job_url": job["url"],
                "source_snapshot": snapshot,
                "document_context": document_context,
                "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest()}
    if stage == "tailor" and layout:
        from applypilot.scoring.resume_blocks import plan_for
        manifest["resume_layout"] = plan_for({"layout": {key: layout[key] for key in ("version", "blocks", "style") if key in layout}})
    atomic_text(manifest_path(source), json.dumps(manifest, sort_keys=True, indent=2))


def approved_source(source: Path, conn=None, *, inventory=False) -> bool:
    """Require readable accepted content and ownership, not a metadata version."""
    from applypilot.database import get_connection
    from applypilot.discovery.admission import current_admission_sql
    source = Path(source)
    try:
        proof = json.loads(manifest_path(source).read_text(encoding="utf-8"))
        stage = proof["stage"]
        if proof.get("status") != "approved" or stage not in {"tailor", "cover"}:
            return False
        content = source.read_bytes()
        if not content.decode("utf-8-sig").strip() or hashlib.sha256(content).hexdigest() != proof.get("sha256"):
            return False
        column = "tailored_resume_path" if stage == "tailor" else "cover_letter_path"
        db = conn if conn is not None else get_connection()
        admission = 'TRUE' if inventory else current_admission_sql('j', 'prepare')
        row = db.execute(f"SELECT j.* FROM jobs j WHERE j.url=? AND {admission} "
                         "AND COALESCE(j.discovery_source_status,'unknown')!='closed' "
                         "AND j.applied_at IS NULL "
                         "AND (j.apply_status IS NULL OR j.apply_status='failed') "
                         "AND COALESCE(j.state,'') NOT IN ('applying','applied',"
                         "'verification_required','verification_quarantined','archived') "
                         "AND NOT EXISTS (SELECT 1 FROM application_attempts a WHERE a.job_url=j.url "
                         "AND a.status IN ('prepared','side_effect_intent','verification_required',"
                         "'verification_quarantined','submitted'))", (proof["job_url"],)).fetchone()
        if row is None:
            return False
        job = dict(row)
        if job.get(f"{stage}_repair_pending") or not job.get(column):
            return False
        if Path(job[column]).with_suffix(".txt").resolve() != source.resolve():
            return False
        from applypilot.preparation_guard import artifact_snapshot_matches
        if not artifact_snapshot_matches(job, proof.get("source_snapshot", {})):
            return False
        from applypilot.career.documents import revoked_document_sources
        return not revoked_document_sources(proof.get("document_context"))
    except (OSError, ValueError, TypeError, KeyError):
        return False

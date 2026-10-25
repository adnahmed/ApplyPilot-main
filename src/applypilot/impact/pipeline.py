"""Pipeline bridge: ledger + matcher + DB snapshot, in one hot-path call.

Tailor and cover-letter call ``claim_pack_for_job(job)``. First computation
for a job runs semantic evidence scoring, then persists a revision-bound
``SelectedEvidenceSnapshot`` to ``jobs.selected_evidence``. Subsequent stages
(and re-runs) reuse that snapshot while its inputs are current. Submitted
artifacts keep their historical snapshot.

An empty ledger degrades to the pre-ledger behavior (None pack) so callers
fall back to the legacy ``real_metrics`` path untouched.
"""

from __future__ import annotations

import hashlib
import json

from applypilot.impact.ledger import JsonImpactLedgerRepository, default_repository
from applypilot.impact.matcher import is_eligible, select_evidence
from applypilot.impact.models import (
    ClaimPack,
    SelectedEvidenceSnapshot,
    record_revision,
)

from applypilot import database


def _snapshot_is_stale(
    snapshot: SelectedEvidenceSnapshot,
    records_by_id: dict[str, object],
    *,
    job_revision: str = "",
    selection_input_revision: str = "",
) -> bool:
    """True if any referenced record was deleted, made ineligible, or edited.

    The snapshot freezes each record's ``ledger_revision``; the live ledger is
    the source of truth. Revision mismatch (any edit), deletion, or a status
    change that removes eligibility invalidates the stored ClaimPack so it is
    recomputed on the next run.
    """
    if not snapshot.job_revision or snapshot.job_revision != job_revision:
        return True
    if (not snapshot.selection_input_revision
            or snapshot.selection_input_revision != selection_input_revision):
        return True
    for item in snapshot.records:
        rid = str(item.get("id") or "")
        stored_rev = str(item.get("ledger_revision") or "")
        current = records_by_id.get(rid)
        if current is None:
            return True
        if not is_eligible(current):
            return True
        if record_revision(current) != stored_rev:
            return True
    return False


def _job_revision(job: dict) -> str:
    fields = {
        "title": job.get("title"),
        "company": job.get("company"),
        "location": job.get("location"),
        "description": job.get("full_description") or job.get("description"),
        "fit_requirements": job.get("fit_requirements"),
    }
    blob = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _selection_input_revision(records: list) -> str:
    rows = sorted((record.id, record_revision(record)) for record in records)
    blob = json.dumps({"selection_version": 2, "records": rows}, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _submitted(job: dict) -> bool:
    return bool(job.get("applied_at") or job.get("apply_status") == "applied"
                or job.get("state") in {"applied", "responded", "interview", "offer", "rejected"})


def claim_pack_for_job(
    job: dict,
    repo: JsonImpactLedgerRepository | None = None,
    refresh: bool = False,
    records: list | None = None,
) -> ClaimPack | None:
    """Return the job's claim pack, computing + persisting on first use.

    A stored snapshot is reused only while every record it references still
    exists, is eligible, and has an unchanged ``record_revision``. Any edit,
    delete, or rejection/revocation of an in-pack record invalidates the
    snapshot and the pack is recomputed (and the DB snapshot overwritten),
    so a rejected/edited record is revoked from previously generated jobs.

    Returns None when there are no eligible records. Persist an empty selection
    too, so removing all evidence cannot leave stale proof IDs on an unsent job.
    """
    url = str(job.get("url") or "")
    if records is None:
        records = (repo or default_repository()).list_records()
    records_by_id = {r.id: r for r in records}
    job_rev = _job_revision(job)
    input_rev = _selection_input_revision(records)
    if not refresh and url:
        stored = database.get_selected_evidence(url)
        if stored:
            snapshot = SelectedEvidenceSnapshot.from_dict(stored)
            # Submitted artifacts are historical and must never be rewritten by
            # a later ledger or job-description refresh.
            if _submitted(job) or not _snapshot_is_stale(
                snapshot,
                records_by_id,
                job_revision=job_rev,
                selection_input_revision=input_rev,
            ):
                pack = ClaimPack.from_dict(stored)
                return pack if pack.has_records else None
    eligible_records = [record for record in records if is_eligible(record)]
    # Empty and unapproved selections cost no semantic matching calls. A saved
    # empty result is valid only until the JD or ledger revision changes.
    pack = select_evidence(eligible_records, job) if eligible_records else ClaimPack()
    pack.job_revision = job_rev
    pack.selection_input_revision = input_rev
    if url:
        snapshot = SelectedEvidenceSnapshot.from_pack(pack)
        database.set_selected_evidence(url, snapshot.to_dict())
    return pack if pack.has_records else None

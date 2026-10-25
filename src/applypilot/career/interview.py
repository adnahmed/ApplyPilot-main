"""Recognition, uncertainty and rejection, adapted from myCareer interview protocols.

Local interview cards keep narrative above its compressed public formats.
Only an explicit owner's response can confirm a claim; no model self-attestation.
"""
from __future__ import annotations
from datetime import datetime, timezone
import copy
import hashlib
import re

from applypilot.vendor.mycareer.runtime import norm
from applypilot.impact.models import record_revision
from .projection import public_claim


def _words(value: str) -> set[str]:
    return {w for w in norm(value).split() if len(w) > 3}


def confirm_record(repository, record_id: str, response: dict, *, expected_revision: str):
    allowed = {"confirmed_detail", "narrative_long", "bullet", "agency", "constraint",
               "disclosure_approved", "resolved_question_ids"}
    if not isinstance(response, dict) or set(response) - allowed:
        raise ValueError("Unknown recognition response fields")
    for key in ("confirmed_detail", "narrative_long", "bullet"):
        if not isinstance(response.get(key), str) or len(response[key].strip()) < 12:
            raise ValueError(f"{key} must contain a concrete, owner-authored description")
    if response.get("agency") not in {"completed", "scoped", "enabled"}:
        raise ValueError("Agency must be completed, scoped, or enabled")
    if response.get("disclosure_approved") is not True:
        raise ValueError("Explicitly approve this public bullet's disclosure before confirming it")
    if any(len(response[key]) > 40_000 for key in ("confirmed_detail", "narrative_long", "bullet")):
        raise ValueError("Recognition response is too large")

    def mutate(record, context):
        if not record.career_detail:
            raise ValueError("This is not a myCareer record")
        detail = copy.deepcopy(record.career_detail)
        claim = detail["claim"]
        if record.review_status == "rejected" or claim.get("status") == "rejected":
            raise ValueError("A rejected claim is permanent and cannot be revived by confirmation")
        scaffold = " ".join(str(claim.get(key) or "") for key in ("text", "bullet", "narrative_short"))
        if len(_words(response["confirmed_detail"]) - _words(scaffold)) < 3:
            raise ValueError("Confirmation must add a particular the scaffold did not supply")
        new_text = norm(response["bullet"])
        for rejection in context.get("rejections", []):
            forbidden = norm(rejection.get("text"))
            if len(forbidden) > 20 and (forbidden in new_text or new_text in forbidden):
                raise ValueError("This restates a permanent do-not-claim entry")
        prior = {key: copy.deepcopy(claim.get(key)) for key in
                 ("text", "bullet", "narrative_long", "confirmed_detail", "agency", "verification", "status")}
        detail.setdefault("history", []).append({"at": datetime.now(timezone.utc).isoformat(),
                                                  "action": "owner_confirmed", "previous": prior})
        changed = any(response[key].strip() != str(claim.get(key) or "").strip()
                      for key in ("bullet", "agency", "narrative_long"))
        claim.update({key: response[key].strip() for key in ("confirmed_detail", "narrative_long", "bullet", "agency")})
        if "constraint" in response:
            claim["constraint"] = str(response["constraint"])
        claim["status"] = "confirmed"
        if changed or not claim.get("verification"):
            # New user recollection is T1. It cannot borrow prior corroboration
            # of differently worded accomplishments or raise itself to T3/T4.
            claim["verification"] = {"tier": "T1", "by_fact_type": {
                "accomplishment.outcome": "T1", "accomplishment.agency": "T1"}}
        detail["disclosure_approved"] = True
        questions = response.get("resolved_question_ids", [])
        known = {q["id"] for q in context.get("open_questions", [])}
        if not isinstance(questions, list) or any(q not in known for q in questions):
            raise ValueError("Resolved questions must reference existing question IDs")
        detail["resolved_question_ids"] = list(dict.fromkeys([*detail.get("resolved_question_ids", []), *questions]))
        record.career_detail = detail
        record.review_status = "reviewed"
        record.resume_eligible = True
        from .projection import refresh_public_fields
        refresh_public_fields(record)
        record.updated_at = datetime.now(timezone.utc).isoformat()
    return repository.update_career(record_id, expected_revision, mutate)


def reject_record(repository, record_id: str, *, reason: str, expected_revision: str):
    if not isinstance(reason, str) or len(reason.strip()) < 5:
        raise ValueError("Record the reason, not just a yes/no rejection")
    def mutate(record, context):
        if not record.career_detail:
            raise ValueError("This is not a myCareer record")
        claim = record.career_detail["claim"]
        now = datetime.now(timezone.utc).isoformat()
        claim["status"] = "rejected"
        record.review_status = "rejected"
        record.resume_eligible = False
        from .projection import refresh_public_fields
        refresh_public_fields(record)
        record.updated_at = now
        record.career_detail.setdefault("history", []).append({"at": now, "action": "owner_rejected", "reason": reason.strip()})
        rid = "rej.applypilot." + hashlib.sha256(record.id.encode()).hexdigest()[:16]
        rejections = context.setdefault("rejections", [])
        if not any(r["id"] == rid for r in rejections):
            rejections.append({"id": rid, "text": claim.get("text") or claim.get("bullet") or record.id,
                               "reason": "not_true", "detail": reason.strip()})
    return repository.update_career(record_id, expected_revision, mutate)


def interview_card(record, *, context: dict | None = None) -> dict:
    """Owner-only autobiography and story; callers must NOT send this to recruiters/models."""
    detail = record.career_detail or {}
    claim = detail.get("claim") or {}
    bundle = detail.get("linked_entities") or {}
    story = bundle.get("story") or {}
    questions = [
        "Which action was personally yours, and which decision belonged to someone else?",
        "What was the baseline and how was the result measured?",
        "Which alternative did you reject, and what did your decision cost?",
        "What went wrong, and who could reasonably describe your contribution differently?",
        "What source or person could close the remaining uncertainty?",
    ]
    for figure in bundle.get("figures", []):
        if not figure.get("instrument"):
            questions.append(f"How was {figure.get('kind') or figure['id']} measured? Name an instrument or record.")
    resolved = set(detail.get("resolved_question_ids", []))
    open_questions = [copy.deepcopy(q) for q in (context or {}).get("open_questions", [])
                      if q["id"] not in resolved and claim.get("id") in q.get("blocks_claim_ids", [])]
    return {"audience": "owner_only", "record_id": record.id, "revision": record_revision(record),
            "review_status": record.review_status, "public_claim": public_claim(record),
            "recognition_detail": claim.get("confirmed_detail", ""),
            "narrative_long": claim.get("narrative_long", ""),
            "agency": claim.get("agency"), "authority": claim.get("authority"),
            "story": {key: story.get(key, "") for key in ("title", "constraint", "before_state", "personal_action",
                     "decision", "result", "tradeoff", "what_went_wrong", "contradiction_risk")},
            "open_questions": open_questions, "questions": questions,
            "measurement_methods": [{key: figure.get(key) for key in ("id", "kind", "baseline", "window", "instrument")}
                                    for figure in bundle.get("figures", [])],
            "do_not_claim": copy.deepcopy((context or {}).get("rejections", []))}

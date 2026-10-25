"""Strict outbound allowlist adapted from myCareer scripts/recruiter_view.js.

Private autobiography, recognition detail, raw records, wages, attester identities,
contact references, uncertainty notes and disclosure instructions are NOT copied.
Free prose is still user-authored: review is necessary, not a proof of factual truth.
"""
from __future__ import annotations
import copy
import re
from typing import Any

from applypilot.vendor.mycareer.runtime import effective_tier, tier_rank, js_num
from applypilot.impact.models import record_revision

PUBLIC_FIGURE_FIELDS = ("id", "value", "unit", "kind", "baseline", "window", "instrument")
PUBLIC_FACT_TYPES = frozenset({"accomplishment.outcome", "accomplishment.agency", "accomplishment.attribution",
                               "scope.budget", "scope.team", "scope.system", "scope.scale",
                               "employment.employer", "employment.title", "employment.dates"})


def verification_view(value: dict | None) -> dict:
    value = value or {}
    per = {key: tier for key, tier in (value.get("by_fact_type") or {}).items()
           if key in PUBLIC_FACT_TYPES and tier in {"T0", "T1", "T2", "T3", "T4"}}
    # Never infer an accomplishment tier from the role's payroll metadata.
    fallback = "T0" if value.get("by_fact_type") and not per else value.get("tier", "T0")
    tier = effective_tier({"by_fact_type": per, "tier": fallback})
    return {"declared_tier": tier if tier_rank(tier) >= 0 else "T0", "by_fact_type": per,
            "external_documents_checked_here": False}


def _alias(value: Any, organization: dict) -> Any:
    if organization.get("nameable") is not False:
        return value
    original, alias = organization.get("name"), organization.get("public_name")
    if isinstance(value, str) and original and alias:
        return re.sub(re.escape(original), lambda _: alias, value, flags=re.I)
    if isinstance(value, list):
        return [_alias(x, organization) for x in value]
    if isinstance(value, dict):
        return {k: _alias(v, organization) for k, v in value.items()}
    return value


def public_claim(record) -> dict | None:
    detail = record.career_detail
    if not detail or record.review_status not in {"auto_approved", "reviewed"} or not record.resume_eligible:
        return None
    claim = detail.get("claim") or {}
    if claim.get("status") != "confirmed" or not detail.get("disclosure_approved", False):
        return None
    verification = verification_view(claim.get("verification"))
    # A payroll-only provenance map cannot make a career accomplishment eligible.
    if tier_rank(verification["declared_tier"]) < 1:
        return None
    bundle = detail.get("linked_entities") or {}
    figures = bundle.get("figures") or []
    # A bullet with an unsourceable linked quantity is not made safe by deleting
    # only its number. Require a separately reviewed qualitative claim instead.
    if any(not figure.get("sourceable") for figure in figures):
        return None
    org = bundle.get("org") or {}
    role = bundle.get("role") or {}
    if org.get("nameable") is False and not org.get("public_name"):
        return None
    bullet = claim.get("bullet") or claim.get("narrative_short") or claim.get("text")
    if not isinstance(bullet, str) or not bullet.strip() or "vault://" in bullet.casefold():
        return None
    # Top-level named fields only: never a spread of the source entity.
    view = {
        "id": record.id, "claim_id": claim.get("id"), "revision": record_revision(record),
        "role_id": claim.get("role_id"), "scope": record.scope.to_dict(),
        "text": bullet.strip(), "agency": claim.get("agency"), "authority": claim.get("authority"),
        "dates": {key: value for key, value in (role.get("dates") or claim.get("dates") or {}).items()
                  if key in {"start", "end", "ongoing", "approximate"}},
        "figures": [{**{key: copy.deepcopy(figure[key]) for key in PUBLIC_FIGURE_FIELDS if key in figure},
                     "verification": verification_view(figure.get("verification"))} for figure in figures],
        "regimes": [{"id": regime.get("id"), "name": regime.get("name"),
                     "accountability": regime.get("accountability"), "revision": regime.get("revision_known")}
                    for regime in bundle.get("regimes", []) if regime.get("status") == "confirmed"],
        "verification": verification_view(claim.get("verification")),
        "knowledge_currency": claim.get("knowledge_currency"),
    }
    return _alias(view, org)


def claim_snapshot(record) -> dict:
    public = public_claim(record)
    if public is None:
        return {"id": record.id, "scope": record.scope.to_dict(), "problem": "Evidence not approved for disclosure",
                "intervention": {"summary": "", "skills": []}, "measurements": [], "outcomes": [],
                "context": {}, "attribution": record.attribution, "resume_eligible": False}
    return {"id": record.id, "scope": public["scope"], "problem": public["text"],
            "intervention": {"summary": public["text"], "skills": []},
            "measurements": [], "outcomes": [], "context": {"career_public": public},
            "attribution": record.attribution, "recency": record.recency, "resume_eligible": True}


def recruiter_view(records, *, target: str | None = None, context: dict | None = None) -> dict:
    records = list(records)
    selected = None
    if target:
        from applypilot.vendor.mycareer.runtime import select_for_target
        corpus = dict(context or {})
        corpus["claims"] = [(r.career_detail or {}).get("claim", {}) for r in records if r.career_detail]
        picked, _ = select_for_target(corpus, target)
        selected = {claim["id"]: i for i, claim in enumerate(picked)}
    claims = []
    for record in records:
        source = (record.career_detail or {}).get("claim") or {}
        if selected is not None and source.get("id") not in selected:
            continue
        view = public_claim(record)
        if view is not None:
            claims.append(view)
    if selected is not None:
        claims.sort(key=lambda claim: selected[claim["claim_id"]])
    return {"format": "applypilot-mycareer-public/1", "target": target, "claims": claims,
            "note": "Owner-reviewed claims with fact-scoped declared provenance; not an independent verification or certification."}


def refresh_public_fields(record) -> None:
    """Keep legacy evidence readers on public text; full recognition stays private."""
    public = public_claim(record)
    text = public["text"] if public else "Evidence not approved for disclosure"
    record.problem = text
    record.intervention.summary = public["text"] if public else ""


def canonical_career_bullets(records) -> dict[str, dict]:
    result = {}
    for record in records:
        public = public_claim(record)
        if public is None:
            continue
        scope = public["scope"]
        section = "experience" if scope.get("type") == "employment" else "projects"
        header = " at ".join(filter(None, (scope.get("role"), scope.get("company")))) or scope.get("project") or "Career evidence"
        key = f"career:{public['claim_id']}@{public['revision']}"
        result[key] = {"text": public["text"], "section": section,
                       "scope_id": public["role_id"] or record.id, "header": header,
                       "subtitle": "", "dates": record.recency, "context": header,
                       "record_id": record.id, "record_revision": public["revision"],
                       "source_claim_id": public["claim_id"], "verification": public["verification"],
                       "metric_binding": [{key: figure.get(key) for key in ("id", "value", "unit", "kind", "baseline", "window")}
                                          for figure in public["figures"]]}
    return result

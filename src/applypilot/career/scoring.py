"""Bounded deterministic evidence preparation; scoring still belongs to ApplyPilot."""
from __future__ import annotations
import hashlib
import json
import re

from .matching import requirement_links, tokens
from .projection import recruiter_view


def compact_score_evidence(resume: str, job: dict, records=(), *, max_chars=10_000) -> dict:
    if max_chars < 1000:
        raise ValueError("Evidence budget is too small")
    records = list(records)
    view = recruiter_view(records)
    query = tokens(" ".join(str(job.get(k) or "") for k in ("title", "full_description", "description")))
    # Prefer complete source lines over a blind leading slice. Position is an
    # explicit tie breaker so a worker sees the same selection for the same input.
    lines = [(i, line.strip()) for i, line in enumerate(resume.splitlines(), 1) if line.strip()]
    ranked = sorted(lines, key=lambda row: (-len(tokens(row[1]) & query), row[0]))
    chosen, used = [], 0
    career = sorted(view["claims"], key=lambda c: (-len(tokens(c["text"]) & query), c["id"]))
    accepted = []
    for claim in career:
        encoded = json.dumps(claim, ensure_ascii=False, separators=(",", ":"))
        if used + len(encoded) > max_chars // 2:
            continue
        accepted.append(claim)
        used += len(encoded)
    for number, text in ranked:
        if used + len(text) + 40 > max_chars:
            continue
        chosen.append({"id": f"resume:L{number}", "text": text})
        used += len(text) + 40
    chosen.sort(key=lambda entry: int(entry["id"].split("L")[-1]))
    result = {"format": "applypilot-score-evidence/1", "resume_sha256": hashlib.sha256(resume.encode()).hexdigest(),
            "resume_lines": chosen, "career_claims": accepted,
            "selection": {"resume_lines_total": len(lines), "resume_lines_selected": len(chosen),
                          "career_claims_total": len(career), "career_claims_selected": len(accepted),
                          "complete": len(chosen) == len(lines) and len(accepted) == len(career)},
            "rule": "A bounded selection is not an exhaustive negative finding. Missing evidence is unknown, not proof of inexperience."}

    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) > max_chars:
        if chosen:
            # Remove the lowest-relevance selected line, retaining source order.
            remove = min(chosen, key=lambda item: (len(tokens(item["text"]) & query), -int(item["id"].split("L")[-1])))
            chosen.remove(remove)
        elif accepted:
            accepted.pop()
        else:
            raise ValueError("Evidence metadata exceeds the configured budget")
        result["selection"].update(resume_lines_selected=len(chosen), career_claims_selected=len(accepted), complete=False)
    return result


def attach_requirement_links(result: dict, records, *, context=None) -> dict:
    """Add provenance and contradictions without creating an upstream fit score."""
    view = recruiter_view(records)
    requirements = result.get("requirements") or []
    links = requirement_links([str(r.get("requirement") or "") for r in requirements], view, context=context)
    indexed = {link["requirement"]: link for link in links}
    known = {claim["id"]: claim for claim in view["claims"]}
    for requirement in requirements:
        link = indexed.get(str(requirement.get("requirement") or ""))
        if not link:
            continue
        requirement["career_match"] = link
        supplied = requirement.get("source_ids") or []
        if not isinstance(supplied, list) or any(not isinstance(sid, str) for sid in supplied):
            raise ValueError("Scoring source_ids must be a list of source identifiers")
        unknown = [sid for sid in supplied if sid.startswith("mycareer:") and sid not in known]
        if unknown:
            raise ValueError("Score cites unknown or non-disclosable career evidence")
        # Preserve independent resume evidence. A model may not obtain a high
        # scalar score solely by treating adjacency/missing detail as direct proof.
        career_only = bool(supplied) and all(sid.startswith("mycareer:") for sid in supplied)
        claimed = str(requirement.get("evidence_class") or "").upper()
        if career_only and link["status"] != "direct" and claimed in {"EXACT", "DIRECT"}:
            raise ValueError("Score treats transferable or unknown career evidence as direct proof")
        if career_only and link["status"] == "missing" and claimed == "NONE":
            raise ValueError("Missing career evidence is unknown, not an established contradiction")
        requirement["source_ids"] = supplied
    result["career_matching_version"] = 1
    return result

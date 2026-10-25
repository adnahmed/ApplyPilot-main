"""Explainable requirement links ported from myCareer scripts/match.js.

The upstream 'equivalent' label is intentionally 'adjacent'. Shared evidence
clusters are a reason to ask, never proof of a credential, jurisdictional
compliance, tenure, causal ownership or suitability. There is no scalar score.
"""
from __future__ import annotations
from functools import lru_cache
import json
import math
import re

from .corpus import ASSETS
from applypilot.vendor.mycareer.runtime import norm


@lru_cache(maxsize=1)
def adjacency() -> dict:
    return json.loads((ASSETS / "data/adjacency.json").read_text(encoding="utf-8"))


def tokens(text: str) -> set[str]:
    text = re.sub(r"\b(iso|soc)(?=\d)", r"\1 ", str(text), flags=re.I)
    return set(norm(text).split())


def similarity(a: str, b: str) -> float:
    aa, bb = tokens(a), tokens(b)
    if not aa or not bb or len(aa & bb) < min(len(aa), len(bb)):
        return 0.0
    return len(aa & bb) / max(len(aa), len(bb))


def mentions(requirement: str, name: str) -> float:
    required, named = tokens(requirement), tokens(name)
    shared = required & named
    if not named or not any(len(token) >= 4 or any(c.isdigit() for c in token) for token in shared):
        return 0.0
    # A conflicting standard number cannot match just because "ISO" is shared.
    nums_r = {t for t in required if t.isdigit() and len(t) >= 3}
    nums_n = {t for t in named if t.isdigit() and len(t) >= 3}
    if nums_r and nums_n and nums_r.isdisjoint(nums_n):
        return 0.0
    coverage = len(shared) / len(named)
    return coverage if coverage >= 0.5 else 0.0


def resolve_regime(requirement: str) -> dict | None:
    ranked = [(max(similarity(requirement, r["name"]), mentions(requirement, r["name"])), r)
              for r in adjacency()["regimes"]]
    score, regime = max(ranked, key=lambda item: item[0], default=(0, None))
    return regime if score > 0 else None


def _links(claims: list[dict]) -> list[dict]:
    return [{"claim_id": c["id"], "revision": c["revision"],
             "role_id": c.get("role_id"), "verification": c.get("verification", {})} for c in claims]


def match_requirement(requirement: str, view: dict, *, contradictions: list[dict] | None = None) -> dict:
    claims = view.get("claims", [])
    required = resolve_regime(requirement)
    target = required["name"] if required else requirement
    def direct_name(name):
        if not similarity(name, target):
            return False
        specificity = {"low", "moderate", "high"} & tokens(requirement)
        if specificity and not specificity <= tokens(name):
            return False
        return True
    found = [c for c in claims if any(direct_name(regime.get("name", ""))
                                    for regime in c.get("regimes", []))]
    result = {"requirement": requirement, "status": "missing", "claims": [],
              "contradictions": list(contradictions or []),
              "why": "No accepted claim on this record supports the requirement.",
              "question": "Ask for a concrete example or source. Missing evidence does not mean never done."}
    if found:
        return result | {"status": "direct", "claims": _links(found),
                         "why": "Accepted claims explicitly name this regime; this is work evidence, not a claim of personal certification.",
                         "question": "Confirm the required revision, role and credential separately where relevant."}
    if required:
        required_clusters = set(required.get("clusters", []))
        neighbours = sorted(((len(required_clusters & set(r.get("clusters", []))), r)
                             for r in adjacency()["regimes"] if r["id"] != required["id"]),
                            key=lambda pair: pair[0], reverse=True)
        for count, regime in neighbours:
            if count < 5:
                break
            found = [c for c in claims if any(similarity(r.get("name", ""), regime["name"])
                                            for r in c.get("regimes", []))]
            if found:
                return result | {"status": "adjacent", "claims": _links(found),
                                 "shared_clusters": sorted(required_clusters & set(regime.get("clusters", []))),
                                 "why": f"{regime['name']} shares evidence clusters with {target}; transferable work only, not equivalence.",
                                 "question": f"Did that evidence actually serve {target}? Confirm with a novel detail and its source."}
        # Do not fall through to a lexical 'evidenced' for an unheld named credential.
        return result
    words = {w for w in tokens(requirement) if len(w) > 3}
    ranked = sorted(((len(words & tokens(c.get("text", ""))), c) for c in claims),
                    key=lambda pair: pair[0], reverse=True)
    hits = [c for count, c in ranked if count >= max(1, math.ceil(len(words) / 2))]
    if hits:
        return result | {"status": "evidenced", "claims": _links(hits),
                         "why": "Lexical match against an accepted accomplishment, not a skills-list assertion; review its scope.",
                         "question": "Check ownership, metric meaning and time period against this requirement."}
    return result


def requirement_links(requirements: list[str], view: dict, *, context: dict | None = None) -> list[dict]:
    context = context or {}
    result = []
    for requirement in requirements[:80]:
        if not isinstance(requirement, str) or not requirement.strip():
            continue
        conflict = []
        for source in context.get("records", []):
            if source.get("conflicts"):
                # Source identities are local IDs, not raw payroll/attestation content.
                affected = set(source.get("covers_claim_ids", []))
                relevant = [c["id"] for c in view.get("claims", []) if c.get("claim_id") in affected
                            or c.get("role_id") in source.get("covers_role_ids", [])]
                if relevant:
                    conflict.append({"source_id": source["id"], "claim_ids": relevant, "kind": "source_conflict"})
        result.append(match_requirement(requirement.strip(), view, contradictions=conflict))
    return result


def recognition_candidates(*, data_classes=(), buyer_types=(), held=()) -> list[dict]:
    """Original deterministic Phase 2B retrieval, explicitly unconfirmed."""
    catalog = adjacency()
    candidates: set[str] = set()
    for category, values in (("data_classes", data_classes), ("buyer_types", buyer_types)):
        for value in values:
            candidates.update(catalog["trigger_conditions"].get(category, {}).get(str(value).strip().lower(), []))
    return [{"id": regime["id"], "name": regime["name"], "status": "candidate",
             "question": f"Did you do work for {regime['name']}? Describe one particular this question did not supply."}
            for regime in catalog["regimes"] if regime["id"] in candidates and regime["id"] not in set(held)]

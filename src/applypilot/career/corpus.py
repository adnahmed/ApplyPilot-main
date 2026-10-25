"""Validate and import myCareer material into the existing atomic impact ledger.

No database backfill, example seeding, provider call or inferred accomplishment.
The original schema/runtime are vendored with byte-level provenance.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

from applypilot.vendor.mycareer import runtime as upstream
from applypilot.impact.models import ImpactRecord, Scope, Intervention

ASSETS = Path(upstream.__file__).parent / "upstream"
MAX_CORPUS_BYTES = 16 * 1024 * 1024


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def validate_corpus(value: dict) -> tuple[dict, list[dict]]:
    """Schema plus original semantic checks; verification remains source metadata."""
    import jsonschema
    if not isinstance(value, dict):
        raise ValueError("myCareer corpus must be a JSON object")
    schema = json.loads((ASSETS / "schema/mycareer.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.validators.validator_for(schema)(schema)
    failures = sorted(validator.iter_errors(value), key=lambda error: str(error.json_path))
    if failures:
        # Do not print source values: they may contain restricted records.
        locations = ", ".join(f"{e.json_path} ({e.validator})" for e in failures[:20])
        raise ValueError("myCareer schema validation failed: " + locations)
    corpus = copy.deepcopy(value)
    for name in upstream.COLLECTIONS:
        corpus.setdefault(name, [])
    corpus.setdefault("person", {})
    report = upstream.Report()
    upstream.structural(corpus, report)
    # Upstream accepts a top-level T3/T4 without naming rendered fact types.
    # The adapter deliberately refuses that broad verification badge.
    for collection in ("claims", "figures", "roles", "regimes"):
        for entity in corpus[collection]:
            verification = entity.get("verification") or {}
            if upstream.tier_rank(verification.get("tier")) >= 3 and not verification.get("by_fact_type"):
                report.err("AP001", f"{entity['id']}: T3/T4 requires fact-scoped verification")
    if report.errors:
        raise ValueError("myCareer semantic validation failed: " + "; ".join(
            f"{code}: {message}" for code, message in report.errors[:20]))
    return corpus, [{"code": code, "message": text} for code, text in report.warnings]


def load_corpus(path: Path) -> dict:
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CORPUS_BYTES:
        raise ValueError("Provide a regular myCareer JSON file no larger than 16 MiB")
    try:
        return json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda _: (_ for _ in ()).throw(
            ValueError("Non-finite numbers are not valid evidence")))
    except UnicodeError as exc:
        raise ValueError("myCareer corpus must be UTF-8 JSON") from exc


def _dates(value: dict | None) -> str:
    value = value or {}
    return " – ".join(filter(None, (value.get("start"), "Present" if value.get("ongoing") else value.get("end"))))


def _private_bundle(claim: dict, index: dict) -> dict:
    """Reference snapshot for safe projection without reading another mutable file."""
    role = index.get(claim.get("role_id"), {})
    org = index.get(role.get("org_id"), {})
    story = index.get(claim.get("story_id"), {})
    figures = [index[x] for x in claim.get("figure_ids", []) if x in index]
    regimes = [index[x] for x in claim.get("regime_ids", []) if x in index]
    clean = lambda x: {k: copy.deepcopy(v) for k, v in x.items() if k != "_collection"}
    return {"role": clean(role), "org": clean(org), "story": clean(story),
            "figures": [clean(x) for x in figures], "regimes": [clean(x) for x in regimes]}


def records_from_corpus(corpus: dict, *, approve_confirmed: bool = False) -> list[ImpactRecord]:
    index = upstream.index(corpus)
    result = []
    now = datetime.now(timezone.utc).isoformat()
    for claim in corpus["claims"]:
        bundle = _private_bundle(claim, index)
        role, org = bundle["role"], bundle["org"]
        organization = org.get("name") if org.get("nameable") is not False else org.get("public_name")
        confirmed = claim.get("status") == "confirmed"
        source_revision = digest({"claim": claim, "linked_entities": bundle})
        detail = {"version": 1, "claim": copy.deepcopy(claim), "linked_entities": bundle,
                  "source_revision": source_revision, "history": [],
                  "disclosure_approved": not bool(claim.get("disclosure_limit_ids"))}
        # All measurement semantics remain intact in the linked figure. Do not
        # invent a business layer, causal direction or a measured-vs-estimated label.
        result.append(ImpactRecord(
            id="mycareer:" + claim["id"],
            scope=Scope(type="employment" if role else "other", company=organization,
                        role=role.get("public_title") or role.get("title"),
                        project=None if role else "Career evidence"),
            problem=claim.get("constraint") or claim.get("text") or "Unresolved career question",
            intervention=Intervention(summary=claim.get("bullet") or claim.get("text", ""),
                                      provenance="mycareer:" + claim["id"]),
            attribution="team_contributor",  # completed is not synonymous with sole ownership
            confidence="low", recency=_dates(role.get("dates") or claim.get("dates")),
            review_status="rejected" if claim.get("status") == "rejected" else (
                "reviewed" if approve_confirmed and confirmed else "drafted"),
            resume_eligible=bool(approve_confirmed and confirmed),
            created_at=now, updated_at=now,
            provenance={"provider": "mycareer", "source_id": claim["id"], "source_revision": source_revision},
            career_detail=detail,
        ))
    from .projection import refresh_public_fields
    for record in result:
        refresh_public_fields(record)
    return result


def import_corpus(value: dict, repository, *, approve_confirmed=False, dry_run=False) -> dict:
    corpus, warnings = validate_corpus(value)
    records = records_from_corpus(corpus, approve_confirmed=approve_confirmed)
    # Canonical claims live in impact records. Other source entities, questions,
    # rejections and sessions share the same envelope and atomic mutation owner.
    context = {k: v for k, v in corpus.items() if k != "claims"}
    if not dry_run:
        repository.merge_career(records, context)
    from .projection import public_claim
    return {"dry_run": dry_run, "records": len(records),
            "outbound_eligible": sum(public_claim(r) is not None for r in records),
            "warnings": warnings, "source_sha256": digest(corpus),
            "verification": "Metadata checked; external evidence documents were not independently verified"}


def corpus_from_repository(repository) -> dict:
    """Local private reconstruction for validation/interview tools, never outbound."""
    corpus = repository.career_context()
    corpus["claims"] = [copy.deepcopy(record.career_detail["claim"])
                        for record in repository.list_records() if record.career_detail]
    for key in upstream.COLLECTIONS:
        corpus.setdefault(key, [])
    corpus.setdefault("person", {})
    corpus.setdefault("mycareer_version", "0.1")
    return corpus

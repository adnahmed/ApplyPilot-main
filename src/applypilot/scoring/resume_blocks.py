"""Typed résumé composition: model-selected blocks, deterministic renderers.

The model selects content/order, never HTML, LaTeX, fonts or executable templates.
Old payloads without a layout retain their original section order.
"""
from __future__ import annotations

import hashlib
import json
import re

from jsonschema import Draft202012Validator

from .validator import sanitize_text

HEADINGS = {"summary": "SUMMARY", "skills": "TECHNICAL SKILLS", "experience": "EXPERIENCE",
            "projects": "SELECTED PROJECTS", "education": "EDUCATION",
            "certifications": "CERTIFICATIONS", "languages": "LANGUAGES"}
SECTION_TYPES = {value: key for key, value in HEADINGS.items()} | {
    "PROFILE": "summary", "PROFESSIONAL SUMMARY": "summary", "SKILLS": "skills",
    "PROFESSIONAL EXPERIENCE": "experience", "WORK EXPERIENCE": "experience",
    "PROJECTS": "projects", "EARLIER EXPERIENCE": "earlier_experience",
    "OPEN-SOURCE & PRODUCTION PLATFORMS": "opensource",
    "OPEN SOURCE & PRODUCTION PLATFORMS": "opensource",
    "OPEN-SOURCE": "opensource", "OPEN SOURCE": "opensource",
}
STYLE_PRESETS = {
    "classic": {"face": "Calibri", "name_face": "Calibri", "name_size": 18,
                "align": "center", "ink": "1A3A5C", "accent": "2A7AB5", "margin": 0.35, "section_gap": 6},
    "technical": {"face": "Arial", "name_face": "Arial", "name_size": 22,
                  "align": "left", "ink": "182C3C", "accent": "35556B", "margin": 0.5, "section_gap": 8},
    "editorial": {"face": "Calibri", "name_face": "Georgia", "name_size": 24,
                  "align": "left", "ink": "292524", "accent": "78716C", "margin": 0.55, "section_gap": 9},
}


LAYOUT_SCHEMA = {"type": "object", "additionalProperties": False,
    "properties": {"version": {"const": 1}, "style": {"enum": list(STYLE_PRESETS)}, "blocks": {"type": "array", "minItems": 1, "maxItems": 7,
        "items": {"type": "object", "additionalProperties": False,
            "properties": {"type": {"enum": list(HEADINGS)}, "show_technologies": {"type": "boolean"}},
            "required": ["type"]}}}, "required": ["version", "blocks"]}


def plan_for(data: dict) -> dict:
    explicit = data.get("layout")
    if explicit is None:
        return {"version": 1, "blocks": [{"type": key, "show_technologies": True}
                for key in HEADINGS if data.get(key)]}
    if not Draft202012Validator(LAYOUT_SCHEMA).is_valid(explicit):
        raise ValueError("Layout must use version 1 and supported typed blocks only")
    kinds = [block["type"] for block in explicit["blocks"]]
    if len(kinds) != len(set(kinds)):
        raise ValueError("Layout contains duplicate blocks")
    plan = {"version": 1, "blocks": [{"type": block["type"],
            "show_technologies": block.get("show_technologies", True)} for block in explicit["blocks"]]}
    # Omitted style keeps legacy manifests and their hashes unchanged.
    if "style" in explicit:
        plan["style"] = explicit["style"]
    return plan


def manifest(data: dict) -> dict:
    plan = plan_for(data)
    identity = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**plan, "layout_id": identity, "selection": "model" if data.get("layout") else "legacy_default"}


def quality_notes(data: dict) -> dict:
    """Local writing feedback, not fact verification or an admission gate."""
    bullets = [str(b).strip() for kind in ("experience", "projects")
               for entry in data.get(kind) or [] if isinstance(entry, dict)
               for b in entry.get("bullets") or []]
    normalized = [re.sub(r"\W+", " ", b.casefold()).strip() for b in bullets]
    generic = ("results-driven", "results driven", "dynamic professional", "passionate about",
               "proven track record", "highly motivated", "leveraging my expertise")
    summary = str(data.get("summary") or "").casefold()
    warnings = []
    if any(phrase in summary for phrase in generic):
        warnings.append("Summary contains generic positioning; prefer supported domain, scope and outcomes.")
    repeated = len(normalized) - len(set(normalized))
    if repeated:
        warnings.append(f"{repeated} exact duplicate bullet(s) across experience/projects; review attribution and repetition.")
    if len(summary.split()) > 80:
        warnings.append("Summary exceeds 80 words; consider foregrounding the strongest role-specific evidence.")
    long_bullets = sum(len(b.split()) > 55 for b in bullets)
    if long_bullets:
        warnings.append(f"{long_bullets} bullet(s) exceed 55 words; review readability.")
    return {"mode": "advisory_only", "factual_review": "not_performed", "warnings": warnings,
            "bullet_count": len(bullets), "duplicate_bullets": repeated,
            "summary_words": len(summary.split())}


def _text(value) -> str:
    # Scalar fields cannot inject another block delimiter or bullet.
    return " ".join(sanitize_text(str(value or "")).split())


def render_blocks(data: dict) -> str:
    sections = []
    for block in plan_for(data)["blocks"]:
        kind, body = block["type"], []
        value = data.get(kind)
        if kind == "summary":
            body.append(_text(value))
        elif kind == "skills":
            body.extend(f"{_text(category)}: {_text(items)}" for category, items in (value or {}).items() if _text(items))
        elif kind in {"experience", "projects"}:
            seen_ids, seen_headers = set(), set()
            for entry in value or []:
                source = str(entry.get("source_id") or "").strip().casefold()
                header = re.sub(r"[^a-z0-9]+", "", str(entry.get("header") or "").casefold())
                if kind == "projects" and ((source and source in seen_ids) or (header and header in seen_headers)):
                    continue
                seen_ids.add(source)
                seen_headers.add(header)
                body.append(_text(entry.get("header")))
                # Legacy subtitles can mix dates and technology. Never guess
                # which part to remove: only the new explicit field is optional.
                details = [_text(entry.get("subtitle"))]
                if block["show_technologies"]:
                    details.append(_text(entry.get("technologies")))
                if any(details):
                    body.append(" | ".join(dict.fromkeys(part for part in details if part)))
                if entry.get("dates") and _text(entry["dates"]) not in " ".join(details):
                    body.append(_text(entry["dates"]))
                body.extend("- " + _text(bullet) for bullet in entry.get("bullets", []) if _text(bullet))
                body.append("")
        elif kind == "education":
            for item in value if isinstance(value, list) else [value]:
                if isinstance(item, dict):
                    body.extend(part for part in [_text(item.get("institution")),
                        ", ".join(dict.fromkeys(_text(item.get(key)) for key in ("degree", "area", "field", "studyType") if item.get(key))),
                        _text(item.get("dates"))] if part)
                else:
                    body.append(_text(item))
                body.append("")
        else:
            body.extend(_text(item) for item in value or [] if _text(item))
        content = "\n".join(body).strip()
        if content:
            sections.append(HEADINGS[kind] + "\n" + content)
    return "\n\n".join(sections)

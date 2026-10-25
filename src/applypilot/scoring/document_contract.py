"""Source evidence and actionable feedback for document generation."""
from __future__ import annotations

import re

CONTRACT_VERSION = 4


def canonical_bullets(resume: str) -> dict[str, dict]:
    bullets = {}
    section, header, subtitle, scope_id = "", "", "", ""
    in_bullets = False
    for number, line in enumerate(resume.splitlines(), 1):
        value = line.strip()
        heading = re.match(r"^(#{1,6})\s+(.+)", value)
        label = (heading.group(2) if heading else value).strip("*").strip()
        if ((heading and len(heading.group(1)) <= 2) or (label.isupper() and len(label) < 90)):
            section = "experience" if re.search(r"experience|employment", label, re.I) else (
                "projects" if re.search(r"projects", label, re.I) else "")
            header = subtitle = scope_id = ""
            in_bullets = False
            continue
        if heading and len(heading.group(1)) >= 3:
            header, subtitle, scope_id = label, "", f"resume:scope:L{number}"
            in_bullets = False
            continue
        if re.match(r"^[-*•]\s+", value):
            text = re.sub(r"^[-*•]\s+", "", value)
            if text and section and header:
                bullets[f"resume:L{number}"] = {
                    "text": text, "context": " | ".join(filter(None, (header, subtitle))),
                    "section": section, "scope_id": scope_id, "header": header, "subtitle": subtitle,
                }
                in_bullets = True
        elif value and section:
            if not header or in_bullets:
                header, subtitle, scope_id = label, "", f"resume:scope:L{number}"
                in_bullets = False
            elif not subtitle:
                subtitle = label
    return bullets


def validation_reasons(report: dict) -> list[str]:
    reasons = []
    validator = report.get("validator") or {}
    for key in ("errors", "impact_errors", "resume_fact_errors"):
        values = validator.get(key, [])
        reasons.extend(values if isinstance(values, list) else [values])
    judge = report.get("judge") or {}
    if judge and not judge.get("passed") and judge.get("issues"):
        reasons.append(judge["issues"])
    for key in ("errors", "validation_errors"):
        value = report.get(key, [])
        reasons.extend(value if isinstance(value, list) else [value])
    return list(dict.fromkeys(str(reason) for reason in reasons if reason))

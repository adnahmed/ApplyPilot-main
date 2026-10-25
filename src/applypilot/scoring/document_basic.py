"""Document renderability checks; output schemas own field types."""
from __future__ import annotations

from applypilot.scoring.validator import sanitize_text


def validate_resume_payload(data: object) -> dict:
    """Check a schema-validated resume without requiring individual sections."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return _result(["Resume response must be a JSON object"])

    from .resume_blocks import render_blocks
    try:
        body = render_blocks(data)
    except ValueError as exc:
        return _result([str(exc)])
    if not isinstance(data.get("title"), str) or not data["title"].strip():
        errors.append("Missing required text field: title")
    if not body.strip():
        errors.append("Resume has no renderable content")

    return _result(errors)


def validate_cover_payload(data: object) -> tuple[str, dict]:
    """Extract a non-empty letter from the required JSON envelope."""
    if not isinstance(data, dict):
        return "", _result(["Cover response must be a JSON object"])
    letter = data.get("letter")
    if not isinstance(letter, str) or not letter.strip():
        return "", _result(["Missing required text field: letter"])
    return sanitize_text(letter), _result([])


def _result(errors: list[str]) -> dict:
    return {
        "passed": not errors,
        "errors": errors,
        "warnings": [],
        "validation_mode": "basic",
        "factual_review": "not_run",
    }

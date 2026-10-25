"""Instructor extraction for JSON returned by ApplyPilot's existing transports.

Transport retries, provider selection and usage diagnostics stay in ``llm``.
Instructor parses complete JSON and runs the caller's original JSON Schema
inside a Pydantic validator, without converting or weakening dynamic schemas.
"""

import json
from types import SimpleNamespace
from typing import Any

from instructor import Mode, ResponseSchema
from jsonschema import Draft202012Validator, ValidationError as SchemaValidationError
from pydantic import RootModel, ValidationError as PydanticValidationError, ValidationInfo, model_validator


def json_validation_summary(error: ValueError | SchemaValidationError) -> str:
    """Describe a repair using schema fields and types, never response values."""
    if isinstance(error, SchemaValidationError):
        path = "/" + "/".join(str(part).replace("~", "~0").replace("/", "~1")
                              for part in error.absolute_path)
        detail = str(error.validator)
        if error.validator == "required" and isinstance(error.instance, dict):
            missing = [key for key in error.validator_value if key not in error.instance]
            detail = "missing required properties: " + json.dumps(missing, ensure_ascii=False)
        elif error.validator == "type":
            detail = "expected type " + json.dumps(error.validator_value)
        elif error.validator == "enum":
            detail = "expected one of " + json.dumps(error.validator_value, ensure_ascii=False)
        elif error.validator == "pattern":
            detail = "expected pattern " + json.dumps(error.validator_value, ensure_ascii=False)
        elif error.validator == "contains":
            detail = "must include an item matching " + json.dumps(error.validator_value, ensure_ascii=False)
        elif error.validator in {"minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems"}:
            detail += " " + str(error.validator_value)
        return f"Schema validation failed at {path[:300]}: {detail[:700]}"
    if isinstance(error, json.JSONDecodeError):
        return f"Invalid JSON syntax at line {error.lineno}, column {error.colno}"
    if isinstance(error, PydanticValidationError):
        if any(issue["type"] == "json_invalid" for issue in error.errors(include_input=False)):
            return "Invalid JSON syntax"
        return "JSON value failed validation; use only finite numbers and schema-valid values"
    return "JSON value failed validation"


class _SchemaValue(RootModel[Any], ResponseSchema):
    """A JSON value whose complete contract comes from the requesting stage."""

    @model_validator(mode="after")
    def validate_schema(self, info: ValidationInfo) -> "_SchemaValue":
        # Pydantic accepts NaN/Infinity in JSON. They are never valid document
        # facts, including when nested below a permissive schema property.
        json.dumps(self.root, allow_nan=False)
        info.context["validator"].validate(self.root)
        return self


def parse_json_response(raw: str, validator: Draft202012Validator) -> Any:
    """Extract a schema-validated value with no network calls or retries.

    Only a single enclosing Markdown fence is removed. Instructor's JSON mode
    parses the whole remaining string; MD_JSON's permissive substring recovery
    is intentionally avoided so commentary or incomplete output is rejected.
    """
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        first, separator, rest = text.partition("\n")
        if separator and first.lower() in {"```", "```json"}:
            text = rest[:-3].strip()

    # chat() has already extracted visible text and handled provider metadata.
    # Adapt that text to Instructor's completion parser; do not fabricate usage,
    # model IDs or a successful finish reason, or replace diagnostic responses.
    completion = SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=text), finish_reason=None,
    )])
    result = _SchemaValue.from_response(
        completion,
        mode=Mode.JSON,
        strict=True,
        validation_context={"validator": validator},
    )
    return result.root

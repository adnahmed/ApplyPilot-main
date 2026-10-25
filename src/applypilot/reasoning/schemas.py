"""Schemas for RLM extraction; business/evidence validation remains at call sites."""

DETAIL_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["full_description", "application_url"],
    "properties": {
        "full_description": {"type": ["string", "null"]},
        "application_url": {"type": ["string", "null"]},
    },
}


def _record(properties):
    return {"type": "object", "additionalProperties": False, "required": list(properties),
            "properties": {name: {"type": "string"} for name in properties}}


RESEARCH_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["facts", "contacts", "unknown_fields"],
    "properties": {
        "facts": {"type": "array",
                  "items": _record(["field", "value", "source_id", "quote"])},
        "contacts": {"type": "array",
                     "items": _record(["name", "company", "role_function", "profile_url", "source_id", "quote"])},
        "unknown_fields": {"type": "array", "items": {"type": "string"}},
    },
}

"""Deterministic, labeled GEPA feedback; no model-as-judge or executable metrics."""
from __future__ import annotations

import json
import math
import re

from jsonschema import Draft202012Validator

OPS = frozenset({"equals", "contains", "not_contains", "one_of", "between"})


def validate_checks(checks):
    if not isinstance(checks, list) or not checks:
        raise ValueError("Every example needs at least one labeled semantic check")
    json.dumps(checks, allow_nan=False)  # Reject non-JSON labels before spending quota.
    for check in checks:
        if (not isinstance(check, dict) or set(check) - {"path", "op", "value", "critical", "feedback"}
                or not {"path", "op", "value"} <= set(check)):
            raise ValueError("Each check needs path, op and value; critical is optional")
        path = check["path"]
        if (not isinstance(path, str) or (path and not path.startswith("/"))
                or re.search(r"~(?![01])", path)):
            raise ValueError("Check paths must be JSON pointers")
        if check["op"] not in OPS or not isinstance(check.get("critical", False), bool):
            raise ValueError("Invalid check operator/critical flag")
        if "feedback" in check and not isinstance(check["feedback"], str):
            raise ValueError("Check feedback must be text")
        value = check["value"]
        if check["op"] == "one_of" and (not isinstance(value, list) or not value):
            raise ValueError("one_of needs a nonempty array")
        if check["op"] == "between":
            if (not isinstance(value, list) or len(value) != 2
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value)
                    or value[0] > value[1]):
                raise ValueError("between needs finite [minimum, maximum] bounds")


def _at(value, pointer):
    if not pointer:
        return value
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise KeyError(pointer)
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError(pointer)
    return value


def _equal(left, right):
    # JSON bool is not numeric 0/1. Compare normalized JSON, not Python's True==1.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def _passes(value, check):
    try:
        actual = _at(value, check["path"])
    except (KeyError, IndexError):
        return False
    expected, op = check["value"], check["op"]
    if op == "equals":
        return _equal(actual, expected)
    if op == "one_of":
        return any(_equal(actual, allowed) for allowed in expected)
    if op == "between":
        return not isinstance(actual, bool) and isinstance(actual, (int, float)) and expected[0] <= actual <= expected[1]
    if isinstance(actual, str) and isinstance(expected, str):
        found = expected in actual
    elif isinstance(actual, list):
        found = any(_equal(item, expected) for item in actual)
    else:
        return False
    return not found if op == "not_contains" else found


def assess(gold, pred):
    from applypilot.structured_output import parse_json_response

    try:
        validator = Draft202012Validator(json.loads(gold.schema_json))
        value = parse_json_response(pred.answer, validator)
    except (ValueError, AttributeError, TypeError) as exc:
        return {"score": 0.0, "feedback": f"Return complete schema-valid JSON ({type(exc).__name__}). "
                "Do not fill missing fields with invented evidence.", "valid": False, "critical_ok": False}
    except Exception as exc:
        from jsonschema import ValidationError
        if not isinstance(exc, ValidationError):
            raise
        return {"score": 0.0, "feedback": "Schema mismatch at /" + "/".join(map(str, exc.absolute_path)),
                "valid": False, "critical_ok": False}
    failed = [check for check in gold.checks if not _passes(value, check)]
    critical_ok = not any(check.get("critical", False) for check in failed)
    score = (len(gold.checks) - len(failed)) / len(gold.checks) if critical_ok else 0.0
    feedback = "All labeled checks passed. Preserve evidence provenance and schema compliance."
    if failed:
        details = [(c.get("feedback") or f"{c['path'] or '/'}: {c['op']} {json.dumps(c['value'], ensure_ascii=True)}")
                   for c in failed]
        feedback = ("Failed labeled requirements: " + "; ".join(details)
                    + ". Re-read the supplied evidence; never invent a fact to satisfy a check.")
    return {"score": score, "feedback": feedback, "valid": True, "critical_ok": critical_ok}


def gepa_metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
    import dspy

    result = assess(gold, pred)
    # DSPy 3.3.1 detects feedback by attribute, not by dict membership.
    return dspy.Prediction(score=result["score"], feedback=result["feedback"])

"""Credential-free diagnostics from explicit reader failure envelopes."""
from __future__ import annotations


def reader_error(value: object) -> RuntimeError:
    from applypilot.operations.runtime import sanitize
    def message(item, depth=0):
        if isinstance(item, str):
            return item[:2000]
        if isinstance(item, dict) and depth < 4:
            for key in ("error", "message", "detail", "text", "structured"):
                found = message(item.get(key), depth + 1)
                if found:
                    return found
        return ""
    detail = sanitize(message(value), 500).strip()
    return RuntimeError("Service reader failed" + (": " + detail if detail else ""))

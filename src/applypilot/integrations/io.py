"""Bounded private artifacts. Credentials are never exported to research models."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile

from .types import SourceError

_SECRET = re.compile(r"(?i)(password|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|secret|private[_-]?key)")


def redact(value, depth=0):
    if depth > 30:
        return "[depth limited]"
    if isinstance(value, dict):
        if "name" in value and "value" in value and (_SECRET.search(str(value["name"])) or "domain" in value):
            value = {**value, "value": "[redacted]"}
        return {str(k): "[redacted]" if _SECRET.search(str(k)) else redact(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, depth + 1) for v in value[:1000]]
    return value


def read_json(path):
    path = Path(path)
    try:
        if path.stat().st_size > 8_000_000:
            raise SourceError("JSON input exceeds 8 MB", "schema")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SourceError("Unable to read JSON input", "schema") from exc


def write_json(path, data, *, overwrite=False):
    path = Path(path).expanduser()
    if path.exists() and not overwrite:
        raise SourceError("Output already exists; choose another path or explicitly allow overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if not overwrite:
            # Atomic no-clobber publication on normal local filesystems.
            os.link(temporary, path)
            temporary.unlink()
        else:
            os.replace(temporary, path)
    except OSError as exc:
        raise SourceError("Could not write private output artifact", "configuration") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

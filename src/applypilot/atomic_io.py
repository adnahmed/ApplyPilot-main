"""Atomic replacement with bounded retry for transient Windows sharing locks."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import time
from uuid import uuid4


log = logging.getLogger(__name__)


def replace_with_retry(source, target):
    for attempt in range(6):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(min(0.05 * 2 ** attempt, 0.4))


def atomic_record(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("JSON record path is a symlink")
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            # Do not turn a successful publication into a failure, or mask the
            # original error. Only this writer's unique temporary file is touched.
            log.warning("Temporary JSON record cleanup deferred")

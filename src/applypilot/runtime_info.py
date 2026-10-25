"""Local runtime identity without credentials or provider requests."""
from __future__ import annotations
import hashlib
from importlib import metadata
import os
from pathlib import Path
import sys
from functools import lru_cache


@lru_cache(maxsize=1)
def runtime_info() -> dict:
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or path.suffix not in {".py", ".js", ".css", ".html", ".yaml", ".json", ".sql", ".md"}:
            continue
        relative = path.relative_to(root)
        if any(part in {"__pycache__", "tests", "test"} for part in relative.parts) or path.name.startswith("test_"):
            continue
        name = relative.as_posix()
        digest.update(name.encode())
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        except OSError:
            digest.update(b"missing")
        digest.update(b"\0")
    try:
        cloak = metadata.version("cloakbrowser")
    except metadata.PackageNotFoundError:
        cloak = None
    return {"build": "applypilot-" + digest.hexdigest()[:12], "build_hash": digest.hexdigest(),
            "python": sys.version.split()[0],
            "python_executable": sys.executable, "package_path": str(root),
            "cloakbrowser": cloak, "required_cloakbrowser": "0.5.6"}


def process_info(role: str) -> dict:
    from applypilot.config import LOADED_CONFIG_PATHS

    return {**runtime_info(), "pid": os.getpid(), "role": role,
            "runtime_id": os.getenv("APPLYPILOT_RUNTIME_ID"),
            "loaded_config_paths": sorted(LOADED_CONFIG_PATHS)}

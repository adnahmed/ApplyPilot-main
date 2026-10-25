"""Resolve and inspect the local backup executables without contacting a repository."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path

from applypilot.backup.common import environment

RESTIC_VERSION = "0.19.1"
RCLONE_VERSION = "1.75.1"


def tool_paths(root: Path) -> dict[str, str]:
    """Use installed binaries; preparation belongs to the package build."""
    paths = {}
    env = environment(root)
    package = find_spec("applypilot_backup_bins")
    packaged = Path(package.origin).parent if package and package.origin else None
    extension = ".exe" if os.name == "nt" else ""
    for tool, filename in (("restic", "restic"), ("rclone", "applypilot-rclone")):
        override = env.get(f"APPLYPILOT_{tool.upper()}_BIN", "").strip()
        if override:
            candidate = Path(override).expanduser()
            found = str(candidate) if candidate.is_file() else shutil.which(override)
        else:
            candidates = ([packaged / (filename + extension)] if packaged else [])
            candidates.append(Path(root).expanduser().resolve() / "github_sync" / "bin" / (filename + extension))
            found = next((str(candidate) for candidate in candidates if candidate.is_file()), None) or shutil.which(filename)
        paths[tool] = str(Path(found).resolve()) if found else ""
    return paths


def _output(command: list[str]) -> str:
    return subprocess.run(
        command, check=True, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ).stdout


@lru_cache(maxsize=16)
def _probe(tool: str, executable: str, _stamp: tuple[int, int, int]) -> tuple[str, str]:
    """Reuse probes until the executable changes or the 30-second window expires."""
    version = ""
    minimum = RESTIC_VERSION if tool == "restic" else RCLONE_VERSION
    try:
        if tool == "restic":
            identifier = json.loads(_output([executable, "version", "--json"]))["version"]
        else:
            identifier = _output([executable, "version"]).removeprefix("rclone v")
        match = re.match(r"(\d+\.\d+\.\d+)", identifier)
        if not match:
            raise ValueError(f"{tool} did not return a supported version identifier")
        version = match.group(1)
        if tuple(map(int, version.split("."))) < tuple(map(int, minimum.split("."))):
            raise ValueError(f"{tool} {minimum} or newer is required")
        if tool == "rclone":
            providers = json.loads(_output([executable, "config", "providers"]))
            if not any(item.get("Name") == "githubreleases" for item in providers):
                raise ValueError("rclone must include ApplyPilot's githubreleases provider")
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError, KeyError) as exc:
        return version, f"{tool}: {exc}"
    return version, ""


def tool_status(root: Path) -> dict:
    """Check executable versions and the compiled provider; never install on a read."""
    paths = tool_paths(root)
    versions = {}
    errors = []
    env = environment(root)
    for tool, executable in paths.items():
        version, error = "", f"{tool} is not installed"
        if executable:
            try:
                stat = Path(executable).stat()
                version, error = _probe(tool, executable, (stat.st_size, stat.st_mtime_ns, int(time.monotonic() // 30)))
            except OSError as exc:
                error = f"{tool}: {exc}"
        if version:
            versions[tool] = version
        if error:
            setting = f"APPLYPILOT_{tool.upper()}_BIN"
            if env.get(setting, "").strip():
                error += f". Correct {setting} or remove it to use the tools prepared by uv sync."
            else:
                error += ". Run uv sync to prepare backup tools with the application environment."
            errors.append(error)
    return {
        "ready": not errors,
        "paths": paths,
        "versions": versions,
        "errors": errors,
    }

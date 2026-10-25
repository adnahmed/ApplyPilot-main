"""Private configuration, cross-process locking and Git checkpoint validation."""
from __future__ import annotations

import errno
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets

from applypilot.onboarding.storage import read_json, write_json

BRANCH = "applypilot-backups"
STATE = "account-state.json"
FORMAT = "applypilot-github-account-v1"
DEFAULTS = {"enabled": False, "repository": "", "static_minutes": 5,
            "snapshot_hours": 6, "keep_snapshots": 30,
            "max_snapshot_gb": 64, "include_documents": True,
            "auth_mode": "auto", "github_account": ""}


def folder(root: Path) -> Path:
    path = root / "github_sync"
    if path.is_symlink():
        raise ValueError("Backup state must not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def repository(value) -> str:
    value = str(value or "").strip().removeprefix("https://github.com/").removesuffix(".git").rstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise ValueError("Use a GitHub repository in owner/name format")
    return value


def config(root: Path) -> dict:
    # Environment values are defaults, not an override of a saved owner disable.
    defaults = dict(DEFAULTS)
    env = environment(root)
    for name, default in DEFAULTS.items():
        raw = env.get("APPLYPILOT_BACKUP_" + name.upper())
        if raw is None or not str(raw).strip():
            continue
        if type(default) is bool:
            text = str(raw).strip().lower()
            if text not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("Invalid backup boolean: " + name)
            defaults[name] = text in {"true", "1", "yes", "on"}
        elif type(default) is int:
            defaults[name] = int(raw)
        else:
            defaults[name] = str(raw).strip()
    cfg = defaults | read_json(folder(root) / "config.json", {})
    cfg.pop("chunk_mb", None)  # Legacy archive framing is owned by restic now.
    if cfg["auth_mode"] not in {"auto", "github_cli", "token"}:
        raise ValueError("Backup authentication must be auto, github_cli, or token")
    if not isinstance(cfg["github_account"], str) or (cfg["github_account"] and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", cfg["github_account"])):
        raise ValueError("Invalid backup GitHub account")
    if cfg["repository"]:
        cfg["repository"] = repository(cfg["repository"])
    for key, low, high in (("static_minutes", 1, 1440), ("snapshot_hours", 1, 168),
                           ("keep_snapshots", 3, 365), ("max_snapshot_gb", 1, 512)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be an integer from {low} to {high}")
    for name in ("enabled", "include_documents"):
        if type(cfg[name]) is not bool:
            raise ValueError(name + " must be true or false")
    return cfg


def environment(root: Path) -> dict:
    from dotenv import dotenv_values
    # Explicit workspace settings win for this integration; no stale parent env after website edits.
    return dict(os.environ) | {k: v for k, v in dotenv_values(root / ".env", encoding="utf-8-sig", interpolate=False).items() if v is not None}


def validate_checkpoint(value: dict) -> dict:
    """Validate Git metadata fetched through the authorized private repository."""
    if not isinstance(value, dict) or value.get("format") != FORMAT:
        raise ValueError("This is not a current account-based backup checkpoint")
    for field in ("workspace", "device"):
        if not isinstance(value.get(field), str) or not re.fullmatch(r"[0-9a-f]{32}", value[field]):
            raise ValueError("Invalid backup identity")
    snapshot = value.get("snapshot")
    if (not isinstance(snapshot, dict) or snapshot.get("engine") != "restic"
            or not re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("id", "")))
            or snapshot.get("tag") != "restic:" + snapshot["id"]):
        raise ValueError("Checkpoint does not reference a completed restic snapshot")
    from .restic import namespace
    namespace(value["workspace"], snapshot)
    files = value.get("files")
    if not isinstance(files, dict) or len(files) > 5000:
        raise ValueError("Invalid static checkpoint file inventory")
    total = 0
    for name, info in files.items():
        if (not isinstance(name, str) or not name.startswith("files/") or not isinstance(info, dict)
                or type(info.get("size")) is not int or not 0 <= info["size"] <= 8 * 1024**2
                or not re.fullmatch(r"[0-9a-f]{64}", str(info.get("sha256", "")))):
            raise ValueError("Invalid static checkpoint file metadata")
        total += info["size"]
    deleted = value.get("static_deleted", [])
    if total > 32 * 1024**2 or not isinstance(deleted, list) or len(deleted) > 5000 or any(not isinstance(name, str) for name in deleted):
        raise ValueError("Static checkpoint exceeds recovery metadata limits")
    if not isinstance(value.get("created_at"), str) or len(value["created_at"]) > 80:
        raise ValueError("Invalid checkpoint timestamp")
    return value


class BackupBusyError(RuntimeError):
    pass


@contextmanager
def lock(root: Path, name="operation", *, required=True):
    """OS-owned advisory lock. The file can remain after a crash; the lock cannot."""
    path = folder(root) / (name + ".lock")
    if path.is_symlink():
        raise ValueError("Backup lock must not be a symlink")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"0")
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            if required:
                raise BackupBusyError("Another backup/recovery operation is already running") from None
            yield False
            return
        yield True
    finally:
        if acquired:
            if os.name == "nt":
                import msvcrt
                os.lseek(fd, 0, 0)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def identity(root: Path) -> dict:
    path = folder(root) / "identity.json"
    value = read_json(path, {})
    if not value:
        value = {"workspace": secrets.token_hex(16), "device": secrets.token_hex(16)}
        write_json(path, value)
    return value


def operation_busy(root: Path) -> bool:
    """Probe the OS lock without trusting stale PID/status files."""
    with lock(root, required=False) as acquired:
        return not acquired

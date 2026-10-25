"""Bounded private storage and revision checks for the owner's setup workspace."""
from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import hashlib
import json
import os
import re
import secrets
from contextlib import contextmanager

SECRET_KEY = re.compile(r"password|secret|token|credential|api.?key", re.I)
CORE = ("profile.json", "searches.yaml", "resume.txt", "resume.pdf")


def now():
    return datetime.now(UTC).isoformat()


def safe_path(root: Path, name: str) -> Path:
    path = root / name
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != root.parent):
        raise ValueError("Setup does not follow symlinked configuration paths")
    return path


def read_bytes(path: Path, limit=2_000_000) -> bytes:
    if path.is_symlink():
        raise ValueError("Symlinked configuration files are not supported in web setup")
    if not path.exists():
        return b""
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError(f"{path.name} exceeds its setup size limit or is not a file")
    return path.read_bytes()


def read_json(path: Path, default=None, limit=2_000_000):
    raw = read_bytes(path, limit)
    return json.loads(raw) if raw else deepcopy(default)


def atomic(path: Path, content: bytes):
    safe_path(path.parent, path.name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        from applypilot.atomic_io import replace_with_retry
        replace_with_retry(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_json(path: Path, value):
    atomic(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode())


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(data).hexdigest()


def revision(root: Path, names=CORE):
    return digest({n: digest(read_bytes(safe_path(root, n), 8_000_000)) for n in names})


def public(value):
    """No stored credentials in browser responses, AI prompts, or diff previews."""
    if isinstance(value, dict):
        return {str(k): public(v) for k, v in value.items() if not SECRET_KEY.search(str(k))}
    if isinstance(value, list):
        return [public(v) for v in value]
    return value


def merge(base, patch):
    result = deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


@contextmanager
def setup_lock(root: Path):
    """Cross-process, released by the OS on crash; no stale lock-file deletion."""
    import time
    path = safe_path(root, "setup-write.lock")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"0")
    acquired = False
    try:
        deadline = time.monotonic() + 5
        while not acquired:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                if time.monotonic() > deadline:
                    raise ValueError("Another setup change is being saved. Retry shortly.") from None
                time.sleep(.05)
        yield
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


def running(root):
    from applypilot.processes import runtime_owned
    return any(runtime_owned(root, name) for name in ("autopilot", "apply-runtime"))


def require_stopped(root):
    if running(root):
        raise ValueError("Autopilot is still finishing active work. Use Pause in the dashboard; settings and connection actions pause managed workers automatically.")


def backup(root, files):
    folder = root / "setup" / "backups" / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(4))
    manifest = {}
    for name in files:
        path = safe_path(root, name)
        raw = read_bytes(path, 8_000_000)
        manifest[name] = {"existed": path.exists(), "sha256": digest(raw)}
        if path.exists():
            atomic(folder / name, raw)
    write_json(folder / "manifest.json", {"created_at": now(), "files": manifest})
    return folder

"""Non-destructive cross-platform process liveness checks.

Never probe Windows with os.kill(pid, 0): unlike POSIX, CPython calls
TerminateProcess for that value. Unknown/access-denied processes are treated
as live so a status check cannot authorize replacing a live workspace.
"""
from __future__ import annotations

import os
import errno
from contextlib import contextmanager
from pathlib import Path


def _windows_kernel32():
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api, ctypes.get_last_error


def _windows_process_alive(pid: int) -> bool:
    api, last_error = _windows_kernel32()
    # SYNCHRONIZE grants observation only, not PROCESS_TERMINATE.
    handle = api.OpenProcess(0x00100000, False, pid)
    if not handle:
        # ERROR_INVALID_PARAMETER means the PID no longer exists. Access
        # denied and other indeterminate errors must not break a live lease.
        return last_error() != 87
    try:
        # WAIT_OBJECT_0: exited; WAIT_TIMEOUT: running; WAIT_FAILED: unknown.
        # Unlike exit-code 259, the signalled state is unambiguous.
        return api.WaitForSingleObject(handle, 0) != 0
    finally:
        api.CloseHandle(handle)


def process_alive(pid: object) -> bool:
    """True for a live/inaccessible PID; False for invalid or exited PIDs.

    This checks liveness, not process identity. OS-owned locks are required
    where stale PID reuse would otherwise allow concurrent writers.
    """
    if isinstance(pid, bool):
        return False
    try:
        if isinstance(pid, float) and not pid.is_integer():
            return False
        value = int(pid)
    except (ValueError, TypeError, OverflowError):
        return False
    if not 0 < value <= 0xFFFFFFFF:
        return False
    if os.name == "nt":
        return _windows_process_alive(value)
    try:
        os.kill(value, 0)  # POSIX-only non-destructive probe.
    except (ProcessLookupError, OverflowError):
        return False
    except OSError:
        return True  # Permission or transient failure is not proof of death.
    return True


@contextmanager
def process_guard(path: Path, *, required: bool = True):
    """Hold an OS-owned lock for the complete lifetime of a singleton.

    A PID JSON file alone has a create-before-write race: a second starter can
    mistake the first starter's empty file for a stale lock and unlink it.
    This stable guard is never unlinked and the OS releases it on process exit.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Refusing a symlinked process guard")
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    acquired = False
    try:
        # Windows permits byte-range locks beyond EOF. Leave the stable file
        # empty: initializing it before locking races with another owner, whose
        # byte lock would reject the initializer's write.
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
            if exc.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if required:
                raise RuntimeError("Another process already owns " + path.name) from None
        yield acquired
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def guard_held(path: Path) -> bool:
    """Probe a stable OS-owned lease without deleting it or trusting a saved PID.

    Missing guards are idle. Unexpected access failures fail closed, because
    inability to inspect a lease is not permission to replace live sessions.
    """
    if not path.exists():
        return path.is_symlink()
    try:
        with process_guard(path):
            return False
    except (OSError, RuntimeError):
        return True


def runtime_owned(root: Path, name: str) -> bool:
    """Current releases use guards; pre-guard installations retain a safe fallback."""
    if name not in {"autopilot", "apply-runtime"}:
        raise ValueError("Unknown runtime lease")
    guard = root / (name + ".guard")
    if guard.exists() or guard.is_symlink():
        return guard_held(guard)
    import json
    try:
        row = json.loads((root / (name + ".lock")).read_text(encoding="utf-8"))
        return process_alive(row.get("pid")) if isinstance(row, dict) else False
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True  # An old writer may be between create and write.

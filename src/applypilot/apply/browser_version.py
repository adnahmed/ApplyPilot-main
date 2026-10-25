"""Read the installed browser version without launching a Windows GUI process.

Windows Chrome does not implement the POSIX --version exit path. Executing
chrome.exe merely to inspect its version can open a normal-profile new tab.
Read its PE version resource instead; a failed read must NEVER spawn Chrome.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
import platform
import re
import subprocess

_VERSION = re.compile(r"\b(\d{1,5}(?:\.\d{1,5}){3})\b")


def _windows_file_version(executable: str) -> str | None:
    """Read VS_FIXEDFILEINFO with version.dll; no shell, PowerShell or browser."""
    import ctypes
    from ctypes import wintypes

    class FixedFileInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in (
            "signature", "struct_version", "file_version_ms", "file_version_ls",
            "product_version_ms", "product_version_ls", "flags_mask", "flags",
            "os", "file_type", "subtype", "date_ms", "date_ls",
        )]

    version = ctypes.WinDLL("version", use_last_error=True)
    size_fn = version.GetFileVersionInfoSizeW
    size_fn.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size_fn.restype = wintypes.DWORD
    read_fn = version.GetFileVersionInfoW
    read_fn.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
    read_fn.restype = wintypes.BOOL
    query_fn = version.VerQueryValueW
    query_fn.argtypes = [wintypes.LPCVOID, wintypes.LPCWSTR,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    query_fn.restype = wintypes.BOOL

    unused = wintypes.DWORD()
    size = size_fn(executable, ctypes.byref(unused))
    if not size or size > 4_000_000:
        return None
    data = ctypes.create_string_buffer(size)
    if not read_fn(executable, 0, size, data):
        return None
    pointer = ctypes.c_void_p()
    length = wintypes.UINT()
    if not query_fn(data, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    if not pointer.value or length.value < ctypes.sizeof(FixedFileInfo):
        return None
    info = ctypes.cast(pointer, ctypes.POINTER(FixedFileInfo)).contents
    if info.signature != 0xFEEF04BD:
        return None
    parts = (info.file_version_ms >> 16, info.file_version_ms & 0xFFFF,
             info.file_version_ls >> 16, info.file_version_ls & 0xFFFF)
    return ".".join(map(str, parts)) if any(parts) else None


@lru_cache(maxsize=32)
def _read_version(executable: str, system: str, mtime_ns: int, size: int) -> str | None:
    # The stat fields invalidate the cache after an in-place browser upgrade.
    try:
        if system == "Windows":
            return _windows_file_version(executable)
        if system not in {"Linux", "Darwin"}:
            return None
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=5, check=False)
        match = _VERSION.search(result.stdout or "") if result.returncode == 0 else None
        return match.group(1) if match else None
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return None


def installed_version(executable: str) -> str | None:
    """Return a validated four-part version, or None; never guess a new version."""
    try:
        path = Path(executable).expanduser().resolve()
        stat = path.stat()
        value = _read_version(str(path), platform.system(), stat.st_mtime_ns, stat.st_size)
        return value if isinstance(value, str) and _VERSION.fullmatch(value) else None
    except (OSError, ValueError):
        return None

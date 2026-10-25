"""Initialize private SearXNG; explicit, reversible repair of generated settings.

Normal startup preserves existing settings. --repair removes only the two
Tor-only engines from the generated JSON/YAML-subset settings and creates a
missing limiter.toml. No secret rotation, limiter-policy change, cache deletion,
or loss of other settings. Custom YAML is left intact rather than guessed at.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import tempfile
import tomllib
import uuid

REMOVED_ENGINES = ("ahmia", "torch")
WEB_ENGINES = ("bing", "google")
BACKUPS = ".applypilot-docs-guide-backups"
LIMITER = ("# ApplyPilot private instance: inherit SearXNG bot-detection defaults.\n"
           "# Enabling/disabling the limiter remains controlled by server.limiter.\n"
           "# Existing operator limiter.toml files are never overwritten.\n")


def defaults():
    return {
        "use_default_settings": {"engines": {"remove": list(REMOVED_ENGINES)}},
        "general": {"instance_name": "ApplyPilot local search"},
        "search": {"formats": ["html", "json"], "safe_search": 1},
        "server": {"secret_key": secrets.token_hex(32), "limiter": False,
                   "public_instance": False, "port": 8080, "bind_address": "0.0.0.0"},
        "outgoing": {"request_timeout": 8.0, "max_request_timeout": 15.0},
        "engines": [{"name": name, "disabled": False} for name in WEB_ENGINES],
    }


def _regular(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Refusing a non-regular config entry: {path.name}")


def _create(path, text):
    _regular(path)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(text)
        path.chmod(0o644)
        return True
    except FileExistsError:
        return False


def initialize(folder=Path("/config")):
    """Create missing files only; keep the original boolean return contract."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    made = _create(folder / "settings.yml", json.dumps(defaults(), indent=2) + "\n")
    _create(folder / "limiter.toml", LIMITER)
    return made


def repaired_settings(raw):
    # The original initializer writes JSON to settings.yml (valid YAML).
    # Refuse custom YAML without a parser; never regex-rewrite a secret-bearing file.
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ValueError("Custom YAML settings.yml was preserved. See DOCS_GUIDES_PATCH.md for the two-engine YAML repair; no config files changed.") from None
    if not isinstance(data, dict):
        raise ValueError("settings.yml must be a configuration object; no files changed")
    new = deepcopy(data)
    inherited = new.get("use_default_settings", False)
    if inherited is True:
        inherited = {}
        new["use_default_settings"] = inherited
    elif inherited is not False and not isinstance(inherited, dict):
        raise ValueError("Unrecognized use_default_settings value; no files changed")
    if isinstance(inherited, dict):
        engines = inherited.setdefault("engines", {})
        if not isinstance(engines, dict):
            raise ValueError("Unrecognized inherited engines configuration; no files changed")
        keep = engines.get("keep_only")
        if keep is not None:
            if not isinstance(keep, list) or not all(isinstance(item, str) for item in keep):
                raise ValueError("Invalid engine keep_only list; no files changed")
            if any(item in REMOVED_ENGINES for item in keep):
                remaining = [item for item in keep if item not in REMOVED_ENGINES]
                if not remaining:
                    raise ValueError("Only Tor engines were selected; choose non-Tor engines explicitly. No files changed.")
                engines["keep_only"] = remaining
        else:
            removed = engines.setdefault("remove", [])
            if not isinstance(removed, list) or not all(isinstance(item, str) for item in removed):
                raise ValueError("Invalid engine removal list; no files changed")
            for name in REMOVED_ENGINES:
                if name not in removed:
                    removed.append(name)
    if "engines" in new:
        overrides = new["engines"]
        if not isinstance(overrides, list) or not all(isinstance(item, dict) for item in overrides):
            raise ValueError("Invalid explicit engines list; no files changed")
        new["engines"] = [item for item in overrides if item.get("name") not in REMOVED_ENGINES]
    if isinstance(inherited, dict) and not inherited.get("engines", {}).get("keep_only"):
        overrides = new.setdefault("engines", [])
        existing = {item.get("name") for item in overrides}
        removed = inherited.get("engines", {}).get("remove", [])
        overrides.extend({"name": name, "disabled": False} for name in WEB_ENGINES
                         if name not in existing and name not in removed)
    return raw if new == data else (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _hash(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _atomic(path, raw, mode):
    fd, temp = tempfile.mkstemp(prefix=".applypilot-config-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def _read(path):
    _regular(path)
    return path.read_bytes() if path.exists() else None


def plan_repair(folder):
    plans = []
    for name in ("settings.yml", "limiter.toml"):
        path = folder / name
        old = _read(path)
        mode = stat.S_IMODE(path.stat().st_mode) if old is not None else 0o644
        if name == "settings.yml":
            new = repaired_settings(old) if old is not None else (json.dumps(defaults(), indent=2) + "\n").encode()
        else:
            if old is not None:
                try:
                    tomllib.loads(old.decode("utf-8"))
                except (UnicodeError, ValueError):
                    raise ValueError("Existing limiter.toml is invalid and was preserved. No config files changed.") from None
            new = old if old is not None else LIMITER.encode()
        if old != new:
            plans.append((path, old, new, mode))
    return plans


def repair(folder=Path("/config"), *, check_only=False):
    folder = Path(folder)
    plans = plan_repair(folder)
    if check_only:
        print(f"SearXNG config preflight passed: {len(plans)} file(s) need repair.")
        return None
    if not plans:
        print("SearXNG config already repaired; existing secret and limiter policy retained.")
        return None
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / BACKUPS
    if base.is_symlink():
        raise ValueError("Refusing symbolic-link backup directory")
    base.mkdir(exist_ok=True, mode=0o700)
    name = "searxng-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    backup = base / name
    backup.mkdir(mode=0o700)
    spec = {"format": 1, "files": []}
    for path, old, new, mode in plans:
        if old is not None:
            _atomic(backup / path.name, old, 0o600)
            if _hash((backup / path.name).read_bytes()) != _hash(old):
                raise OSError("Config backup verification failed")
        spec["files"].append({"name": path.name, "before": _hash(old), "after": _hash(new), "mode": mode})
    _atomic(backup / "manifest.json", (json.dumps(spec, indent=2) + "\n").encode(), 0o600)
    written = []
    try:
        for path, old, new, mode in plans:
            if _read(path) != old:
                raise RuntimeError("Configuration changed during repair")
            _atomic(path, new, mode)
            written.append((path, old, new, mode))
        for path, old, new, mode in written:
            if _read(path) != new:
                raise OSError("Config installation verification failed")
    except BaseException:
        for path, old, new, mode in reversed(written):
            if _read(path) != new:
                raise RuntimeError("Concurrent config edit: retain backup and recover manually")
            path.unlink() if old is None else _atomic(path, old, mode)
        raise
    print(f"SearXNG repaired {len(written)} config file(s); secret and unrelated settings retained.")
    print(f"SearXNG backup ID: {name}")
    return name


def restore(folder, name):
    if not name.startswith("searxng-") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-" for c in name):
        raise ValueError("Invalid SearXNG backup ID")
    folder = Path(folder)
    backup = folder / BACKUPS / name
    if (folder / BACKUPS).is_symlink() or backup.is_symlink():
        raise ValueError("Refusing symbolic-link backup")
    spec = json.loads(_read(backup / "manifest.json"))
    if spec.get("format") != 1:
        raise ValueError("Unrecognized backup format")
    plans = []
    seen = set()
    for item in spec["files"]:
        filename = item["name"]
        if filename not in ("settings.yml", "limiter.toml") or filename in seen:
            raise ValueError("Invalid backup entry")
        seen.add(filename)
        path = folder / filename
        current = _read(path)
        if _hash(current) == item["before"]:
            continue
        if _hash(current) != item["after"]:
            raise ValueError("Rollback would overwrite a later config edit; no files changed")
        old = _read(backup / filename) if item["before"] is not None else None
        if _hash(old) != item["before"]:
            raise ValueError("Damaged config backup; no files changed")
        plans.append((path, current, old, item["mode"]))
    for path, expected, old, mode in plans:
        if _read(path) != expected:
            raise RuntimeError("Config changed during rollback")
        path.unlink() if old is None else _atomic(path, old, mode)
    print(f"Restored {len(plans)} SearXNG config file(s); backups retained.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, default=Path("/config"))
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--repair", action="store_true")
    actions.add_argument("--check-repair", action="store_true")
    actions.add_argument("--restore", metavar="BACKUP_ID")
    args = parser.parse_args(argv)
    try:
        if args.restore:
            restore(args.folder, args.restore)
        elif args.repair or args.check_repair:
            repair(args.folder, check_only=args.check_repair)
        else:
            made = initialize(args.folder)
            print("SearXNG private configuration created" if made else "SearXNG existing configuration retained; use --repair for the docs/guides hotfix")
        return 0
    except Exception as exc:
        # Never dump settings or secret values from parser exception messages.
        print(f"SearXNG configuration STOPPED: {str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

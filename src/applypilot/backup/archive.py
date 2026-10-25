"""Allowlisted files and a consistent PostgreSQL dump for restic."""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
import shutil
from applypilot import postgres

from .common import canonical, environment
from applypilot.reasoning.policies import CHECKPOINT, export_bundle
from applypilot.reasoning.settings import DSPY_TASKS

POLICY_FILES = {f"ai-programs/{task}.json" for task in DSPY_TASKS} | {"learning/settings.json", CHECKPOINT}

CORE = {"profile.json", "searches.yaml", "searches.target.yaml", "resume.txt", "resume.pdf", "resume.docx",
        "impact_ledger.json", "company_limits.yaml", "discovery_runtime.yaml", "unattended.json",
        "evidence.json", "public_evidence.json", "impact_signal_cache.json", "manual_actions.md",
        "greenhouse_slugs_runtime.json", "free_sources_mcp.json", "portable_settings.json"} | POLICY_FILES
STORES = set(postgres.STORE_PATHS)
DATABASE_DUMP = "databases.pgdump"
DBS = {DATABASE_DUMP}
TREES = {"tailored_resumes", "cover_letters", "tracking", "impact_briefs", "evidence", "diary", "files", "successful_paths"}
SETUP = {"setup/draft.json", "setup/sources.json", "setup/github.json"}
SUFFIXES = {".json", ".yaml", ".yml", ".md", ".txt", ".pdf", ".tex", ".docx", ".csv", ".html",
            ".png", ".jpg", ".jpeg", ".webp"}
DENY = re.compile(r"(^\.|credentials?|cookies?|oauth|secrets?|tokens?|password|\.pem$|\.key$|\.env)", re.I)
MAX_FILES = 100_000
GIT_LIMIT = 8 * 1024 * 1024


def allowed(name: str) -> bool:
    p = PurePosixPath(name)
    if not name or "\\" in name or ":" in name or str(p) != name or p.is_absolute() or any(x in {"..", "."} for x in p.parts):
        return False
    if any(DENY.search(x) for x in p.parts):
        return False
    return name in CORE | DBS | SETUP or (p.parts[0] in TREES and p.suffix.lower() in SUFFIXES) or (
        name.startswith("setup/uploads/") and p.suffix.lower() == ".pdf")


def inventory(root: Path, include_documents=True) -> dict[str, Path]:
    result = {}
    for name in sorted(CORE | STORES | SETUP):
        path = root / name
        if path.exists():
            result[name] = path
    for name in sorted(TREES | {"setup/uploads"}):
        if name == "files" and not include_documents:
            continue
        directory = root / name
        if directory.is_symlink():
            raise ValueError("Backup will not follow symlinks: " + name)
        if directory.exists():
            for path in directory.rglob("*"):
                rel = path.relative_to(root).as_posix()
                if path.is_symlink():
                    raise ValueError("Backup will not follow symlinks: " + rel)
                if path.is_file() and allowed(rel):
                    result[rel] = path
    for name, path in result.items():
        if (name not in STORES and not allowed(name)) or (not path.is_dir() if name in STORES else not path.is_file()) or any(p.is_symlink() for p in [path, *path.parents] if p != root.parent):
            raise ValueError("Unsafe backup path: " + name)
    if len(result) > MAX_FILES:
        raise ValueError("Snapshot has too many files; archive old generated documents separately")
    return result


def postgres_snapshot(sources, target: Path, *, report=lambda **kw: None):
    report(phase='snapshot', message='Creating one PostgreSQL snapshot of all selected stores')
    postgres.dump_stores(sources, target, compress=False)
    postgres.validate_dump_schemas(target, expected_schemas=[postgres.namespace(source) for source in sources])


def database_schemas(name: str, path: Path, root: Path) -> tuple[str, ...]:
    if name != DATABASE_DUMP:
        raise ValueError("Unsupported database archive")
    return postgres.validate_dump_schemas(path, allowed_schemas=[
        postgres.namespace(root / store) for store in postgres.STORE_PATHS])



def copy_stable(source: Path, target: Path):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for _ in range(3):
        before = source.stat()
        with source.open("rb") as inp, target.open("wb") as out:
            shutil.copyfileobj(inp, out, 1024 * 1024)
        after = source.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) == (after.st_size, after.st_mtime_ns, after.st_ino):
            target.chmod(0o600)
            return
    raise ValueError("A file kept changing during backup: " + source.name)


def plain_safe(data: bytes, root: Path, name: str):
    """Best-effort accidental-secret detection, not a universal secret classifier."""
    text = data.decode("utf-8")
    if re.search(r"-----BEGIN .*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}", text):
        raise ValueError("Credential-like text found in plaintext Git file: " + name)
    for k, v in environment(root).items():
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|COOKIE|WEBHOOK", k) and len(str(v)) >= 12 and str(v) in text:
            raise ValueError("Configured secret found in plaintext Git file: " + name)
    if name.endswith(".json"):
        value = json.loads(text)
        def walk(v):
            if isinstance(v, dict):
                for k, item in v.items():
                    if re.fullmatch(r"password|access_token|refresh_token|api_key|client_secret|private_key", k, re.I) and item:
                        raise ValueError("Secret field found in plaintext Git file: " + name)
                    walk(item)
            elif isinstance(v, list):
                for item in v: walk(item)
        walk(value)


def portable_settings(root: Path) -> dict:
    from applypilot.onboarding.environment import ENV_HINTS, SECRET, LOCAL_ONLY
    from urllib.parse import urlsplit
    result = {}
    for k, v in environment(root).items():
        if k not in ENV_HINTS or k in LOCAL_ONLY or SECRET.search(k) or k.endswith(("_PATH", "_DIR", "_BIN", "_PYTHON", "_DB", "_COOKIES")):
            continue
        if not v or k.startswith("APPLYPILOT_BACKUP_"): continue
        if "://" in v:
            parsed = urlsplit(v)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
                continue
        result[k] = v
    return result


def static_files(root: Path) -> dict[str, bytes]:
    result = {}
    choices = (CORE - POLICY_FILES - {"resume.pdf", "resume.docx", "impact_signal_cache.json", "manual_actions.md", "free_sources_mcp.json",
        "impact_ledger.json", "evidence.json", "profile.json"})
    for name, path in inventory(root).items():
        if name not in choices:
            continue
        if path.stat().st_size > GIT_LIMIT:
            continue  # Always included in the encrypted full snapshot, never lost.
        raw = path.read_bytes()
        plain_safe(raw, root, name)
        result["files/" + name] = raw
    portable = canonical(portable_settings(root)) + b"\n"
    plain_safe(portable, root, "portable_settings.json")
    result["files/portable_settings.json"] = portable
    # Small policy updates do not require re-uploading the full private corpus.
    checkpoint = canonical(export_bundle(workspace=root)) + b"\n"
    plain_safe(checkpoint, root, CHECKPOINT)
    result["files/" + CHECKPOINT] = checkpoint
    # The full private ledger and interview material live only in the encrypted snapshot.
    return result


def stage(root: Path, destination: Path, cfg: dict, progress=lambda **kw: None) -> dict:
    paths = inventory(root, cfg["include_documents"])
    sizes = {name: postgres.store_size(path) if name in STORES else path.stat().st_size for name,path in paths.items()}
    total = sum(sizes.values())
    if total > cfg["max_snapshot_gb"] * 1024**3:
        raise ValueError("Snapshot exceeds the configured disk-size budget")
    # One plaintext staging copy plus room for restic cache and in-flight packs.
    required = total * 2 + 256 * 1024**2
    free = shutil.disk_usage(destination.parent).free
    if free < required:
        raise ValueError(
            f"Not enough free disk for a verified snapshot: source_bytes={total}, "
            f"required_bytes={required}, free_bytes={free}, shortfall_bytes={required - free}; "
            "reserve roughly 2× source size plus working space on the staging volume"
        )
    records = {}
    staged_bytes = 0
    stores = [path for name, path in paths.items() if name in STORES]
    if stores:
        target = destination / DATABASE_DUMP
        postgres_snapshot(stores, target, report=progress)
        records[DATABASE_DUMP] = {"size": target.stat().st_size}
        staged_bytes = records[DATABASE_DUMP]["size"]
        if staged_bytes > cfg["max_snapshot_gb"] * 1024**3:
            raise ValueError("Snapshot grew beyond its configured size budget")
    paths = {name: path for name, path in paths.items() if name not in STORES}
    for index, (name, path) in enumerate(paths.items()):
        target = destination / name
        copy_stable(path, target)
        records[name] = {"size": target.stat().st_size}
        staged_bytes += records[name]["size"]
        if staged_bytes > cfg["max_snapshot_gb"] * 1024**3:
            raise ValueError("Snapshot grew beyond its configured size budget")
        if index % 100 == 0:
            progress(message=f"Snapshotting {index + 1}/{len(paths)} files")
    portable = canonical(portable_settings(root)) + b"\n"
    target = destination / "portable_settings.json"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(portable); target.chmod(0o600)
    records["portable_settings.json"] = {"size": len(portable)}
    checkpoint = canonical(export_bundle(workspace=root)) + b"\n"
    target = destination / CHECKPOINT
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.write_bytes(checkpoint); target.chmod(0o600)
    records[CHECKPOINT] = {"size": len(checkpoint)}
    if sum(row["size"] for row in records.values()) > cfg["max_snapshot_gb"] * 1024**3:
        raise ValueError("Snapshot grew beyond its configured size budget")
    return records

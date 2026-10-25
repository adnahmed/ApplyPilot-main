"""Readable Git checkpoints, restic snapshots and journalled workspace recovery."""
from __future__ import annotations

from applypilot import operations as ops

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
from applypilot import postgres
import tempfile
import time

from applypilot.onboarding.storage import atomic, now, read_json, require_stopped, setup_lock, write_json
from . import archive, restic
from .common import (BRANCH, DEFAULTS, FORMAT, STATE, canonical, config, environment, folder, identity,
                     lock, operation_busy, repository, validate_checkpoint, BackupBusyError)
from .github import Remote

# Local-only control files are journaled for rollback, NEVER accepted from archives.
RESTORE_LOCAL = {".env", "setup/commit.json", "github_sync/config.json", f"github_sync/{STATE}",
                 "github_sync/identity.json", "github_sync/restic-storage.json", "github_sync/restart-required.json"}


def status(root: Path) -> dict:
    cfg = config(root)
    state = read_json(folder(root) / STATE, {})
    env = environment(root)
    from applypilot.integrations.github_auth import backup_auth_status
    selected = backup_auth_status(root, cfg, env)
    active = operation_busy(root)
    health = read_json(folder(root) / "repository-health.json", {})
    historical_lock = state.get("error_code") == "restic_repository_busy" or any(
        item.get("diagnostics", {}).get("return_code") == 11
        for item in (state.get("error_details") or {}).get("exception_chain", []) if isinstance(item, dict))
    if (not active and historical_lock and health.get("status") == "accessible"
            and float(health.get("checked_time", 0)) > float(state.get("failed_at", 0))):
        state = {**state, "status": "recovered", "previous_error": state.get("error"), "error": None,
                 "error_code": None, "progress": None, "message": health.get("message")}
    if state.get("status") == "running" and not active:
        state = {**state, "status": "interrupted", "progress": None,
                 "message": "The previous backup was interrupted. The next snapshot reuses encrypted data already uploaded."}
    latest = float(state.get("snapshot_time", 0))
    from .reporting import report
    return {"config": cfg, "branch": BRANCH, "state": state, "auth": selected, "readiness": readiness(root),
            **report(root, cfg, state),
            "repository_health": health,
            "github_cli": shutil.which("gh") is not None,
            "token_saved": bool(env.get("APPLYPILOT_BACKUP_TOKEN")),
            "restore": read_json(folder(root) / "restore-active.json", {}),
            "snapshot_age_seconds": max(0, time.time() - latest) if latest else None,
            "snapshot_overdue": cfg["enabled"] and (not latest or time.time() - latest > cfg["snapshot_hours"] * 7200),
            "plaintext": "Selected configuration, policy checkpoints and diary files are readable in private Git. Restic stores full snapshots in private Releases; repository read access permits recovery.",
            "engine": "restic", "access_mode": "github",
            "policies": {"checkpoint": archive.CHECKPOINT, "encrypted": False,
                         "full_snapshot_includes_learning": Path(env.get("APPLYPILOT_LEARNING_DIR") or str(root / "learning")).expanduser().resolve() == (root / "learning").resolve(),
                         "manual_export": "applypilot policy export policies.json"},
            "excluded": [".env and OAuth credentials", "browser sessions/cookies", "logs, operational telemetry and local backups", "arbitrary external paths"],
            "active": active,
            "runtime_build": "restic-account-v1",
            "schedule_note": "Automatic sync runs while autopilot or the dashboard process is running. Impact writes, policy promotion, rollback and import request a checkpoint too."}


def readiness(root: Path) -> dict:
    """Local prerequisites only; no network calls or secret values."""
    cfg=config(root)
    problems=[]
    if not cfg["repository"]: problems.append("Choose your private backup repository")
    if not cfg["enabled"]: problems.append("Authorize and save GitHub backups first")
    from .tools import tool_status
    tools = tool_status(root)
    problems.extend(tools["errors"])
    return {"ready":not problems,"problems":problems,"tools":tools}


def require_ready(root: Path) -> None:
    check=readiness(root)
    if not check["ready"]:
        raise ValueError("; ".join(check["problems"]))


def configure(root: Path, data: dict) -> dict:
    if set(data) - set(DEFAULTS) - {"confirm_private_upload"}:
        raise ValueError("Unknown backup setting")
    old = config(root)
    cfg = old | {k: v for k, v in data.items() if k in DEFAULTS}
    # Validate every submitted setting before creating a remote repository or
    # changing authentication. A bad retention number must never create one.
    for key, low, high in (("static_minutes",1,1440),("snapshot_hours",1,168),("keep_snapshots",3,365),("max_snapshot_gb",1,512)):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be from {low} to {high}")
    if type(cfg["enabled"]) is not bool or type(cfg["include_documents"]) is not bool:
        raise ValueError("Checkbox settings must be true/false")
    if cfg.get("auth_mode") not in {"auto", "github_cli", "token"}:
        raise ValueError("Invalid GitHub backup authentication mode")
    if not isinstance(cfg.get("github_account"), str) or (cfg["github_account"] and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", cfg["github_account"])):
        raise ValueError("Invalid GitHub backup account")
    if not isinstance(cfg.get("repository"), str):
        raise ValueError("Use a GitHub repository in owner/name format")
    cfg["repository"] = repository(cfg["repository"]) if cfg["repository"].strip() else ""
    state = read_json(folder(root) / STATE, {})
    if old["repository"] and old["repository"] != cfg["repository"] and state.get("head"):
        raise ValueError("This workspace is bound to its recovery repository. Restore into a fresh workspace to switch repositories.")
    newly_authorized = (not old["enabled"] or not cfg["repository"] or cfg["repository"] != old["repository"]
                        or cfg["github_account"] != old["github_account"])
    if cfg["enabled"] and newly_authorized and data.get("confirm_private_upload") is not True:
        raise ValueError("Confirm uploading your personal data to this private repository")
    expected = old
    if cfg["enabled"] and not cfg["repository"]:
        from applypilot.integrations.github_auth import backup_auth_status, backup_token, missing_backup_credential
        from applypilot.onboarding.github_connect import create_backup_repository, verify_identity
        selected = backup_auth_status(root, cfg)
        if selected["source"] == "token":
            token = backup_token(root, cfg)
            if not token:
                raise missing_backup_credential("token")
            login = verify_identity(token)
            if cfg["github_account"] and login.casefold() != cfg["github_account"].casefold():
                raise ValueError("The selected backup token belongs to a different GitHub account")
            cfg.update(repository=login + "/applypilot-backup", github_account=login)
        else:
            # This existing helper owns its own backup lock and browser login.
            # Call it before acquiring the configuration write lock below.
            prepared = create_backup_repository(root, {"confirm": True, "expected_account": selected["account"]})
            if prepared.get("already_configured"):
                raise ValueError("Backup settings changed while preparing storage. Reload before saving.")
            expected = config(root)
            cfg.update(repository=prepared["repository"], github_account=expected["github_account"],
                       auth_mode=expected["auth_mode"])
    with lock(root):
        if config(root) != expected:
            raise ValueError("Backup settings changed while preparing storage. Reload before saving.")
        if cfg["enabled"]:
            if newly_authorized or cfg["auth_mode"] != old["auth_mode"]:
                # A published recovery repository must never be recreated after
                # credential loss. Preparation is restricted to new destinations.
                can_create = (data.get("confirm_private_upload") is True
                              and not read_json(folder(root) / STATE, {}).get("head"))
                remote = Remote(root, cfg["repository"], cfg=cfg, create_missing=can_create)
                try:
                    remote.require_write_access()
                    cfg["github_account"] = remote.login
                    cfg["auth_mode"] = remote.auth_source
                finally:
                    remote.close()
        write_json(folder(root) / "config.json", cfg)
    return status(root)


def _state(root, **kwargs):
    path = folder(root) / STATE
    data = read_json(path, {}) | kwargs
    write_json(path, data)
    return data



def record_failure(root: Path, exc: Exception):
    """Persist failure evidence and an owner notification, never a retry clock."""
    if isinstance(exc, BackupBusyError): return
    if getattr(exc, "_backup_failure_recorded", False): return
    try: exc._backup_failure_recorded = True
    except Exception: pass
    state = read_json(folder(root) / STATE, {})
    failures = int(state.get("failures", 0)) + 1
    from applypilot.integrations.github_auth import GitHubCredentialError
    from applypilot.onboarding.diagnostics import explain
    from applypilot.operations.runtime import exception_details, sanitize
    env = environment(root)
    detail = getattr(exc, "details", None) or explain(exc, component="backup_sync")
    message = str(exc) if isinstance(exc, (ValueError, GitHubCredentialError)) else detail.get("error") or str(exc)
    diagnostics = exception_details(exc, secrets=env)
    busy = isinstance(exc, restic.ResticBusyError)
    _state(root, status="busy" if busy else "authentication_required" if isinstance(exc, GitHubCredentialError) else "error", error=sanitize(message, 700, secrets=env),
           error_code=detail.get("code"), error_details=diagnostics, progress=None, failed_at=time.time(), failures=failures)
    from .reporting import record
    record(root, status="busy" if busy else "failed", error=sanitize(message, 1800, secrets=env))
    if busy:
        return
    url = env.get("APPLYPILOT_ALERT_WEBHOOK", "").strip()
    if not url or time.time()-float(state.get("alert_attempt_at",0)) < 3600: return
    _state(root, alert_attempt_at=time.time())
    try:
        from urllib.parse import urlsplit
        from applypilot.referrals.store import public_url
        import httpx
        public_url(url)
        if urlsplit(url).scheme != "https": raise ValueError("Owner webhook must use HTTPS")
        with httpx.Client(timeout=10,follow_redirects=False,trust_env=False) as client:
            response = client.post(url,json={"text": "ApplyPilot: GitHub backup needs attention. Open Backups & recovery. Last completed sync: " + str(state.get("synced_at") or "none") + ". The local workspace is still available."})
            if not 200 <= response.status_code < 300: raise ValueError("Owner webhook rejected notification")
        _state(root, alerted_at=time.time(), alert_error=None)
    except Exception as exc:
        ops.capture(exc, component_name="backup", operation="alert", stage="backup", severity="warning")
        _state(root, alert_error="Backup alert could not be delivered; local dashboard status retained.")


def _head(remote, local, ids):
    head = remote.head()
    if head is None:
        if local.get("head"):
            raise ValueError("Remote recovery branch disappeared; refusing to silently replace it")
        return None, None
    meta = validate_checkpoint(remote.metadata(head))
    if meta.get("workspace") != ids["workspace"]:
        raise ValueError("This repository already has another workspace. Use Restore, not Sync, on this computer.")
    if local.get("head") != head and meta.get("device") != ids["device"]:
        raise ValueError("Remote backup advanced on another device. Automatic database merging is not supported.")
    return head, meta



def open_remote(root, name, *, progress=lambda **kw: None, interactive=False, remote_factory=Remote):
    from applypilot.integrations.github_auth import GitHubCredentialError, backup_auth_status
    try:
        return remote_factory(root, name)
    except GitHubCredentialError:
        selected = backup_auth_status(root)
        if not interactive or selected["source"] != "github_cli":
            raise
        account = selected.get("account")
        if not account:
            raise ValueError("Connect your intended GitHub account once before authorizing backup recovery.")
        progress(message="GitHub approval is needed. Approve the saved account; this backup will continue automatically.",
                 phase="authentication_required")
        from applypilot.onboarding.github_connect import connect
        connect(root, {"consent": True, "expected_account": account}, progress)
        return remote_factory(root, name)


def check(root: Path, progress=lambda **kw: None) -> dict:
    cfg = config(root)
    remote = open_remote(root, cfg["repository"], progress=progress, interactive=True)
    try:
        head = remote.head()
        meta = validate_checkpoint(remote.metadata(head)) if head else None
        checked = False
        if meta and (meta.get("snapshot") or {}).get("engine") == "restic":
            engine = restic.Repository(root, cfg, meta["workspace"], remote.token, progress, snapshot=meta["snapshot"])
            engine.check()
            checked = True
        result = {"repository": cfg["repository"], "private": True, "branch": BRANCH,
                  "auth_source": remote.auth_source, "github_account": remote.login,
                  "head": head, "recovery_available": bool(meta and meta.get("snapshot")),
                  "created_at": meta.get("created_at") if meta else None,
                  "snapshot": meta.get("snapshot") if meta else None,
                  "integrity_checked": checked,
                  "note": "Encrypted repository integrity verified." if checked else
                          "Read access verified. Write access is proven by the first completed Sync."}
        _state(root, connection=result)
        return result
    finally:
        remote.close()


@ops.observe('backup', 'sync', stage='backup')
def sync(root: Path, *, full=False, automatic=False, progress=lambda **kw: None, remote_factory=Remote) -> dict:
    with lock(root, required=False) as acquired:
        if not acquired:
            raise BackupBusyError("Another backup/recovery operation is using the repository; retry this finite operation")
        original_progress = progress
        def progress(**kw):
            if kw.get("phase") == "authenticate":
                _state(root, error=None, error_code=None, error_details=None)
            _state(root, status="running", progress=kw.get("message"), phase=kw.get("phase"),
                   transfer={k: v for k, v in kw.items() if k in {"received_bytes", "expected_bytes", "transfer_attempt", "asset_index", "asset_count"}},
                   progress_at=time.time())
            original_progress(**kw)
        if (folder(root) / "restore-active.json").exists():
            raise ValueError("Finish or roll back interrupted recovery before syncing")
        cfg = config(root)
        if automatic and not cfg["enabled"]: return {"status": "disabled"}
        if not cfg["enabled"]:
            raise ValueError("Enable GitHub backups and confirm private-data upload first")
        state = read_json(folder(root) / STATE, {})
        ids = identity(root)
        remote = None
        try:
            progress(message="Checking backup authentication", phase="authenticate")
            remote = open_remote(root, cfg["repository"], progress=progress, interactive=not automatic, remote_factory=remote_factory)
            if hasattr(remote, "require_write_access"):
                remote.require_write_access()
            progress(message="Checking private repository and recovery lineage")
            head, previous = _head(remote, state, ids)
            full = full or not previous or not previous.get("snapshot")
            static_started = time.time()
            static = archive.static_files(root)
            static_hash = hashlib.sha256(canonical({k: hashlib.sha256(v).hexdigest() for k, v in static.items()})).hexdigest()
            if not full and previous.get("static_hash") == static_hash:
                _state(root, checked_at=static_started, status="up_to_date", error=None, error_code=None,
                       error_details=None, progress=None)
                return {"status": "up_to_date"}
            if sum(len(v) for v in static.values()) > 32 * 1024**2 or len(static) > 5000:
                raise ValueError("Plain Git checkpoint exceeds 32 MiB/5000 files; archive older diary entries before syncing")
            snapshot = previous.get("snapshot") if previous else None
            snapshot_time = float(state.get("snapshot_time", 0))
            engine = None
            if full:
                engine = restic.Repository(root, cfg, ids["workspace"], remote.token, progress, snapshot=snapshot)
                if head is None:
                    remote.ensure_repository()
                engine.ensure(initialize=not snapshot)
                snapshot_time = time.time()
                snapshot = engine.backup(ids["device"])
            if full:
                # Refresh after long snapshot/upload work: never overlay an older profile on a newer archive.
                static_started = time.time()
                static = archive.static_files(root)
                static_hash = hashlib.sha256(canonical({k: hashlib.sha256(v).hexdigest() for k, v in static.items()})).hexdigest()
            # Separate small Git checkpoint. Restore overlays these newer static files on the full snapshot.
            previous_static = set((previous or {}).get("files", {})) | set((previous or {}).get("static_deleted", []))
            deleted = sorted(name for name in previous_static if name.startswith("files/") and
                             not (root / name.removeprefix("files/")).exists() and name not in static)
            meta = {"format": FORMAT, **ids, "created_at": now(), "static_hash": static_hash,
                           "static_deleted": deleted,
                           "files": {k: {"size": len(v), "sha256": hashlib.sha256(v).hexdigest()} for k, v in static.items()},
                           "snapshot": snapshot}
            progress(message="Committing readable profile, searches and diary")
            new_head = remote.commit(static, meta, head)
            _state(root, head=new_head, status="synced", checked_at=static_started, synced_at=now(), snapshot=snapshot,
                   snapshot_time=snapshot_time, error=None, failures=0, progress=None, phase=None, transfer={},
                   error_code=None, error_details=None)
            warning = None
            maintenance = None
            if engine is not None:
                try:
                    if remote.head() == new_head:
                        maintenance = {"at": now(), "status": "completed",
                                       **(engine.prune(cfg["keep_snapshots"], snapshot["id"]) or {})}
                    else:
                        maintenance = {"at": now(), "status": "deferred", "reason": "The remote checkpoint changed during this backup."}
                except Exception as exc:
                    ops.capture(exc, component_name="backup", operation="maintenance", stage="backup", severity="warning")
                    from applypilot.operations.runtime import sanitize
                    maintenance = {"at": now(), "status": "deferred", "error": sanitize(exc, 1800)}
                    warning = "Backup succeeded; repository maintenance was deferred: " + sanitize(exc, 700)
            _state(root, status="synced", progress=None, phase=None, warning=warning,
                   **({"maintenance": maintenance} if maintenance else {}))
            from .reporting import record
            record(root, status="synced", head=new_head, snapshot=snapshot if full else None, maintenance=maintenance)
            return {"status": "synced", "head": new_head, "snapshot": snapshot, "warning": warning, "checkpoint_verified": True}
        except Exception as exc:
            record_failure(root, exc)  # Evidence only; the Activity caller owns recovery.
            raise
        finally:
            if remote is not None:
                remote.close()


def list_points(root, progress=lambda **kw: None):
    cfg = config(root); remote = Remote(root, cfg["repository"])
    try:
        rows = []
        head = remote.head()
        meta = validate_checkpoint(remote.metadata(head)) if head else None
        if meta and (meta.get("snapshot") or {}).get("engine") == "restic":
            engine = restic.Repository(root, cfg, meta["workspace"], remote.token, progress, snapshot=meta["snapshot"])
            rows.extend({"id": row["id"], "tag": "restic:" + row["id"], "created_at": row["time"],
                         "draft": False, "engine": "restic",
                         "bytes": (row.get("summary") or {}).get("total_bytes_processed"),
                         "files": (row.get("summary") or {}).get("total_files_processed"),
                         "data_added": (row.get("summary") or {}).get("data_added"),
                         "data_added_packed": (row.get("summary") or {}).get("data_added_packed")} for row in engine.snapshots())
            try:
                _state(root, remote_storage=remote.storage_usage(restic.namespace(meta["workspace"])), remote_storage_error=None)
            except Exception as exc:
                from applypilot.operations.runtime import sanitize
                _state(root, remote_storage_error=sanitize(exc, 1800))
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        _state(root, points=rows)
        return {"points": rows}
    finally: remote.close()


def require_idle(root):
    from applypilot.control import require_offline_maintenance
    require_offline_maintenance(root)


from .safety import assert_start_allowed as assert_start_allowed


def _rebase(value, old, new):
    if isinstance(value, dict): return {k: _rebase(v, old, new) for k, v in value.items()}
    if isinstance(value, list): return [_rebase(v, old, new) for v in value]
    if isinstance(value, str) and old and (value == old or value.startswith(old + "/") or value.startswith(old + "\\")):
        return new + value[len(old):].replace("\\", "/")
    return value


def _prepare_restored(staged, source_root, root):
    # Recovery is not fresh authorization to dispatch an old outbox.
    path = staged / "unattended.json"
    if path.exists():
        cfg = json.loads(path.read_bytes()); cfg["enabled"] = False; write_json(path, cfg)
    for path in staged.rglob("*.json"):
        # Permanent observations are immutable evidence, not machine configuration.
        if path.name == "impact_ledger.json" or "diary" in path.relative_to(staged).parts: continue
        # Policy revision hashes and source-grounded instructions are immutable;
        # never rewrite them as if they were local configuration paths.
        if path.relative_to(staged).parts[0] in {"learning", "ai-programs"}: continue
        try:
            data = json.loads(path.read_bytes())
            rebased = _rebase(data, source_root, str(root.resolve()))
            if rebased != data: write_json(path, rebased)
        except (ValueError, UnicodeError): pass
    checkpoint = staged / archive.CHECKPOINT
    policy_bundle = None
    if checkpoint.exists():
        from applypilot.reasoning.policies import load
        policy_bundle = load(checkpoint)
    return {"held_applications":0,"held_messages":0,"policies_restored":None}, policy_bundle


def _sanitize_restored_database(root, source_root):
    db = root / "main.pgstore"
    counts = {"held_applications": 0, "held_messages": 0, "policies_restored": None}
    if db.exists():
        with closing(postgres.connect(db)) as conn, conn:
            tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
            if "referral_auto_grants" in tables:
                conn.execute("UPDATE referral_auto_grants SET revoked_at=? WHERE revoked_at IS NULL", (now(),))
            if "referral_settings" in tables:
                for rid, text in conn.execute("SELECT id,config_json FROM referral_settings").fetchall():
                    cfg = json.loads(text); cfg["mode"] = "draft"
                    conn.execute("UPDATE referral_settings SET config_json=? WHERE id=?", (json.dumps(cfg),rid))
            if "referral_outbox" in tables:
                # Preserve uncertain attempts; never convert them back into sendable drafts.
                counts["held_messages"] = conn.execute("UPDATE referral_outbox SET state='cancelled', error_kind='restored_snapshot_hold' WHERE state IN ('draft','approved','deferred','pending')").rowcount
                conn.execute("UPDATE referral_outbox SET state='send_unknown', error_kind='restored_snapshot_uncertain' WHERE state='sending'")
            if "jobs" in tables:
                columns = {r[1] for r in conn.table_info('jobs')}
                if {"apply_status", "apply_error", "agent_id", "state"} <= columns:
                    pending = "apply_status IN ('in_progress','failed') AND COALESCE(state,'')!='applied'"
                    if "applied_at" in columns:
                        pending += " AND applied_at IS NULL"
                    if "application_attempts" in tables:
                        attempt_columns = {r[1] for r in conn.table_info('application_attempts')}
                        if "browser_ambiguous_action_at" not in attempt_columns:
                            conn.execute("ALTER TABLE application_attempts ADD COLUMN browser_ambiguous_action_at TEXT")
                        conn.execute("UPDATE application_attempts SET browser_ambiguous_action_at="
                                     "COALESCE(browser_ambiguous_action_at,?) WHERE status IN "
                                     "('prepared','side_effect_intent','verification_required','verification_quarantined') "
                                     "AND job_url IN (SELECT url FROM jobs WHERE " + pending + ")", (now(),))
                    counts["held_applications"] = conn.execute(
                        "UPDATE jobs SET apply_status='failed',state='verification_required',"
                        "apply_error='Restored snapshot: submission outcome requires reconciliation',"
                        "agent_id=NULL WHERE " + pending
                    ).rowcount
                # Rebase absolute output paths, not URLs or free-text claims.
                for col in columns:
                    if col.endswith("_path"):
                        quoted = '"' + col.replace('"','""') + '"'
                        for job_url, value in conn.execute(f"SELECT url,{quoted} FROM jobs WHERE {quoted} IS NOT NULL").fetchall():
                            new = _rebase(value, source_root, str(root.resolve()))
                            if new != value: conn.execute(f"UPDATE jobs SET {quoted}=? WHERE url=?", (new,job_url))
    return counts


def restore(root: Path, data: dict, progress=lambda **kw: None, remote_factory=Remote):
    if data.get("confirm_restore") != "RESTORE":
        raise ValueError("Confirm RESTORE: replaces saved data, retains a local rollback copy, and disables old outreach authorization")
    with lock(root), lock(root, "lifecycle"), lock(root, "policies"):
        require_idle(root)
        if (folder(root) / "restore-active.json").exists():
            raise ValueError("Roll back the interrupted recovery first")
        # Full restore writes only allowlisted workspace paths. Never claim a
        # successful restore while inference is still reading a different store.
        env = environment(root)
        for name, subdir in (("APPLYPILOT_LEARNING_DIR", "learning"), ("APPLYPILOT_DSPY_ARTIFACT_DIR", "ai-programs")):
            if env.get(name) and Path(env[name]).expanduser().resolve() != (root / subdir).resolve():
                raise ValueError("Full recovery uses workspace learning/ai-programs paths. Remove custom " + name +
                                 " and restart first, or use policy-only import with your configured directory.")
        cfg = config(root); remote = open_remote(root, cfg["repository"], progress=progress, interactive=True, remote_factory=remote_factory)
        try:
            head = remote.head()
            if not head: raise ValueError("No backup branch exists in this repository")
            latest = validate_checkpoint(remote.metadata(head))
            tag = data.get("tag") or (latest.get("snapshot") or {}).get("tag")
            if not isinstance(tag, str) or not restic.SNAPSHOT.fullmatch(tag):
                raise ValueError("Choose a completed ApplyPilot snapshot")
            with tempfile.TemporaryDirectory(prefix="restore-", dir=folder(root)) as tmp:
                work = Path(tmp); staged = work / "files"; staged.mkdir(mode=0o700)
                engine = restic.Repository(root, cfg, latest["workspace"], remote.token, progress, snapshot=latest["snapshot"])
                manifest = engine.restore(tag, work)
                if not data.get("tag"):
                    # Most recent static checkpoint can be newer than the database snapshot; preserve both timestamps.
                    paths = remote.files(head)
                    for name in latest.get("static_deleted", []):
                        local = name.removeprefix("files/")
                        if not name.startswith("files/") or not archive.allowed(local) or local in archive.DBS:
                            raise ValueError("Invalid static deletion marker")
                        (staged / local).unlink(missing_ok=True)
                    for name, info in latest["files"].items():
                        if not name.startswith("files/"): continue  # journal is a derived readable view.
                        local = name.removeprefix("files/")
                        if not archive.allowed(local) or local in archive.DBS or info["size"] > archive.GIT_LIMIT:
                            raise ValueError("Invalid static recovery path")
                        item = paths.get(name)
                        if not item or item["mode"] != "100644" or item["type"] != "blob":
                            raise ValueError("Incomplete static recovery tree")
                        raw = remote.blob(item["sha"])
                        if len(raw) != info["size"] or hashlib.sha256(raw).hexdigest() != info["sha256"]:
                            raise ValueError("Static file checksum mismatch")
                        atomic(staged / local, raw)
                counts, policy_bundle = _prepare_restored(staged, manifest["source_root"], root)
                require_idle(root)
                # No writes to remote during restore; disable scheduler until owner explicitly resumes it.
                cfg["enabled"] = False
                def finish():
                    if policy_bundle is not None:
                        from applypilot.reasoning.policies import install_bundle
                        counts["policies_restored"] = install_bundle(
                            policy_bundle, trust=True, replace=True, workspace=root, restore=True)
                    sanitized = _sanitize_restored_database(root, manifest["source_root"])
                    counts.update({k:v for k,v in sanitized.items() if k != "policies_restored"})
                    _restore_public_environment(root)
                    write_json(root / "setup" / "commit.json", {"state": "restored_from_github", "at": now()})
                    gate = read_json(root / "setup" / "gate.json", {})
                    if gate.get("waiting"):
                        write_json(folder(root) / "restart-required.json", {"pid": gate.get("pid"), "at": now()})
                    write_json(folder(root) / "config.json", cfg)
                    ids = {"workspace": latest["workspace"], "device": secrets.token_hex(16)}
                    write_json(folder(root) / "identity.json", ids)
                    _state(root, head=head, snapshot=latest.get("snapshot"), status="restored", snapshot_time=0,
                           restored_at=now(), restored_tag=tag, failures=0, error=None, progress=None)
                rollback = install(root, staged, finalize=finish)
                return {"restored": True, "tag": tag, "static_checkpoint": latest["created_at"] if not data.get("tag") else manifest["created_at"],
                        "database_checkpoint": manifest["created_at"], "rollback": rollback, **counts,
                        "note": "Saved profile, searches and history restored. Restart autopilot, reconnect local accounts, reauthorize outreach, then enable backups on this device. Data created after the selected checkpoints cannot be recovered."}
        finally: remote.close()


def _restore_public_environment(root):
    from applypilot.onboarding.environment import ENV_HINTS, SECRET, LOCAL_ONLY, validate_key
    from dotenv import dotenv_values, set_key
    path = root / ".env"
    if path.is_symlink(): raise ValueError("Environment file must not be a symlink")
    saved = dotenv_values(path)
    restored = read_json(root / "portable_settings.json", {})
    with setup_lock(root):
        if not path.exists(): atomic(path, b"")
        for k, v in restored.items():
            if k in saved or k not in ENV_HINTS or k in LOCAL_ONLY or SECRET.search(k): continue
            try: validate_key(k, v)
            except ValueError: continue
            set_key(str(path), k, v, quote_mode="always")
        path.chmod(0o600)


def install(root: Path, staged: Path, finalize=lambda: None):
    """Journalled filesystem replacement plus a transactional PostgreSQL restore.

    Capture previous stores together before replacement and restore all selected
    schemas atomically.
    The journal supports compensating rollback across the database and files.
    Application owners must remain stopped through authorization neutralization.
    """
    postgres.require_idle()
    recovery = folder(root) / ("rollback-" + secrets.token_hex(12))
    recovery.mkdir(mode=0o700)
    all_files = {p.relative_to(staged).as_posix():p for p in staged.rglob('*') if p.is_file()}
    database_files = {name:path for name,path in all_files.items() if name in archive.DBS}
    files = {name:path for name,path in all_files.items() if name not in archive.DBS}
    current = {name:path for name,path in archive.inventory(root).items() if name not in archive.STORES}
    names = sorted(set(files)|set(current))
    controls = sorted(RESTORE_LOCAL)
    journal = {"format":"applypilot-restore-account-v1", "state":"preparing", "directory":recovery.name,
               "files":{},"started_at":now()}
    for name in names+controls:
        if not archive.allowed(name) and name not in RESTORE_LOCAL:
            raise ValueError('Invalid live restore target')
        target=root/name
        if any(p.is_symlink() for p in [target,*target.parents] if p!=root.parent):
            raise ValueError('Restore targets must not be symlinks')
        journal['files'][name]={'existed':target.exists()}
    database_schemas = {name: archive.database_schemas(name, path, root) for name, path in database_files.items()}
    selected_schemas = {schema for schemas in database_schemas.values() for schema in schemas}
    if archive.CHECKPOINT in files:
        selected_schemas.add(postgres.namespace(root / "learning/learning.pgstore"))
    stores = {name: (root / name).exists() for name in postgres.STORE_PATHS
              if postgres.namespace(root / name) in selected_schemas}
    previous_stores = [root / name for name, existed in stores.items() if existed]
    if previous_stores:
        previous = recovery / archive.DATABASE_DUMP
        postgres.dump_stores(previous_stores, previous)
        postgres.validate_dump_schemas(previous, expected_schemas=[postgres.namespace(path) for path in previous_stores])
    journal['database_bundle'] = {'stores': stores, 'applied': False}
    marker=folder(root)/'restore-active.json'
    write_json(marker,journal)
    try:
        for name in controls:
            target=root/name
            if target.exists(): archive.copy_stable(target,recovery/name)
        for name in names:
            target=root/name
            if target.exists():
                old=recovery/name;old.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                os.replace(target,old)
            if name in files:
                target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                os.replace(files[name],target)
        if stores:
            # Journal intent BEFORE restore: a crash may occur after commit.
            journal['database_bundle']['applied'] = True
            write_json(marker, journal)
        for name, path in database_files.items():
            postgres.restore_dump_schemas(path, clean=True, expected_schemas=database_schemas[name])
        for name in stores:
            with closing(postgres.connect(root / name, isolation_level=None)):
                pass
        finalize()
        write_json(recovery/'journal.json',journal|{'state':'completed'})
        marker.unlink()
    except BaseException:
        _rollback(root)
        raise
    return recovery.name


def _rollback(root):
    marker=folder(root)/'restore-active.json'
    journal=read_json(marker,{})
    if not journal:return {'rolled_back':False}
    if journal.get('format') != 'applypilot-restore-account-v1':
        raise ValueError('This recovery journal belongs to an older backup format; finish it with the matching application version.')
    directory=journal.get('directory','')
    if not re.fullmatch(r'rollback-[a-f0-9]{24}',directory):raise ValueError('Invalid recovery journal')
    recovery=folder(root)/directory
    bundle = journal.get('database_bundle', {})
    if bundle.get('applied'):
        stores = bundle.get('stores', {})
        if not isinstance(stores, dict) or any(
            name not in postgres.STORE_PATHS or type(existed) is not bool for name, existed in stores.items()
        ):
            raise ValueError('Invalid database recovery stores')
        previous_schemas = [postgres.namespace(root / name) for name, existed in stores.items() if existed]
        if previous_schemas:
            postgres.restore_dump_schemas(recovery / archive.DATABASE_DUMP, clean=True, expected_schemas=previous_schemas)
        for name, existed in stores.items():
            if not existed:
                store = root / name
                if store.is_symlink() or not store.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Invalid new store rollback path')
                metadata = store / 'namespace.json'
                if metadata.is_symlink():
                    raise ValueError('Rollback store metadata must not be a symlink')
                postgres.drop_store(store)
                if metadata.exists():
                    if read_json(metadata, {}) != {'format': 1, 'schema': postgres.namespace(store)}:
                        raise ValueError('New store metadata changed during recovery')
                    metadata.unlink()
                if store.exists():
                    store.rmdir()  # Only the empty logical store created by this restore.
    for name,info in journal['files'].items():
        if not archive.allowed(name) and name not in RESTORE_LOCAL:raise ValueError('Invalid recovery journal path')
        target=root/name;old=recovery/name
        if any(p.is_symlink() for p in [target,old,*target.parents,*old.parents] if p!=root.parent):
            raise ValueError('Rollback targets must not be symlinks')
        if old.exists():
            target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            os.replace(old,target)
        elif not info['existed']:
            target.unlink(missing_ok=True)
    marker.unlink()
    return {'rolled_back':True}


def rollback(root):
    with lock(root), lock(root, "lifecycle"), lock(root, "policies"):
        require_idle(root)
        return _rollback(root)

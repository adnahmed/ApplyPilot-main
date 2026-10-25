"""Restic owns encrypted repository objects, snapshots, integrity and retention."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath

from applypilot.onboarding.storage import now

from . import archive
from .common import environment, folder

SNAPSHOT = re.compile(r"restic:([0-9a-f]{64})")
COMPLETE = "applypilot-complete"
METADATA = "snapshot.json"
FORMAT = "applypilot-restic-account-v1"
STORAGE = "restic-storage.json"


def namespace(workspace, snapshot=None):
    base = "applypilot-store-" + workspace + "-account"
    if snapshot is not None and not isinstance(snapshot, dict):
        raise ValueError("Invalid backup storage metadata")
    value = snapshot.get("namespace", base) if snapshot else base
    if not isinstance(value, str) or not re.fullmatch(re.escape(base) + r"(?:-[0-9a-f]{16})?", value):
        raise ValueError("Backup storage namespace does not belong to this workspace")
    return value


def _option(name, value):
    # Restic parses each -o argument as CSV before parsing the backend value.
    output = io.StringIO()
    csv.writer(output, lineterminator="").writerow([f"{name}={value}"])
    return output.getvalue()


def _command_name(args):
    command = args[0] if args and args[0] in {"cat", "init", "check", "snapshots", "backup", "tag", "forget", "ls", "restore", "list", "unlock"} else "unknown"
    if command in {"cat", "list"} and len(args) > 1 and args[1] in {"config", "snapshot", "lock", "locks"}:
        return command + " " + args[1]
    return command


def _failure_details(errors, env, started, timeout):
    from applypilot.operations.runtime import stderr_details
    return {**stderr_details(errors, secrets=env),
            "elapsed_seconds": round(time.monotonic() - started, 3), "timeout_seconds": round(timeout, 3)}


class ResticError(RuntimeError):
    def __init__(self, command, code, *, diagnostics=None):
        self.returncode = code
        self.stderr = (diagnostics or {}).get("stderr", "")
        self.error_kind = {10: "configuration", 11: "storage", 12: "auth", 130: "cancelled"}.get(code, "unknown")
        explanation = {
            3: "Some data could not be processed completely.",
            10: "The encrypted repository is missing. It was not automatically replaced.",
            11: "The repository could not be locked. Check the recorded lock owner and retry when its operation finishes.",
            12: "This repository does not support GitHub account recovery.",
            130: "The operation was cancelled.",
        }.get(code, "The encrypted repository operation did not complete. This operation did not verify a completed checkpoint.")
        message = f"Restic {command} failed (exit {code}). {explanation}"
        super().__init__(message)
        self.details = {**(diagnostics or {}), "error": message, "code": "restic_command_failed", "return_code": code,
                        "command": command, "details_redacted": True, "timed_out": False}


class ResticBusyError(ResticError):
    def __init__(self, command, *, diagnostics, locks):
        super().__init__(command, 11, diagnostics=diagnostics)
        local = next((item for item in locks if item.get("local_process_alive")), None)
        if local:
            message = f"Backup storage is in use by process {local['pid']} on {local['hostname']}. It will retry automatically."
        elif locks:
            owner = locks[0]
            message = f"Backup storage is locked by process {owner.get('pid', 'unknown')} on {owner.get('hostname') or 'another device'}. It will retry automatically."
        else:
            message = "Backup storage is busy. Stale-lock recovery did not release it; it will retry automatically."
        self.args = (message,)
        self.details.update(error=message, code="restic_repository_busy", locks=locks, retry_after_seconds=60)


def _lock_owner(value, lock_id):
    import psutil
    if not isinstance(value, dict):
        raise ValueError("Invalid backup lock metadata")
    hostname = str(value.get("hostname") or "")[:255]
    pid = value.get("pid")
    created = datetime.fromisoformat(str(value.get("time") or "").replace("Z", "+00:00"))
    if created.tzinfo is None:
        raise ValueError("Backup lock timestamp has no timezone")
    age = max(0, (datetime.now(timezone.utc) - created).total_seconds())
    local = hostname.casefold() == socket.gethostname().casefold()
    alive = None
    if local and type(pid) is int and pid > 0:
        try:
            process = psutil.Process(pid)
            alive = process.is_running() and process.create_time() <= created.timestamp()
        except psutil.NoSuchProcess:
            alive = False
        except psutil.AccessDenied:
            alive = True
    return {"id": lock_id, "hostname": hostname, "pid": pid, "created_at": created.isoformat(),
            "age_seconds": round(age), "exclusive": bool(value.get("exclusive")),
            "local_process_alive": alive, "stale": alive is False or (not local and age > 1800)}


class ResticTimeoutError(TimeoutError):
    def __init__(self, command, code=None, *, diagnostics=None):
        message = f"Restic {command} exceeded its time budget. Prior checkpoints remain available."
        super().__init__(message)
        self.returncode = code
        self.stderr = (diagnostics or {}).get("stderr", "")
        self.details = {**(diagnostics or {}), "error": message, "code": "restic_command_timeout", "return_code": code,
                        "command": command, "details_redacted": True, "timed_out": True}


def _stop(process):
    """Stop only this command and its owned transport subprocesses."""
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        parent.terminate()
        for child in children:
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs([parent, *children], timeout=5)
        for child in alive:
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    process.wait(timeout=10)


class Repository:
    def __init__(self, root: Path, cfg: dict, workspace: str, token: str, progress=lambda **kw: None, *, snapshot=None):
        from .tools import tool_status
        if not isinstance(workspace, str) or not re.fullmatch(r"[0-9a-f]{32}", workspace):
            raise ValueError("Invalid backup workspace identity")
        tools = tool_status(root)
        if not tools["ready"]:
            raise ValueError("; ".join(tools["errors"]))
        self.root, self.cfg, self.workspace, self.progress = root, cfg, workspace, progress
        from applypilot.onboarding.storage import read_json
        pending = read_json(folder(root) / STORAGE, {}) if snapshot is None else {}
        if not isinstance(pending, dict):
            raise ValueError("Invalid pending backup initialization")
        if pending and pending.get("workspace") != workspace:
            raise ValueError("Pending backup initialization belongs to another workspace")
        self.namespace = namespace(workspace, snapshot if snapshot is not None else pending)
        self.deadline = time.monotonic() + 21000
        self.tools = tools["paths"]
        # Ignore unrelated restic/rclone settings, password commands and remotes.
        self.env = {k: v for k, v in environment(root).items()
                    if not k.upper().startswith(("RESTIC_", "RCLONE_"))}
        self.env.update({
            "RESTIC_REPOSITORY": "rclone:applypilot_gh:backup",
            "RESTIC_CACHE_DIR": str(folder(root) / "restic-cache"),
            "RESTIC_PROGRESS_FPS": "0.05",
            # Rclone's in-memory config sentinel is case-sensitive (Python uses "nul").
            "RCLONE_CONFIG": "NUL" if os.name == "nt" else os.devnull,
            "RCLONE_CONFIG_APPLYPILOT_GH_TYPE": "githubreleases",
            "RCLONE_CONFIG_APPLYPILOT_GH_REPOSITORY": cfg["repository"],
            "RCLONE_CONFIG_APPLYPILOT_GH_TOKEN": token,
            "RCLONE_CONFIG_APPLYPILOT_GH_RELEASE_PREFIX": self.namespace,
        })

    def lock_owners(self, *, deadline=None):
        deadline = min(self.deadline, deadline or time.monotonic() + 90)
        ids = self._run("list", "locks", "--no-lock", timeout=deadline - time.monotonic())
        if not isinstance(ids, list) or len(ids) > 64:
            raise ValueError("Backup lock inventory exceeds the inspection budget")
        return [_lock_owner(self._run("cat", "lock", lock_id, "--no-lock",
                                     timeout=deadline - time.monotonic()), lock_id) for lock_id in ids]

    def run(self, *args, cwd=None, timeout=21000, allow_missing=False, parse_output=True):
        deadline = min(self.deadline, time.monotonic() + timeout)
        kwargs = dict(cwd=cwd, allow_missing=allow_missing, parse_output=parse_output)
        try:
            result = self._run(*args, timeout=deadline - time.monotonic(), **kwargs)
        except ResticError as exc:
            if exc.returncode != 11 or args[0] in {"unlock", "list"}:
                raise
            owners = self.lock_owners(deadline=min(deadline, time.monotonic() + 90))
            # Even an old local lock remains protected while its original process lives.
            # Restic alone decides which eligible stale locks to delete; never remove-all.
            if any(owner["local_process_alive"] for owner in owners) or not any(owner["stale"] for owner in owners):
                raise ResticBusyError(_command_name(args), diagnostics=exc.details, locks=owners) from None
            self.progress(phase="unlock", message="Recovering an interrupted backup: removing stale repository locks")
            self._run("unlock", timeout=min(90, deadline - time.monotonic()), parse_output=False)
            try:
                result = self._run(*args, timeout=deadline - time.monotonic(), **kwargs)
            except ResticError as retry_error:
                if retry_error.returncode == 11:
                    remaining = self.lock_owners(deadline=min(deadline, time.monotonic() + 90))
                    raise ResticBusyError(_command_name(args), diagnostics=retry_error.details, locks=remaining) from None
                raise
            from applypilot.onboarding.storage import write_json
            write_json(folder(self.root) / "repository-health.json", {
                "namespace": self.namespace, "status": "accessible", "checked_at": now(),
                "checked_time": time.time(), "operation": _command_name(args),
                "recovered_locks": [owner for owner in owners if owner["stale"]],
                "message": "Interrupted backup locks recovered; repository access verified. Backup completion is reported separately."})
        return result

    def _run(self, *args, cwd=None, timeout=21000, allow_missing=False, parse_output=True):
        operation = _command_name(args)
        timeout = min(timeout, self.deadline - time.monotonic())
        if timeout <= 0:
            raise ResticTimeoutError(operation, diagnostics={"elapsed_seconds": 0, "timeout_seconds": 0,
                                     "stderr_captured": False, "stderr_bytes": 0, "stderr_truncated": False})
        # rclone.program is parsed by restic as a command, including on Windows.
        program = json.dumps(self.tools["rclone"].replace("\\", "/"), ensure_ascii=False)
        command = [self.tools["restic"], "--insecure-no-password", "--json",
                   "-o", _option("rclone.program", program),
                   "-o", "rclone.args=serve restic --stdio --cache-objects=false --transfers 2 --checkers 2",
                   "-o", "rclone.timeout=5m", *args]
        # Keep stdout private; retain only sanitized, bounded failure stderr.
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            started = time.monotonic()
            try:
                process = subprocess.Popen(command, cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
                                           stdout=output, stderr=errors,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except OSError as exc:
                exc.details = {**_failure_details(errors, self.env, started, timeout), "command": operation,
                               "code": "restic_start_failed", "details_redacted": True}
                raise
            try:
                while True:
                    try:
                        code = process.wait(timeout=min(20, max(.1, timeout - (time.monotonic() - started))))
                        break
                    except subprocess.TimeoutExpired:
                        if time.monotonic() - started >= timeout:
                            raise TimeoutError("Restic operation exceeded its time budget") from None
                        self.progress(phase=args[0], message="Encrypted backup operation: " + args[0],
                                      elapsed_seconds=round(time.monotonic() - started))
            except BaseException as exc:
                stop_error = None
                try:
                    _stop(process)
                except Exception as cleanup_error:
                    stop_error = cleanup_error
                diagnostics = _failure_details(errors, self.env, started, timeout)
                if isinstance(exc, TimeoutError):
                    raise ResticTimeoutError(operation, process.returncode, diagnostics=diagnostics) from stop_error
                if isinstance(exc, Exception):
                    exc.details = {**(getattr(exc, "details", None) or {}), **diagnostics,
                                   "command": operation, "return_code": process.returncode, "details_redacted": True}
                if stop_error is not None:
                    raise exc from stop_error
                raise
            if allow_missing and code == 10:
                return None
            if code != 0:
                raise ResticError(operation, code, diagnostics=_failure_details(errors, self.env, started, timeout))
            if not parse_output:
                return None
            if output.tell() > 128 * 1024**2:
                raise ValueError("Restic output exceeds the supported metadata budget")
            output.seek(0)
            data = output.read()
        if not data.strip():
            return []
        if args[:2] == ("list", "locks"):
            ids = data.decode("ascii").split()
            if any(not re.fullmatch(r"[0-9a-f]{64}", item) for item in ids):
                raise ValueError("Invalid backup lock identifier")
            return ids
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return [json.loads(line) for line in data.splitlines() if line.strip()]

    def ensure(self, *, initialize=False):
        value = self.run("cat", "config", allow_missing=initialize)
        if value is None:
            self.progress(phase="initialize", message="Initializing encrypted backup storage")
            try:
                self.run("init", "--repository-version", "2", parse_output=False)
            except ResticError as exc:
                if not initialize or "repository already contains keys" not in exc.stderr:
                    raise
                from applypilot.onboarding.storage import read_json, write_json
                old = self.namespace
                retained = read_json(folder(self.root) / STORAGE, {}).get("retained_namespaces", [])
                if not isinstance(retained, list):
                    raise ValueError("Invalid retained backup namespace inventory")
                retained = [namespace(self.workspace, {"namespace": item}) for item in retained]
                self.namespace = namespace(self.workspace) + "-" + secrets.token_hex(8)
                # Persist before creating any objects. A later attempt resumes
                # this generation; the incomplete encrypted data is untouched.
                write_json(folder(self.root) / STORAGE, {
                    "workspace": self.workspace, "namespace": self.namespace,
                    "retained_namespaces": list(dict.fromkeys([*retained, old])),
                    "reason": "incomplete_initialization", "updated_at": now()})
                self.env["RCLONE_CONFIG_APPLYPILOT_GH_RELEASE_PREFIX"] = self.namespace
                self.progress(phase="initialize", message="Incomplete storage retained; initializing a new backup generation")
                self.run("init", "--repository-version", "2", parse_output=False)

    def check(self):
        self.progress(phase="verify", message="Verifying encrypted repository data")
        self.run("check", "--read-data", parse_output=False)

    def snapshots(self):
        rows = self.run("snapshots", "--tag", COMPLETE + ",workspace:" + self.workspace)
        if not isinstance(rows, list):
            raise ValueError("Invalid restic snapshot listing")  # noqa: TRY004 - invalid external JSON
        return [row for row in rows if isinstance(row, dict) and re.fullmatch(r"[0-9a-f]{64}", str(row.get("id", "")))]

    def backup(self, device: str):
        # Stable paths let restic find the parent and reuse its file metadata.
        work = folder(self.root) / "restic-input"
        if work.is_symlink() or work.resolve().parent != folder(self.root).resolve():
            raise ValueError("Backup staging must stay within the workspace")
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(mode=0o700)
        try:
            files = archive.stage(self.root, work / "files", self.cfg, self.progress)
            metadata = {"format": FORMAT, "workspace": self.workspace, "source_root": str(self.root.resolve()),
                        "created_at": now()}
            (work / METADATA).write_text(json.dumps(metadata), encoding="utf-8")
            (work / METADATA).chmod(0o600)
            self.progress(phase="backup", message="Saving an encrypted incremental snapshot")
            result = self.run("backup", "--host", "applypilot-" + self.workspace,
                              "--tag", "applypilot,workspace:" + self.workspace + ",device:" + device,
                              "--pack-size", "64", "--", "files", METADATA, cwd=work)
            messages = result if isinstance(result, list) else [result]
            summary = next((row for row in messages if row.get("message_type") == "summary"), {})
            snapshot_id = summary.get("snapshot_id", "")
            if not re.fullmatch(r"[0-9a-f]{64}", snapshot_id):
                raise ValueError("Restic did not confirm a complete snapshot")
            self.check()
            changed = self.run("tag", "--add", COMPLETE, snapshot_id)
            changed = changed if isinstance(changed, list) else [changed]
            completed = next((row.get("new_snapshot_id") for row in changed
                              if row.get("old_snapshot_id") == snapshot_id), None)
            if not isinstance(completed, str) or not re.fullmatch(r"[0-9a-f]{64}", completed):
                raise ValueError("Restic did not confirm the verified snapshot identity")
            self.run("cat", "snapshot", completed)
            return {"engine": "restic", "tag": "restic:" + completed, "id": completed,
                    "namespace": self.namespace,
                    "created_at": metadata["created_at"], "source_root": metadata["source_root"],
                    "bytes": sum(row["size"] for row in files.values()), "files": len(files),
                    "data_added": summary.get("data_added", 0),
                    "data_added_packed": summary.get("data_added_packed")}
        finally:
            if work.exists() and not work.is_symlink() and work.resolve().parent == folder(self.root).resolve():
                shutil.rmtree(work)

    def prune(self, keep: int, current: str):
        # Let restic choose retention, but never forget the published pointer.
        plan = self.run("forget", "--dry-run", "--keep-last", str(keep), "--group-by", "",
                        "--tag", COMPLETE + ",workspace:" + self.workspace)
        remove = [row["id"] for group in plan for row in (group.get("remove") or [])
                  if row.get("id") != current and re.fullmatch(r"[0-9a-f]{64}", str(row.get("id", "")))]
        if remove:
            self.progress(phase="retention", message="Removing expired snapshots and unused encrypted data")
            self.run("forget", "--prune", *remove, parse_output=False)
        return {"removed_snapshots": len(remove), "keep_snapshots": keep}

    def restore(self, tag: str, destination: Path):
        match = SNAPSHOT.fullmatch(tag)
        if not match:
            raise ValueError("Choose a complete restic snapshot")
        snapshot_id = match[1]
        snapshot = self.run("cat", "snapshot", snapshot_id)
        if not {COMPLETE, "workspace:" + self.workspace} <= set(snapshot.get("tags", [])):
            raise ValueError("Snapshot is not a verified recovery point for this workspace")
        # Validate scope and disk budget BEFORE asking restic to write any files.
        nodes = self.run("ls", snapshot_id)
        nodes = nodes if isinstance(nodes, list) else [nodes]
        files, directories, total = {}, set(), 0
        for node in nodes:
            if node.get("message_type") != "node":
                continue
            name = node.get("path", "").removeprefix("/")
            path = PurePosixPath(name)
            if not name or "\\" in name or ":" in name or str(path) != name or any(p in {".", ".."} for p in path.parts):
                raise ValueError("Unsafe path in encrypted snapshot")
            if node.get("type") == "dir":
                directories.add(name)
                continue
            if node.get("type") != "file" or name in files:
                raise ValueError("Snapshot contains an unsupported or duplicate filesystem entry")
            if name != METADATA and (not name.startswith("files/") or not archive.allowed(name[6:])):
                raise ValueError("Snapshot contains a file outside the recovery allowlist")
            size = node.get("size")
            if type(size) is not int or size < 0 or (name == METADATA and size > 1024**2):
                raise ValueError("Invalid snapshot file size")
            files[name] = size
            total += size
        allowed_dirs = {str(parent) for name in files for parent in PurePosixPath(name).parents if str(parent) != "."}
        if directories - allowed_dirs or METADATA not in files or len(files) > archive.MAX_FILES + 1:
            raise ValueError("Snapshot has unexpected directories or missing recovery metadata")
        if total > self.cfg["max_snapshot_gb"] * 1024**3 or shutil.disk_usage(destination.parent).free < total * 2 + 256 * 1024**2:
            raise ValueError("Insufficient disk or snapshot exceeds configured restore budget")
        self.progress(phase="restore", message="Downloading and verifying the selected snapshot")
        self.run("restore", snapshot_id, "--target", str(destination), "--verify", parse_output=False)
        actual = {}
        for path in destination.rglob("*"):
            if path.is_symlink():
                raise ValueError("Restored snapshot contains a redirected path")
            if path.is_file():
                actual[path.relative_to(destination).as_posix()] = path.stat().st_size
        if actual != files:
            raise ValueError("Restored snapshot differs from its authenticated file listing")
        metadata = json.loads((destination / METADATA).read_text(encoding="utf-8"))
        if metadata.get("format") != FORMAT or metadata.get("workspace") != self.workspace:
            raise ValueError("Unsupported snapshot or mismatched workspace identity")
        if not isinstance(metadata.get("source_root"), str) or not isinstance(metadata.get("created_at"), str):
            raise ValueError("Snapshot recovery metadata is incomplete")  # noqa: TRY004 - invalid external JSON
        staged = destination / "files"
        staged.mkdir(exist_ok=True)
        for name in archive.DBS:
            if (staged / name).exists():
                archive.database_schemas(name, staged / name, self.root)
        return metadata

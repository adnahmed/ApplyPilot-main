"""Owner-triggered GitHub browser login. The official gh CLI owns OAuth storage."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time

import httpx
from .storage import now, read_json, require_stopped, setup_lock, write_json
from applypilot.integrations.github_auth import cli_env, saved_token


def status(root):
    # Presence only: no keychain or network access on every dashboard poll.
    saved = read_json(root / "setup" / "github-auth.json", {})
    from applypilot.backup.common import config
    from applypilot.integrations.github_auth import backup_auth_status
    cfg = config(root)
    return {"backup_repository": cfg["repository"], "backup_auth": backup_auth_status(root, cfg),
            "cli_installed": bool(shutil.which("gh")), "login": saved.get("login", ""),
            "verified_at": saved.get("verified_at"), "method": "GitHub CLI OAuth",
            "note": "Connect verifies the saved login first; no personal token needs to be pasted."}


def verify_identity(token):
    with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = client.get("https://api.github.com/user", headers={
            "Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"})
    if response.status_code == 401:
        raise ValueError("GitHub rejected the saved login. Use Reconnect GitHub; credentials were not printed.")
    if response.status_code != 200:
        raise RuntimeError("GitHub identity verification is unavailable (HTTP " + str(response.status_code) + "). Retry without deleting credentials.")
    login = response.json().get("login", "")
    if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", login):
        raise ValueError("GitHub did not return a valid account identity")
    return login


def connect(root, data, progress=lambda **kw: None):
    from applypilot.processes import process_guard
    with process_guard(root / "github-connect.guard"):
        return _connect(root, data, progress)


def _connect(root, data, progress):
    if data.get("consent") is not True:
        raise ValueError("Confirm connecting your own GitHub account")
    binary = shutil.which("gh")
    if not binary:
        raise ValueError("Install GitHub CLI (gh) on the machine running ApplyPilot, then click Connect. No token or OAuth app registration is required.")
    existing = read_json(root / "setup" / "github-auth.json", {})
    expected = data.get("expected_account", "")
    if expected and (not isinstance(expected, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", expected)):
        raise ValueError("Invalid GitHub account")
    login = ""
    if data.get("reconnect") is not True:
        token = saved_token(account=expected or existing.get("login", ""))
        if token:
            try:
                login = verify_identity(token)
            except ValueError:
                pass
    if not login:
        progress(message="Approve GitHub CLI in your browser. A one-time device code will appear here.",
                 auth_url="https://github.com/login/device")
        # Neither stdout nor stderr is retained. Publish only a validated device
        # code; upstream errors may contain secrets and never enter task JSON.
        proc = subprocess.Popen([binary, "auth", "login", "--hostname", "github.com",
                                 "--web", "--git-protocol", "https", "--skip-ssh-key"],
                                env=cli_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        lines = queue.Queue(maxsize=256)
        def reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                try:
                    lines.put_nowait(line[:2048])
                except queue.Full:
                    pass
            proc.stdout.close()
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 240
            while proc.poll() is None or not lines.empty():
                if time.monotonic() >= deadline:
                    raise ValueError("GitHub sign-in timed out. Click Connect to try again; no saved login was deleted.")
                try:
                    line = lines.get(timeout=.2)
                except queue.Empty:
                    continue
                match = re.search(r"\b([A-Z0-9]{4}-[A-Z0-9]{4})\b", line)
                if match and "code" in line.lower():
                    progress(message="Enter this one-time code on GitHub, then approve GitHub CLI.",
                             auth_url="https://github.com/login/device", device_code=match[1])
            if proc.returncode != 0:
                raise ValueError("GitHub sign-in did not complete. Existing credentials were not deleted; retry Connect.")
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait(timeout=5)
            thread.join(timeout=1)
            progress(auth_url=None, device_code=None)
        token = saved_token(account=expected)
        if not token:
            raise ValueError("GitHub CLI did not save a usable login")
        login = verify_identity(token)
    if expected and login.casefold() != expected.casefold():
        raise ValueError("Sign in with the existing backup account. No backup identity or repository was changed.")
    with setup_lock(root):
        write_json(root / "setup" / "github-auth.json", {"login": login, "verified_at": now()})
    return {"connected": True, "login": login, "method": "GitHub CLI OAuth", "sent": 0,
            "note": "GitHub login is ready. Backup uploads require a private repository and your explicit approval."}


def create_backup_repository(root, data, progress=lambda **kw: None):
    require_stopped(root)
    if data.get("confirm") is not True:
        raise ValueError("Confirm creating a private GitHub repository; no personal data is uploaded by this action")
    from applypilot.backup.common import config, folder, lock
    name = data.get("name", "applypilot-backup")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", name):
        raise ValueError("Choose a repository name, not a URL")
    cfg = config(root)
    if not cfg["repository"]:
        connect(root, {"consent": True, "expected_account": data.get("expected_account", "")}, progress)
    with lock(root):
        cfg = config(root)
        if cfg["repository"]:
            return {"repository": cfg["repository"], "created": False,
                    "already_configured": True, "enabled": cfg["enabled"], "uploaded": False,
                    "note": "Existing backup repository kept. Use Check repository, then Sync now. "
                            "This action did not verify access, replace the repository or upload anything."}
        account = read_json(root / "setup" / "github-auth.json", {}).get("login", "")
        if data.get("expected_account") and account.casefold() != str(data["expected_account"]).casefold():
            raise ValueError("The connected GitHub account changed while preparing backup storage; save the selected account again.")
        token = saved_token(account=account)
        if not token:
            raise ValueError("Connect GitHub first")
        login = verify_identity(token)
        full_name = login + "/" + name
        headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            existing_repo = client.get("https://api.github.com/repos/" + full_name, headers=headers)
            created = False
            if existing_repo.status_code == 200:
                repo = existing_repo.json()
            elif existing_repo.status_code == 404:
                response = client.post("https://api.github.com/user/repos", headers=headers,
                                       json={"name": name, "private": True, "auto_init": False})
                if response.status_code != 201:
                    raise ValueError("GitHub could not create the private backup repository. Existing repositories and data were not changed.")
                repo, created = response.json(), True
            else:
                raise ValueError("GitHub could not check the backup repository. Retry without deleting your login.")
        if (repo.get("full_name", "").casefold() != full_name.casefold() or repo.get("private") is not True or
                repo.get("archived") or repo.get("permissions", {}).get("push") is False):
            raise ValueError("The selected repository must be private, writable, unarchived and owned by your signed-in account.")
        cfg.update(repository=full_name, auth_mode="github_cli", github_account=login, enabled=False)
        write_json(folder(root) / "config.json", cfg)
    return {"repository": full_name, "created": created, "enabled": False, "uploaded": False,
            "note": "Private repository created. Authorize backups to start the first full snapshot; no recovery key is required."}

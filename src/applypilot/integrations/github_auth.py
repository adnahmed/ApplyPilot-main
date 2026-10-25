"""Reuse GitHub CLI OAuth without storing its token in ApplyPilot settings/logs."""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess


def cli_env(env=None):
    result = dict(os.environ if env is None else env)
    # gh otherwise refuses browser login when an environment token is present.
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_DEBUG", "DEBUG"):
        result.pop(key, None)
    result.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    return result


def saved_token(*, account="", env=None):
    """Read the credential store, never a shell, and never return tokens to HTTP."""
    binary = shutil.which("gh")
    if not binary:
        return ""
    if account and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", account):
        raise ValueError("Invalid saved GitHub account")
    argv = [binary, "auth", "token", "--hostname", "github.com"]
    if account:
        argv += ["--user", account]
    try:
        result = subprocess.run(argv, env=cli_env(env), stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=6, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    token = result.stdout.strip() if result.returncode == 0 else ""
    return token if 0 < len(token) <= 8192 and not re.search(r"\s", token) else ""


def evidence_token(root=None, env=None):
    from applypilot.config import APP_DIR
    from applypilot.onboarding.storage import read_json
    env = os.environ if env is None else env
    explicit = env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")
    if explicit:
        return explicit
    auth = read_json(Path(root or APP_DIR) / "setup" / "github-auth.json", {})
    return saved_token(account=auth.get("login", ""), env=env)


class GitHubCredentialError(RuntimeError):
    """Safe, actionable authentication failure without a token or upstream body."""

    error_kind = "auth"

    def __init__(self, message: str, *, source: str):
        super().__init__(message)
        self.details = {"error": message, "code": "github_authentication_failed",
                        "component": "github", "auth_source": source,
                        "causes": [type(self).__name__], "stderr_captured": False,
                        "details_redacted": True}


def backup_auth_status(root, cfg=None, env=None):
    """Describe selection without querying the keychain/network or returning secrets.

    Explicit modes are strict. Auto prefers an owner-selected CLI identity; a
    leftover environment token must not override that successful connection.
    An unconnected legacy workspace may still explicitly supply a backup PAT.
    Never silently try a different account/credential after an API rejection.
    """
    from applypilot.backup.common import config, environment
    from applypilot.onboarding.storage import read_json
    cfg = config(root) if cfg is None else cfg
    env = environment(root) if env is None else env
    mode = cfg.get("auth_mode", "auto")
    if mode not in {"auto", "github_cli", "token"}:
        raise ValueError("Invalid GitHub backup authentication mode")
    saved = read_json(Path(root) / "setup" / "github-auth.json", {})
    account = cfg.get("github_account", "")
    connected = saved.get("login", "")
    token_present = bool(env.get("APPLYPILOT_BACKUP_TOKEN"))
    if mode == "token":
        source = "token"
    elif mode == "github_cli":
        source = "github_cli"
    else:
        source = "github_cli" if account or connected or not token_present else "token"
    if source == "github_cli":
        account = account or connected
    return {"mode": mode, "source": source, "account": account,
            "token_saved": token_present,
            "token_ignored": source == "github_cli" and token_present}


def backup_token(root, cfg=None):
    from applypilot.backup.common import environment
    env = environment(root)
    selected = backup_auth_status(root, cfg, env)
    if selected["source"] == "github_cli":
        # gh env tokens are cleared inside saved_token. Do not fall back to a
        # PAT if the selected account is missing, revoked or unavailable.
        return saved_token(account=selected["account"], env=env)
    token = env.get("APPLYPILOT_BACKUP_TOKEN", "")
    if token and (len(token) > 8192 or re.search(r"\s", token)):
        raise GitHubCredentialError(
            "The saved backup token contains whitespace or is malformed. Save a raw repository token "
            "without a Bearer prefix, or select GitHub CLI OAuth in Backups.", source="token")
    return token


def missing_backup_credential(source):
    if source == "github_cli":
        return GitHubCredentialError(
            "The selected GitHub CLI account has no readable saved credential. Connect or reconnect "
            "GitHub in this same OS account, then retry Check repository. A saved manual token is "
            "not used in GitHub CLI mode.", source=source)
    return GitHubCredentialError(
        "Backup authentication is set to a manual token, but APPLYPILOT_BACKUP_TOKEN is empty. "
        "Save the repository token or choose GitHub CLI OAuth in Backups.", source=source)


def rejected_backup_credential(source):
    action = ("Reconnect the selected GitHub CLI account" if source == "github_cli" else
              "Replace APPLYPILOT_BACKUP_TOKEN, or select GitHub CLI OAuth and authorize it")
    return GitHubCredentialError(
        "GitHub rejected the selected backup credential (HTTP 401). " + action +
        ". Keep the existing repository. No alternate credential or account was tried; "
        "no new recovery point is verified.", source=source)

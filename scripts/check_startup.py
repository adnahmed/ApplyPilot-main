#!/usr/bin/env python3
"""Report startup prerequisites without starting workers or sending any messages.

Run from the checkout with: uv run python scripts/check_startup.py --docker
Optional --prepare-monitoring-token explicitly creates only the missing local
Prometheus bearer-token file from the gateway master key. Explicit --sync or
--repair actions may replace it; no credential values are printed.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / "docs/guides"


def _monitoring_token_source(guides):
    from dotenv import dotenv_values
    compose_env = dotenv_values(guides / ".env", encoding="utf-8-sig", interpolate=False)
    configured = os.environ.get("LITELLM_ENV_FILE") or compose_env.get("LITELLM_ENV_FILE") or ".env"
    source = Path(configured).expanduser()
    if not source.is_absolute():
        source = guides / source
    if not source.is_file():
        raise ValueError("Gateway env_file is missing; preserve/configure docs/guides/.env before preparing monitoring")
    environment = dotenv_values(source, encoding="utf-8-sig", interpolate=True)
    # Compose loads this field from env_file, NOT a same-named host environment
    # variable. Choosing a stale shell value here makes every scrape return 401.
    token = environment.get("LITELLM_MASTER_KEY")
    if not token or "${" in token or any(c in token for c in ("\n", "\r", "\x00")) or token.startswith("sk-change"):
        raise ValueError("Set a real LITELLM_MASTER_KEY in the gateway env file before preparing its monitoring token")
    if token != token.strip() or any(c.isspace() or ord(c) < 33 or ord(c) > 126 for c in token) or token[:1] in {"\"", "'"}:
        raise ValueError("Gateway master key must be an unquoted single token without whitespace")
    return token


def _write_private_token(target, token, *, replace):
    target.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(token + "\n")
        return
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def prepare_monitoring_token(guides, *, replace=False, repair_empty_dir=False):
    """Create the private scrape token, or explicitly sync it from the gateway key.

    The host copy stays mode 0600. Compose copies it into a private named volume
    readable by Prometheus's non-root user before Prometheus starts, so the host file
    does not need to be made world-readable just to satisfy a container bind mount.
    """
    target = guides / "monitoring/litellm_metrics_token"
    if target.is_symlink():
        raise ValueError("Refusing symlinked monitoring token")
    # Validate the credential BEFORE removing even an empty accidental directory.
    token = None
    if target.is_dir():
        if not repair_empty_dir:
            raise ValueError("Token path is a directory; run --repair-monitoring-token (only an EMPTY directory is repaired)")
        token = _monitoring_token_source(guides)
        try:
            target.rmdir()  # Never recursive: nonempty directories are not ours to delete.
        except OSError as exc:
            raise ValueError("Token directory is nonempty, mounted, or not removable; inspect it without deleting its contents") from exc
    if target.exists():
        if not target.is_file() or (not target.read_bytes().strip() and not replace):
            raise ValueError("monitoring/litellm_metrics_token is not a nonempty file; inspect it before replacing it")
        if not replace:
            return "Existing monitoring token preserved; use --sync-monitoring-token to explicitly replace it from the gateway key"
    token = token or _monitoring_token_source(guides)
    _write_private_token(target, token, replace=target.exists())
    return ("Synchronized the private monitoring token from the gateway key"
            if replace else "Created the missing local monitoring token; keep this file private")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", action="store_true", help="Also validate Compose configuration; do not build or start it")
    token_group = parser.add_mutually_exclusive_group()
    token_group.add_argument("--prepare-monitoring-token", action="store_true", help="Explicitly initialize a missing Prometheus token from the gateway master key")
    token_group.add_argument("--sync-monitoring-token", action="store_true", help="Explicitly replace the Prometheus token with the current gateway master key")
    token_group.add_argument("--repair-monitoring-token", action="store_true", help="Repair an empty accidental directory and sync the monitoring token; never removes directory contents")
    args = parser.parse_args(argv)
    if args.prepare_monitoring_token or args.sync_monitoring_token or args.repair_monitoring_token:
        try:
            print(prepare_monitoring_token(GUIDES,
                replace=args.sync_monitoring_token or args.repair_monitoring_token,
                repair_empty_dir=args.repair_monitoring_token))
        except ValueError as exc:
            print("CHECK: " + str(exc))  # Only our content-free validation messages.
            return 1
        except (OSError, ImportError) as exc:
            print("CHECK: Token preparation failed (" + type(exc).__name__ + "); check path permissions and python-dotenv")
            return 1
        if not args.docker:
            return 0
    problems = []
    if not (3, 11) <= sys.version_info[:2] < (3, 13):
        problems.append("Use Python 3.11 or 3.12: uv sync --python 3.12")
    for module in ("applypilot", "anthropic", "openai", "mcp", "jobspy", "github", "cryptography", "starlette", "uvicorn", "dotenv", "pypdf", "docx", "patchright", "playwright", "cloakbrowser"):
        if importlib.util.find_spec(module) is None:
            problems.append(f"Missing Python dependency: {module}; run uv sync")
    for program in ("claude", "npx", "pdflatex"):
        if not (shutil.which(program) or (os.name == "nt" and shutil.which(program + ".cmd"))):
            problems.append(f"Missing executable on PATH: {program} (needed for full application/outreach operation)")
    if args.docker:
        if not shutil.which("docker"):
            problems.append("Docker CLI unavailable; start/install Docker Desktop")
        else:
            try:
                result = subprocess.run(["docker", "compose", "-f", "docker-compose.litellm.yml", "config", "--quiet"], cwd=GUIDES, capture_output=True, timeout=30)
                if result.returncode:
                    problems.append("Compose config validation failed. Run docker compose -f docker-compose.litellm.yml config --quiet in docs/guides for details")
                else:
                    print("Compose configuration parsed; containers were not started")
            except (OSError, subprocess.TimeoutExpired):
                problems.append("Docker Compose configuration check could not complete")
            # Unified Compose reads the token from the same env_file as LiteLLM.
            # The legacy host token file is used only by standalone monitoring.
            try:
                _monitoring_token_source(GUIDES)
            except (ValueError, OSError) as exc:
                problems.append("Gateway monitoring credential is not configured: " + str(exc))
    if problems:
        for item in problems:
            print("CHECK: " + item)
        print("Preflight incomplete. The viewer may still work, but full autopilot prerequisites are not verified.")
        return 1
    print("Local prerequisites found. Live gateway/model/browser/Gmail connectivity still needs the dashboard checks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

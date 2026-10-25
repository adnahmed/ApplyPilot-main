"""Filesystem-only recovery interlock, safe for local evidence tools."""
import os
from applypilot.onboarding.storage import read_json

def assert_start_allowed(root):
    restart = read_json(root / "github_sync" / "restart-required.json", {})
    if restart.get("pid") == os.getpid():
        raise RuntimeError("Recovery completed: restart this autopilot process to open the restored database, without rerunning the setup wizard.")
    if (root / "github_sync" / "restore-active.json").exists():
        raise RuntimeError("Recovery is incomplete. Open Backups in the dashboard and roll it back before starting workers.")

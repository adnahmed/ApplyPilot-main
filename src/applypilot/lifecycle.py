"""OS-backed lifetime of a dashboard server, not a browser tab or saved PID."""
from __future__ import annotations

from pathlib import Path
import re
import secrets

from applypilot.processes import guard_held, process_guard

OWNER_ENV = "APPLYPILOT_DASHBOARD_OWNER"


def dashboard_owner_path(root: Path, owner: str) -> Path:
    # An internal identifier, never a user-supplied pathname.
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{24}", owner):
        raise ValueError("Invalid dashboard lifetime identifier")
    return Path(root) / "dashboard-owners" / (owner + ".guard")


def dashboard_owner_alive(root: Path, owner: str) -> bool:
    return guard_held(dashboard_owner_path(root, owner))


class DashboardLifetime:
    """A launch belongs to exactly one server instance, even across PID reuse.

    Guards are intentionally never unlinked. A zero-byte/old pathname does not
    mean ownership; only an OS-held lock does. An inaccessible lock fails closed.
    A replacement dashboard cannot accidentally inherit the old server's lease.
    """

    def __init__(self, root: Path):
        self.owner = secrets.token_hex(12)
        self._guard = process_guard(dashboard_owner_path(root, self.owner))
        self._guard.__enter__()
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self._guard.__exit__(None, None, None)

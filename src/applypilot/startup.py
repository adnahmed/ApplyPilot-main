"""CLI dashboard discovery: verify the actual workspace listener, never guess a port."""
from __future__ import annotations

import time
import webbrowser


def dashboard_result(command: str, result: dict, *, open_browser: bool | None = None,
                     json_output: bool = False) -> dict:
    from applypilot import config, control
    from applypilot.reasoning.learning_store import settings
    from applypilot.reasoning.settings import flag, integer
    from applypilot.web.server import existing_url

    launching = command in {"start", "resume", "restart"}
    timeout = integer("APPLYPILOT_START_DASHBOARD_WAIT_SECONDS", 20, 0, 120) if launching else 0
    deadline = time.monotonic() + timeout
    url = existing_url(config.APP_DIR)
    while not url and launching and result.get("state") in {"starting", "running"} and time.monotonic() < deadline:
        time.sleep(.2)
        result = {**result, **control.status(config.APP_DIR)}
        url = existing_url(config.APP_DIR)
    result = {**result, "dashboard_url": url, "dashboard_ready": bool(url)}
    if launching:
        cfg = settings()
        result["learning"] = {key: cfg[key] for key in ("enabled", "auto_promote", "allow_judged_promotion")}
        if not url:
            result["dashboard_warning"] = ("Dashboard not ready; no URL has been guessed. "
                "Run 'applypilot status --runtime' or inspect the local logs/autopilot-controller.log.")
        should_open = open_browser if open_browser is not None else flag("APPLYPILOT_OPEN_DASHBOARD", True) and not json_output
        if url and should_open:
            try:
                result["browser_opened"] = bool(webbrowser.open(url))
            except (OSError, webbrowser.Error):
                result["browser_opened"] = False
    return result

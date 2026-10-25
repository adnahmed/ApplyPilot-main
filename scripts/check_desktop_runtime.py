"""Check the actual desktop interpreter; optionally prepare and smoke-launch CloakBrowser.

No GitHub upload, social login, cookie export, account API or model request.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile

# Always use this checkout, not an unrelated pip-installed ApplyPilot.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from applypilot.runtime_info import runtime_info
from applypilot.onboarding.browser_runtime import sdk, prepare, failure


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-browser", action="store_true",
                        help="Use upstream verified binary download if needed, then open a temporary blank desktop browser")
    args = parser.parse_args()
    report = {"runtime": runtime_info(), "browser_launched": False, "accounts_accessed": False}
    try:
        if not (3, 11) <= sys.version_info[:2] < (3, 13):
            raise ValueError("ApplyPilot requires Python 3.11 or 3.12; use the desktop launcher")
        sdk()
        if args.prepare_browser:
            module = prepare()
            with tempfile.TemporaryDirectory(prefix="applypilot-browser-check-") as tmp:
                browser = None
                try:
                    browser = module.launch_persistent_context(tmp, headless=False, timeout=90000)
                    page = browser.pages[0] if browser.pages else browser.new_page()
                    page.goto("about:blank")
                    report["browser_launched"] = True
                except Exception as exc:
                    raise failure(exc, "launch") from None
                finally:
                    if browser is not None:
                        browser.close()
        report["ready"] = True
    except Exception as exc:
        report.update(ready=False, diagnostic=getattr(exc, "details", {"error": str(exc), "code": "runtime_incompatible"}))
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

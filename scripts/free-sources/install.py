#!/usr/bin/env python3
"""Install optional SDKs in separate environments, never into JobSpy's environment.

Run with Python 3.11/3.12. Network access is required on the user's machine.
No login, account import, API purchase, or live collection is performed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

PACKAGES = {
    "ddgs": "ddgs>=9,<10",
    "playwright": "playwright>=1.40,<2",
    "twikit": "twikit>=2.3,<3",
    "linkedin_scraper": "linkedin-scraper>=3.1.2,<4",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("providers", nargs="+", choices=sorted(PACKAGES))
    parser.add_argument("--python", default=sys.executable, help="Existing Python 3.11/3.12 executable")
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("APPLYPILOT_DIR", Path.home()/".applypilot"))/"tools")
    parser.add_argument("--no-browser", action="store_true", help="Skip the browser download for browser SDKs")
    args = parser.parse_args()
    try:
        version = json.loads(subprocess.check_output([args.python, "-c", "import sys,json;print(json.dumps(list(sys.version_info[:2])))"], text=True))
        if version not in ([3, 11], [3, 12]):
            parser.error("ApplyPilot and these workers require Python 3.11 or 3.12")
        for provider in dict.fromkeys(args.providers):
            dest = args.root.expanduser().resolve() / provider
            dest.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if os.name != "nt":
                dest.parent.chmod(0o700)
            py = dest / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            if not py.is_file():
                subprocess.run([args.python, "-m", "venv", str(dest)], check=True)
            subprocess.run([str(py), "-m", "pip", "install", PACKAGES[provider]], check=True)
            if not args.no_browser and provider in {"playwright", "linkedin_scraper"}:
                subprocess.run([str(py), "-m", "playwright", "install", "chromium"], check=True)
            frozen = subprocess.check_output([str(py), "-m", "pip", "freeze"], text=True)
            (dest / "installed-requirements.txt").write_text(frozen, encoding="utf-8")
            print(f"Installed {provider}. Record: {dest / 'installed-requirements.txt'}")
            print(f"APPLYPILOT_{provider.upper()}_PYTHON={py}")
        print("No sessions were created. Configure authorized sessions separately; see docs/free-sources/README.md.")
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Installation stopped ({type(exc).__name__}). Earlier environments may have been installed.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

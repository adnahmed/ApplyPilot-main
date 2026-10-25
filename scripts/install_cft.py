#!/usr/bin/env python3
"""Install the repository's Chrome for Testing 148 line for the current OS.

Keeps the existing install until download and extraction succeed; honors
APPLYPILOT_DIR. No browser session, profile, or login data is replaced.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

MAJOR = "148"
FEED = "https://googlechromelabs.github.io/chrome-for-testing/known-good-versions-with-downloads.json"


def platform_key(system=None, machine=None):
    system = system or platform.system()
    machine = (machine or platform.machine()).lower()
    if system == "Windows":
        return "win32" if machine in {"x86", "i386", "i686"} else "win64"
    if system == "Darwin":
        return "mac-arm64" if machine in {"arm64", "aarch64"} else "mac-x64"
    if system == "Linux" and machine in {"x86_64", "amd64"}:
        return "linux64"
    raise RuntimeError(f"No supported Chrome for Testing download for {system}/{machine}")


def binary_relative(key):
    if key.startswith("win"):
        return Path(f"chrome-{key}/chrome.exe")
    if key.startswith("mac-"):
        return Path(f"chrome-{key}/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
    return Path(f"chrome-{key}/chrome")


def latest_download(data, key):
    candidates = [v for v in data["versions"] if v["version"].startswith(f"{MAJOR}.")]
    # Do not rely on upstream feed ordering or choose a build without this OS.
    for item in sorted(candidates, key=lambda v: tuple(map(int, v["version"].split("."))), reverse=True):
        for download in item.get("downloads", {}).get("chrome", []):
            if download["platform"] == key:
                return item["version"], download["url"]
    raise RuntimeError(f"No CfT {MAJOR}.x download found for {key}")


def extract(archive, folder, key):
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            path = PurePosixPath(info.filename)
            if (path.is_absolute() or ".." in path.parts or "\\" in info.filename
                    or not path.parts or path.parts[0] != f"chrome-{key}" or ":" in info.filename):
                raise ValueError("Unsafe or unexpected Chrome archive path")
        if key.startswith("mac-"):
            # ditto preserves framework symlinks and executable permissions.
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(folder)], check=True)
        else:
            z.extractall(folder)
            if key == "linux64":
                for info in z.infolist():
                    mode = info.external_attr >> 16
                    target = folder / info.filename
                    if target.is_file() and mode & 0o111:
                        target.chmod(0o755)
    binary = folder / binary_relative(key)
    if not binary.is_file():
        raise RuntimeError(f"Chrome download did not contain {binary_relative(key)}")


def main():
    key = platform_key()
    home = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot")).expanduser().resolve()
    cft = home / "chrome-for-testing"
    with urllib.request.urlopen(FEED, timeout=60) as response:
        version, url = latest_download(json.load(response), key)
    marker = cft / f"{key}.version"
    binary = cft / binary_relative(key)
    if binary.is_file() and marker.is_file() and marker.read_text().strip() == version:
        print(f"Chrome for Testing {version} already installed at {binary}")
        return
    cft.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="download-", dir=cft) as temporary:
        staging = Path(temporary)
        archive = staging / "chrome.zip"
        print(f"Downloading Chrome for Testing {version} ({key})")
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as output:
            shutil.copyfileobj(response, output)
        extract(archive, staging, key)
        target = cft / f"chrome-{key}"
        previous = staging / "previous"
        if target.exists():
            target.rename(previous)  # Locked Windows install fails before replacing it.
        try:
            (staging / f"chrome-{key}").rename(target)
        except BaseException:
            if previous.exists():
                previous.rename(target)
            raise
        marker.write_text(version + "\n", encoding="utf-8")
    print(f"Installed Chrome for Testing {version}: {binary}")


if __name__ == "__main__":
    main()

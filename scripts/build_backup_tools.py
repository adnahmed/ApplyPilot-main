"""Bundle backup executables while uv/Hatch builds the application package."""
from __future__ import annotations

import bz2
import hashlib
import json
import os
import shutil
import subprocess
import sysconfig
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import go
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

RESTIC_VERSION = "0.19.1"


def _run(command: list[str], *, cwd=None, env=None, timeout=30) -> str:
    result = subprocess.run(
        command, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, check=False,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    if result.returncode:
        raise RuntimeError(f"Backup tool build failed: {(result.stderr or result.stdout)[-8000:]}")
    return result.stdout


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ApplyPilot-package-build"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _restic(destination: Path, operating_system: str, architecture: str) -> None:
    suffix = ".zip" if operating_system == "windows" else ".bz2"
    stem = f"restic_{RESTIC_VERSION}_{operating_system}_{architecture}"
    asset = stem + suffix
    release_url = f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}"
    with tempfile.TemporaryDirectory(prefix="restic-build-", dir=destination) as temporary:
        staging = Path(temporary)
        checksums = staging / "SHA256SUMS"
        archive = staging / asset
        print(f"Preparing restic {RESTIC_VERSION} ({operating_system}/{architecture})", flush=True)
        _download(f"{release_url}/SHA256SUMS", checksums)
        entries = [line.split() for line in checksums.read_text(encoding="ascii").splitlines()]
        expected = next((parts[0] for parts in entries if len(parts) == 2 and parts[1] == asset), None)
        if expected is None:
            raise RuntimeError(f"The official restic release has no executable for {operating_system}/{architecture}")
        _download(f"{release_url}/{asset}", archive)
        with archive.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise RuntimeError(f"SHA256 verification failed for {asset}")
        executable = staging / ("restic.exe" if operating_system == "windows" else "restic")
        if suffix == ".zip":
            with (zipfile.ZipFile(archive) as bundle, bundle.open(stem + ".exe") as source,
                  executable.open("wb") as output):
                shutil.copyfileobj(source, output)
        else:
            with bz2.open(archive, "rb") as source, executable.open("wb") as output:
                shutil.copyfileobj(source, output)
        executable.chmod(0o755)
        if json.loads(_run([str(executable), "version", "--json"]))["version"] != RESTIC_VERSION:
            raise RuntimeError("The downloaded restic executable returned an unexpected version")
        os.replace(executable, destination / executable.name)


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return
        goroot = Path(go.__file__).resolve().parent
        compiler = goroot / "bin" / ("go.exe" if os.name == "nt" else "go")
        env = dict(os.environ, GOROOT=str(goroot), CGO_ENABLED="0", GOWORK="off", GOENV="off", GOTOOLCHAIN="local")
        for setting in ("GOOS", "GOARCH", "GOFLAGS"):
            env.pop(setting, None)
        operating_system, architecture = _run([str(compiler), "env", "GOHOSTOS", "GOHOSTARCH"], env=env).split()
        supported = {"windows": {"amd64"}, "linux": {"amd64", "arm64"}, "darwin": {"amd64", "arm64"}}
        if architecture not in supported.get(operating_system, set()):
            raise RuntimeError(f"Backup tool builds are not supported for {operating_system}/{architecture}")
        root = Path(self.root)
        destination = root / "build" / "backup-tools" / sysconfig.get_platform()
        destination.mkdir(parents=True, exist_ok=True)
        _restic(destination, operating_system, architecture)
        source = root / "tools" / "rclone-githubreleases"
        with tempfile.TemporaryDirectory(prefix="rclone-build-", dir=destination) as temporary:
            executable = Path(temporary) / ("applypilot-rclone.exe" if os.name == "nt" else "applypilot-rclone")
            print("Building ApplyPilot backup storage support (the first build may take several minutes)", flush=True)
            _run([str(compiler), "build", "-mod=readonly", "-buildvcs=false", "-trimpath", "-o", str(executable), "."],
                 cwd=source, env=env, timeout=1200)
            providers = json.loads(_run([str(executable), "config", "providers"]))
            if not any(item.get("Name") == "githubreleases" for item in providers):
                raise RuntimeError("The built backup adapter is missing its GitHub Releases provider")
            os.replace(executable, destination / executable.name)
        (destination / "__init__.py").write_text('"""Backup executables bundled with ApplyPilot."""\n', encoding="utf-8")
        suffix = ".exe" if operating_system == "windows" else ""
        for field in ("force_include", "force_include_editable"):
            for name in ("__init__.py", "restic" + suffix, "applypilot-rclone" + suffix):
                build_data.setdefault(field, {})[str(destination / name)] = "applypilot_backup_bins/" + name
        build_data["pure_python"] = False
        build_data["infer_tag"] = True

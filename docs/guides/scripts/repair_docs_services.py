"""Rebuild/recreate ONLY the docs/guides services in an existing Compose project.

Default is verification-only. --apply rebuilds operations-exporter, repairs the
existing SearXNG named volume with its own backup, and recreates exporter,
Grafana and SearXNG. Never runs down, deletes volumes, changes .env, submits an
application, resets quotas, or recreates LiteLLM/Postgres/Redis/egress workers.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


class RepairError(RuntimeError):
    pass


class Compose:
    def __init__(self, root: Path, *, standalone=False, project_name=None):
        self.directory = root / "docs/guides"
        filename = "docker-compose.litellm.yml"
        if standalone:
            self.directory /= "monitoring"
            filename = "docker-compose.monitoring.yml"
        self.command = ["docker", "compose", "-f", filename]
        if project_name:
            self.command += ["--project-name", project_name]
        self.services = ["operations-exporter", "grafana"] + ([] if standalone else ["searxng"])
        self.standalone = standalone

    def run(self, *args, timeout=120, quiet=False):
        try:
            # Capture configuration and exec output so secrets are not printed accidentally.
            result = subprocess.run(self.command + list(args), cwd=self.directory,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RepairError(f"Docker operation could not complete: {type(exc).__name__}") from None
        if result.returncode:
            if quiet:
                return result
            # Do not dump resolved environment/config or complete container logs.
            raise RepairError(f"Docker command failed ({result.returncode}): docker compose {' '.join(args[:5])}. Run that command in the existing project to inspect its error.")
        return result

    def preflight(self):
        self.run("version", timeout=30)
        self.run("config", "--quiet", timeout=45)
        # A checkout with a different project name must not create an empty second stack.
        for service in self.services:
            ids = self.run("ps", "--all", "--quiet", service, timeout=30).stdout.split()
            if len(ids) != 1:
                raise RepairError(f"Expected one existing {service} container; found {len(ids)}. Select the original checkout and Compose project name. Nothing was recreated.")
        if not self.standalone:
            result = self.run("run", "--rm", "--no-deps", "research-config", "python",
                              "/init/init_searxng.py", "--check-repair", timeout=120, quiet=True)
            if result.returncode:
                # Only the initializer's known non-secret status line is surfaced.
                for line in result.stdout.splitlines():
                    if line.startswith("SearXNG configuration STOPPED:"):
                        print(line)
                raise RepairError("SearXNG repair preflight failed; no existing service or config was changed.")
        print("Existing Compose project and repair prerequisites verified.")

    def endpoint(self, service, internal_port):
        output = self.run("port", service, str(internal_port), timeout=20).stdout.strip().splitlines()
        for item in output:
            host, sep, port = item.rpartition(":")
            if sep and host in ("127.0.0.1", "[::1]", "::1") and port.isdigit():
                return f"http://{'[::1]' if host in ('::1', '[::1]') else host}:{port}"
        raise RepairError(f"{service} has no loopback-only published endpoint. Refusing a non-local probe.")

    def apply(self):
        print("Building the corrected exporter; its image runs a populated-journal self-test.")
        # Do not pull/upgrade Grafana, SearXNG, or the other stack services.
        self.run("build", "operations-exporter", timeout=900)
        if not self.standalone:
            self.run("stop", "searxng", timeout=90)
            try:
                result = self.run("run", "--rm", "--no-deps", "research-config", "python",
                                  "/init/init_searxng.py", "--repair", timeout=120, quiet=True)
                for line in result.stdout.splitlines():
                    if line.startswith(("SearXNG repaired", "SearXNG backup ID:", "SearXNG config already", "SearXNG configuration STOPPED:")):
                        print(line)
                if result.returncode:
                    raise RepairError("SearXNG config repair failed")
            except Exception:
                # Also attempt recovery after a Docker/transport timeout, not only exit-code failures.
                try:
                    self.run("start", "searxng", timeout=90, quiet=True)
                except Exception:
                    pass
                raise RepairError("SearXNG config repair failed; start of the existing container was attempted. Inspect its status and retained config backup.") from None
        self.run("up", "-d", "--no-deps", "--force-recreate", *self.services, timeout=180)
        print("Only operations-exporter, Grafana" + (" and SearXNG" if not self.standalone else "") + " were recreated.")

    def restore_searxng(self, backup_id):
        if self.standalone:
            raise RepairError("Standalone monitoring has no SearXNG service")
        self.run("stop", "searxng", timeout=90)
        try:
            result = self.run("run", "--rm", "--no-deps", "research-config", "python",
                              "/init/init_searxng.py", "--restore", backup_id, timeout=120)
            print(result.stdout.strip())
        finally:
            self.run("start", "searxng", timeout=90)

    def verify(self):
        grafana = self.endpoint("grafana", 3000)
        searxng = None if self.standalone else self.endpoint("searxng", 8080)
        deadline = time.monotonic() + 120
        last = "services have not answered"
        while time.monotonic() < deadline:
            try:
                result = self.run("exec", "-T", "operations-exporter", "python", "/app/check_operations_image.py", "--require-store", timeout=15, quiet=True)
                if result.returncode:
                    raise RepairError("Exporter unavailable or operations_store_up=0; no healthy journal has been confirmed")
                if fetch_json(grafana + "/api/health").get("database") != "ok":
                    raise RepairError("Grafana database health has not passed")
                if searxng:
                    fetch(searxng + "/healthz")
                    config = fetch_json(searxng + "/config")
                    engine_data = config.get("engines")
                    if not isinstance(engine_data, list):
                        raise RepairError("SearXNG /config did not expose a recognized engine list")
                    if any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in engine_data):
                        raise RepairError("SearXNG /config contains malformed engine entries")
                    names = {item["name"] for item in engine_data}
                    if {"ahmia", "torch"} & names:
                        raise RepairError("SearXNG still loaded a removed Tor engine")
                    if not names:
                        raise RepairError("SearXNG exposed an empty engine list")
                    self.run("exec", "-T", "searxng", "sh", "-c", "test -r /etc/searxng/limiter.toml", timeout=15)
                print("VERIFIED: exporter HTTP 200 + operations_store_up=1; Grafana database=ok" +
                      ("; SearXNG healthy with Tor engines absent and limiter.toml readable." if searxng else "."))
                print("These are service/config checks, not proof of job submissions or external search/provider success.")
                return
            except (RepairError, OSError, ValueError) as exc:
                last = str(exc) if isinstance(exc, RepairError) else type(exc).__name__
                time.sleep(3)
        raise RepairError("Runtime verification did not pass: " + last)


def fetch(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        if response.status != 200:
            raise RepairError("Local health endpoint did not return HTTP 200")
        data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise RepairError("Local health response exceeded the safety limit")
        return data


def fetch_json(url):
    value = json.loads(fetch(url))
    if not isinstance(value, dict):
        raise RepairError("Local health endpoint returned a non-object response")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Original ApplyPilot project folder")
    parser.add_argument("--standalone-monitoring", action="store_true")
    parser.add_argument("--project-name", help="Only for an existing custom Compose project name")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore-searxng", metavar="BACKUP_ID")
    args = parser.parse_args(argv)
    try:
        root = args.root.expanduser().resolve(strict=True)
        if not (root / "pyproject.toml").is_file():
            raise RepairError("Choose the original ApplyPilot project folder")
        compose = Compose(root, standalone=args.standalone_monitoring, project_name=args.project_name)
        if args.restore_searxng:
            # Do not require the config to be migratable before restoring its backup.
            compose.run("config", "--quiet", timeout=45)
            ids = compose.run("ps", "--all", "--quiet", "searxng").stdout.split()
            if len(ids) != 1:
                raise RepairError("No unique existing SearXNG container in this project")
            compose.restore_searxng(args.restore_searxng)
        else:
            if args.apply:
                compose.preflight()
                compose.apply()
            compose.verify()
        return 0
    except (RepairError, OSError) as exc:
        print(f"DOCS SERVICES NOT VERIFIED: {exc}", file=sys.stderr)
        print("Source files may be installed, but this is NOT a successful runtime verification. No volumes or quota history were deleted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

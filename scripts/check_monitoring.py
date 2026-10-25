#!/usr/bin/env python3
"""Read-only LiteLLM/monitoring diagnosis, usable from any working directory.

No model calls, no credential output, no restarts or changes to Docker resources.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import urllib.parse
import urllib.request

GUIDES = Path(__file__).resolve().parents[1] / "docs/guides"

def _json(url):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=5) as response:
        return json.loads(response.read(2_000_000))

def _docker_state(compose, service):
    result = subprocess.run([*compose, "ps", "-a", "-q", service], capture_output=True, text=True, timeout=15)
    ids = result.stdout.split()
    if result.returncode or not ids:
        print("Docker service %s: not found in selected project" % service)
        return
    for cid in ids:
        state = subprocess.run(["docker", "inspect", "--format", "{{json .State}}", cid], capture_output=True, text=True, timeout=10)
        if state.returncode:
            continue
        data = json.loads(state.stdout)
        print("Docker %s: state=%s health=%s exit_code=%s" % (service, data.get("Status"), (data.get("Health") or {}).get("Status", "n/a"), data.get("ExitCode")))
        # Don't print config/env, auth values, or arbitrary health command output.

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090")
    ap.add_argument("--project", default="guides", help="Use the same Compose project that owns the existing volumes")
    ap.add_argument("--compose-file", type=Path, default=GUIDES / "docker-compose.litellm.yml")
    args = ap.parse_args(argv)
    problems = []
    token = GUIDES / "monitoring/litellm_metrics_token"
    unified = args.compose_file.name == "docker-compose.litellm.yml"
    if unified:
        print("Monitoring token source: gateway env_file -> private named volume")
        if token.is_dir():
            print("Legacy host token directory is unused by this stack; its contents are left untouched")
    elif token.is_dir() or token.is_symlink() or not token.is_file() or not token.stat().st_size:
        print("Standalone monitoring host token is missing/invalid; prepare or repair it")
        problems.append("token_invalid")
    else:
        print("Monitoring host token: regular nonempty file (contents not printed)")
    compose_file = args.compose_file.resolve()
    compose = ["docker", "compose"]
    # Resolve interpolation from the selected stack directory, not the caller's CWD.
    env_file = compose_file.parent / ".env"
    if env_file.is_file():
        compose += ["--env-file", str(env_file)]
    compose += ["-p", args.project, "-f", str(compose_file)]
    if shutil.which("docker"):
        for service in ("litellm", "prometheus-token-init", "prometheus", "warp-egress-manager"):
            try:
                _docker_state(compose, service)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                print("Could not inspect Docker service: " + service)
    base = args.prometheus.rstrip("/")
    try:
        payload = _json(base + "/api/v1/targets")
        targets = [t for t in payload.get("data", {}).get("activeTargets", []) if t.get("labels", {}).get("job") == "litellm"]
    except (OSError, ValueError) as exc:
        print("Prometheus API unavailable (" + type(exc).__name__ + "); inspect token-init and LiteLLM separately")
        return 2
    if not targets:
        print("LiteLLM target absent from current configuration")
        problems.append("target_absent")
    for target in targets:
        print("LiteLLM target: %s health=%s" % (target.get("scrapeUrl"), target.get("health")))
        if target.get("health") != "up":
            problems.append("scrape_not_up")
        if target.get("lastError"):
            print("  last error: " + str(target["lastError"]))
        print("  last scrape: %s" % target.get("lastScrape"))
    query = urllib.parse.quote('scrape_samples_scraped{job="litellm"}')
    try:
        result = _json(base + "/api/v1/query?query=" + query).get("data", {}).get("result", [])
        print("Current scrape samples: " + (", ".join(r.get("metric", {}).get("instance", "?") + "=" + r.get("value", ["", "?"])[1] for r in result) or "no series"))
        if not any(float(r.get("value", ["", 0])[1]) > 0 for r in result):
            problems.append("no_samples")
    except (OSError, ValueError):
        problems.append("samples_query_failed")
    if unified:
        try:
            query = urllib.parse.quote('gemini_egress_region_exits')
            rows = _json(base + "/api/v1/query?query=" + query).get("data", {}).get("result", [])
            print("Gemini observed exit regions: " + (", ".join(
                row.get("metric", {}).get("status", "?") + "=" + row.get("value", ["", "?"])[1]
                for row in rows) or "no region telemetry yet"))
            if not rows:
                problems.append("region_telemetry_missing")
            else:
                eligible = [row for row in rows if row.get("metric", {}).get("status") == "eligible"]
                if not any(float(row.get("value", ["", 0])[1]) > 0 for row in eligible):
                    print("Gemini is currently blocked pending eligible exit evidence; other free providers are not globally blocked")
            query = urllib.parse.quote('time() - gemini_egress_region_scan_timestamp_seconds')
            scans = _json(base + "/api/v1/query?query=" + query).get("data", {}).get("result", [])
            if not scans or all(float(row.get("value", ["", 1e9])[1]) > 600 for row in scans):
                print("Gemini region scan is missing or stale")
                problems.append("region_scan_stale")
        except (OSError, ValueError):
            problems.append("region_query_failed")
    return 1 if problems else 0

if __name__ == "__main__":
    raise SystemExit(main())

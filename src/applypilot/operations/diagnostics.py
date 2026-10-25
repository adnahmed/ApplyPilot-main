"""Safe monitoring diagnostics: fixed loopback destinations, never model probes."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
import json
import urllib.error
import urllib.request
from .runtime import sanitize


def _get(url):
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    # Do not send local diagnostics through an HTTP(S)_PROXY environment variable.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=3) as response:
        return json.loads(response.read(2_000_000))


def check():
    def probe(name, url):
        try:
            result = _get(url)
            if name == "targets":
                return name, {"ok": True, "items": [{"job": t.get("labels", {}).get("job"),
                    "health": t.get("health"), "last_error": sanitize(t.get("lastError")),
                    "last_scrape": t.get("lastScrape"), "scrape_duration": t.get("lastScrapeDuration")}
                    for t in result.get("data", {}).get("activeTargets", [])][:50]}
            if name in {"litellm_series", "operations_series"}:
                return name, {"ok": True, "series": result.get("data", {}).get("result", [])[:10]}
            return name, {"ok": True, "database": result.get("database"), "version": result.get("version")}
        except (OSError, ValueError) as exc:
            return name, {"ok": False, "error": sanitize(exc)}
    queries = [
        ("targets", "http://127.0.0.1:9090/api/v1/targets"),
        ("grafana", "http://127.0.0.1:3000/api/health"),
        ("litellm_series", "http://127.0.0.1:9090/api/v1/query?query=count(%7B__name__%3D~%22litellm_.%2B%22%7D)"),
        ("operations_series", "http://127.0.0.1:9090/api/v1/query?query=applypilot_operations_store_up"),
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = dict(pool.map(lambda args: probe(*args), queries))
    result["notes"] = [
        "Target down with 401/403: refresh monitoring/litellm_metrics_token using the matching proxy metrics key.",
        "Connection refused/DNS: unified stack must scrape litellm:4000; standalone monitoring uses the external host target.",
        "Target up with no LiteLLM request series: verify the prometheus callback and generate ordinary pipeline traffic; this check never calls a model.",
        "Grafana health only proves its API/database is alive. Datasource and panel failures need Grafana logs/Explore.",
        "operations_store_up=0 means the shared operations directory is missing/unreadable or no patched worker has initialized it.",
    ]
    return result

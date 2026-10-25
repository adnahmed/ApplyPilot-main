"""Bounded Prometheus chart data using the installed Grafana dashboard metrics."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import math
import json
import threading
import time
from urllib.parse import urlencode
from urllib.error import HTTPError

from applypilot.operations.diagnostics import _get
from applypilot.operations.runtime import sanitize

_CACHE = {}
_LOCK = threading.Lock()
_QUERIES = {
    "requests": ("AI requests per minute", "requests/min", "sum by (litellm_model_name) (rate(litellm_deployment_total_requests_total[5m])) * 60"),
    "failures": ("AI failures per minute", "failures/min", "sum by (litellm_model_name) (rate(litellm_deployment_failure_responses_total[5m])) * 60"),
    "errors": ("Provider errors per minute", "errors/min", "sum by (exception_status, exception_class) (rate(litellm_deployment_failure_responses_total[5m])) * 60"),
    "availability": ("AI monitoring availability", "up", 'up{job="litellm"}'),
}


def charts(*, hours=6):
    hours = max(1, min(24, int(hours)))
    with _LOCK:
        saved = _CACHE.get(hours)
        if saved and time.monotonic() - saved[0] < 30:
            return saved[1]
    end = int(time.time())
    step = max(60, hours * 3600 // 180)

    def fetch(pair):
        key, (title, unit, query) = pair
        chart = {"id": key, "title": title, "unit": unit, "series": [], "available": False}
        try:
            result = _get("http://127.0.0.1:9090/api/v1/query_range?" + urlencode({
                "query": query, "start": end - hours * 3600, "end": end, "step": step,
                "timeout": "2s", "limit": 20}))
            if not isinstance(result, dict):
                raise ValueError("Prometheus returned a response that is not a JSON object")
            if result.get("status") != "success":
                raise ValueError(result.get("error") or "Prometheus did not return chart data")
            data = result.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                raise ValueError("Prometheus returned no valid time-series collection")
            for series in data["result"][:20]:
                if (not isinstance(series, dict) or not isinstance(series.get("metric"), dict)
                        or not isinstance(series.get("values"), list)):
                    raise ValueError("Prometheus returned a malformed time series")
                labels = {name: sanitize(value, 180) for name, value in series["metric"].items()
                          if name in {"litellm_model_name", "exception_status", "exception_class", "job"}}
                points = []
                for stamp, raw in series["values"][:181]:
                    number = float(raw)
                    timestamp = float(stamp)
                    if math.isfinite(number) and math.isfinite(timestamp):
                        points.append({"at": timestamp, "value": number})
                if points:
                    chart["series"].append({"label": " · ".join(labels.values()) or title, "labels": labels,
                                            "points": points, "latest": points[-1]["value"]})
            chart["available"] = bool(chart["series"])
            if not chart["available"]:
                chart["message"] = "Prometheus has no samples for this metric in the selected period."
            if isinstance(result.get("warnings"), list):
                chart["warnings"] = [sanitize(warning, 500) for warning in result["warnings"][:5]]
        except HTTPError as exc:
            message = str(exc)
            try:
                failure = json.loads(exc.read(8192))
                if isinstance(failure, dict) and failure.get("error"):
                    message = str(failure["error"])
            except (OSError, ValueError):
                pass
            finally:
                exc.close()
            chart["error"] = sanitize(message, 700)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            chart["error"] = sanitize(exc, 700)
        return chart

    with ThreadPoolExecutor(max_workers=4) as pool:
        items = list(pool.map(fetch, _QUERIES.items()))
    value = {"source": "Prometheus", "at": datetime.now(timezone.utc).isoformat(), "hours": hours,
             "available": any(item["available"] for item in items), "charts": items,
             "grafana_url": "http://localhost:3000/d/litellm-provider-fabric", "refresh_seconds": 30}
    with _LOCK:
        _CACHE[hours] = (time.monotonic(), value)
    return value

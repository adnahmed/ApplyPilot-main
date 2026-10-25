"""Finite discovery reads; Temporal selects and retries transport operations."""
from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit


from .models import Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask

_browser_recovery_url = ContextVar("discovery_browser_recovery_url", default=None)


@contextmanager
def browser_recovery(url=None):
    token = _browser_recovery_url.set(url)
    try:
        yield
    finally:
        _browser_recovery_url.reset(token)


def browser_recovery_url():
    return _browser_recovery_url.get()


@dataclass(frozen=True, slots=True)
class TransportResult:
    html: str
    status_code: int
    headers: dict[str, str]
    url: str
    layer: str
    user_agent: str = ""
    cookies: list[dict] = field(default_factory=list)
    elapsed_ms: float = 0


@dataclass(frozen=True, slots=True)
class TransportError:
    error: str
    last_url: str
    layer_attempted: str
    elapsed_ms: float = 0
    kind: FailureKind = FailureKind.UNKNOWN
    http_status: int | None = None
    retry_after: float | None = None


class TaskDeferred(Exception):
    operation_outcome = "deferred"

    def __init__(self, message, kind=FailureKind.CONFIGURATION, *, retry_after=None,
                 http_status=None):
        super().__init__(message)
        self.failure = FetchFailure(kind, message, http_status, retry_after)


def diagnostic_url(value):
    """Retain request location without embedding credentials in failure history."""
    parts = urlsplit(str(value))
    host = parts.hostname or ''
    if ':' in host:
        host = '['+host+']'
    if parts.port:
        host += ':'+str(parts.port)
    pairs = parse_qsl(parts.query,keep_blank_values=True)
    secret = re.compile(r'token|secret|password|api.?key|session|cookie|authorization|csrf|signature',re.I)
    if not parts.username and not parts.password and not any(secret.search(key) for key,_ in pairs):
        return urlunsplit((parts.scheme,parts.netloc,parts.path,parts.query,''))
    query = [(key,'[redacted]' if secret.search(key) else val) for key,val in pairs]
    return urlunsplit((parts.scheme,host,parts.path,urlencode(query),'')).rstrip('?')


def task_params(task: SearchTask, providers=()) -> dict:
    if task.target is None:
        raise TaskDeferred("Task has no target")
    if providers and task.target.provider not in providers:
        raise TaskDeferred("Task provider does not match adapter")
    pairs = task.target.request_params
    params = dict(pairs)
    if len(params) != len(pairs):
        raise TaskDeferred("Duplicate request parameter names")
    if task.logical_request_key and task.logical_request_key != task.target.request_key():
        raise TaskDeferred("Logical request key differs from target request identity")
    return params


def page_metadata(task, context, **extra):
    target = task.target
    return {
        "provider": target.provider if target else None,
        "scope": target.scope if target else None,
        "kind": target.kind if target else None,
        "request_key": target.request_key() if target else task.logical_request_key,
        "origins": list(target.origins) if target else [],
        "cycle_id": context.cycle_id,
        **extra,
    }


def deferred_page(task, context, error, *, cursor=None, records=(), **metadata):
    if isinstance(error, TaskDeferred):
        failure = error.failure
    elif isinstance(error, TransportError):
        failure = FetchFailure(error.kind, error.error, error.http_status, error.retry_after)
    else:
        raise error
    return DiscoveryPage(
        task_id=task.task_id or "", records=tuple(records), next_cursor=cursor,
        complete=False, partial=bool(records), exhausted=False,
        coverage=Coverage.PARTIAL if records else Coverage.UNKNOWN,
        failures=(failure,), request_metadata=page_metadata(
            task, context, status="deferred", **metadata),
    )


def reserve_budget(context, resource, amount=1):
    """Compatibility access/cancellation hook; Temporal owns execution capacity."""
    if type(amount) not in (int, float) or not math.isfinite(amount) or amount <= 0:
        raise ValueError('Request amount must be finite and positive')
    service = getattr(context, 'reserve_budget', None)
    if service is not None:
        if service(resource, amount) is False:
            raise PermissionError('Provider operation is not authorized')
    elif resource == 'paid':
        raise PermissionError('Paid source requires a verified billing adapter')


def authorized(context, provider):
    return context.runtime.get("authorized_sources", {}).get(provider) is True


def access_blocked(body, url=""):
    """Compatibility predicate for an observed provider challenge document."""
    from applypilot.browser.cloudflare import is_browser_challenge
    return is_browser_challenge(body)


def _validate_response(body, status_code=200, min_size=0):
    if not 200 <= status_code < 300:
        return False, f"HTTP {status_code}"
    if access_blocked(body):
        return False, "Provider returned a browser challenge"
    if len(body.encode("utf-8")) < min_size:
        return False, "Response too small"
    return True, "ok"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward credentials or silently change an authorized request.
        return None


def _plain_request(url, *, timeout=20.0, headers=None, method="GET", data=None):
    from applypilot.integrations.net import public_url

    start = time.monotonic()
    url = public_url(url)
    request = urllib.request.Request(url, data=data, method=method, headers={
        "User-Agent": "ApplyPilot/1.0 (discovery)", **(headers or {}),
    })
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return TransportResult(response.read().decode("utf-8"), response.status,
                                   dict(response.headers), response.url, "plain",
                                   elapsed_ms=(time.monotonic() - start) * 1000)
    except urllib.error.HTTPError as exc:
        return TransportResult(exc.read().decode("utf-8", errors="replace"), exc.code,
                               dict(exc.headers), url, "plain")


def _response_error(result):
    from applypilot.integrations.search_providers import retry_after

    status = result.status_code
    headers = {k.lower(): v for k, v in result.headers.items()}
    retry = retry_after(headers.get("retry-after"))
    return TransportError(f"HTTP {status}" if not 200 <= status < 300 else "Provider returned a browser challenge",
                          result.url, result.layer, result.elapsed_ms, FailureKind.UNKNOWN, status, retry)


def fetch_with_transport(url, *, headers=None, timeout=20.0, method="GET", data=None,
                         force_layer=None, validate=True):
    """One requested transport operation; Temporal chooses subsequent attempts."""
    from applypilot.browser.cloudflare import is_browser_challenge
    from applypilot.integrations.net import public_url
    from applypilot.integrations.types import SourceError

    url = public_url(url)
    if force_layer is None and browser_recovery_url() == url:
        force_layer = "flaresolverr"
    if force_layer == "flaresolverr":
        if method != "GET" or data is not None:
            raise ValueError("Discovery solver reads require GET without request data")
        from applypilot.browser.cloudflare import FlareSolverrClient
        solution, html = FlareSolverrClient(proxy=os.environ.get("APPLYPILOT_BROWSER_PROXY_URL")).solve_for_html(
            public_url(url), max_timeout=timeout)
        result = TransportResult(html, solution.status or 200, {}, public_url(solution.final_url),
                                 "flaresolverr", user_agent=solution.user_agent)
    elif force_layer in (None, "plain"):
        result = _plain_request(url, headers=headers, timeout=timeout, method=method, data=data)
    else:
        raise ValueError("Unknown discovery transport")
    if isinstance(result, TransportError):
        return result
    if is_browser_challenge(result.html, result.headers):
        error = SourceError("Provider returned a browser challenge", "unknown")
        error.http_status = result.status_code
        error.retry_after = _response_error(result).retry_after
        if method == "GET" and data is None and force_layer != "flaresolverr":
            error.challenge_url = public_url(result.url)
        raise error
    if not 200 <= result.status_code < 300:
        return _response_error(result)
    return result


def fetch_json_with_transport(url, *, headers=None, timeout=20.0):
    result = fetch_with_transport(url, headers={"Accept": "application/json", **(headers or {})},
                                  timeout=timeout, validate=False)
    if isinstance(result, TransportError):
        return None, result
    return json.loads(result.html), result


def request_json(context, url, *, headers=None):
    reserve_budget(context, "http")
    fetch = getattr(context, "fetch_json", fetch_json_with_transport)
    data, result = fetch(url, headers=headers, timeout=20.0)
    if isinstance(result, TransportError):
        raise TaskDeferred(result.error, result.kind, retry_after=result.retry_after,
                           http_status=result.http_status)
    if result is not None and not 200 <= result.status_code < 300:
        error = _response_error(result)
        raise TaskDeferred(error.error, error.kind, retry_after=error.retry_after,
                           http_status=error.http_status)
    return data


def load_export(task, context, params, cursor=None):
    """Read one explicitly allowlisted JSON export, never an implicit home file.

    Envelope: {request_key, cursor, payload, complete: bool, next_cursor: str|null}.
    The request key includes export_path. Export creation must use the same target.
    inventory_files[provider] grants exact files, not recursive directory access.
    """
    provider = task.target.provider
    if not authorized(context, provider):
        raise TaskDeferred("Export ingestion requires explicit source authorization", FailureKind.ACCESS_DENIED)
    path = Path(params.get("export_path", ""))
    allowed = context.runtime.get("inventory_files", {}).get(provider, ())
    if not params.get("export_path") or path.resolve() not in {Path(p).resolve() for p in allowed}:
        raise TaskDeferred("export_path is not an authorized export file", FailureKind.ACCESS_DENIED)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise TaskDeferred(f"Export cannot be read: {type(exc).__name__}", FailureKind.SCHEMA) from exc
    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise TaskDeferred("Export requires an envelope with payload", FailureKind.SCHEMA)
    if envelope.get("request_key") != task.target.request_key() or envelope.get("cursor") != cursor:
        raise TaskDeferred("Export does not belong to this exact request/cursor")
    if type(envelope.get("complete")) is not bool:
        raise TaskDeferred("Export must declare complete as a boolean", FailureKind.SCHEMA)
    next_cursor = envelope.get("next_cursor")
    if next_cursor is not None and (not isinstance(next_cursor, str) or next_cursor == cursor):
        raise TaskDeferred("Export has an invalid continuation cursor", FailureKind.SCHEMA)
    return envelope


def recovery_grant(task, context, cursor=None):
    """Validate the identity and configured scope of one recovery read."""
    task_params(task)
    failed = getattr(context, "failed_task", None)
    grant = getattr(context, "recovery_authorization", None)
    if not isinstance(failed, SearchTask) or failed.status != "failed" or not isinstance(grant, Mapping):
        raise TaskDeferred("Recovery requires the exact failed task and an explicit grant", FailureKind.ACCESS_DENIED)
    if (failed.task_id != task.task_id or failed.target != task.target
            or failed.refresh_generation != task.refresh_generation
            or grant.get("task_id") != task.task_id
            or grant.get("request_key") != task.target.request_key()
            or grant.get("cursor") != cursor or grant.get("authorized") is not True):
        raise TaskDeferred("Recovery identity/authorization mismatch", FailureKind.ACCESS_DENIED)
    if grant.get("browser_purpose") != "discovery":
        raise TaskDeferred("Submission browser resources cannot be used for discovery", FailureKind.ACCESS_DENIED)
    url = grant.get("url")
    if not isinstance(url, str) or urlparse(url).scheme != "https":
        raise TaskDeferred("Recovery requires the exact failed HTTPS URL")
    from applypilot.integrations.net import public_url
    public_url(url)
    return grant


def legacy_publish(jobs, provider, *, context=None, conn=None):
    """Compatibility writers delegate all jobs/admission work to the repository."""
    from .repository import publish_records
    if context is None:
        from .context import build_context
        context = build_context()
    result = publish_records(tuple(jobs), context=context, provider=provider, conn=conn)
    return result.new, result.strict_duplicates


def legacy_run(executor, provider, *, context=None, task=None):
    if task is None:
        return {"found": 0, "new": 0, "existing": 0, "status": "deferred",
                "errors": ["A scheduler-issued SearchTask is required; no adapter search matrix"]}
    if context is None:
        from .context import build_context
        context = build_context()
    page = executor(task, context)
    new = existing = 0
    if page.records and not page.failures:
        new, existing = legacy_publish(page.records, provider, context=context)
    return {"found": len(page.records), "new": new, "existing": existing,
            "status": page.request_metadata.get("status", "done"),
            "errors": [failure.message for failure in page.failures], "page": page}

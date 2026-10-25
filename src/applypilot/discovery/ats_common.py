"""Native ATS board targets, one-page transport, and failure normalization."""
from __future__ import annotations

import logging
import urllib.error
from collections.abc import Callable
from urllib.parse import parse_qs, quote, urlparse

import yaml

from applypilot.config import CONFIG_DIR
from applypilot.discovery.models import (
    Coverage, DiscoveryPage, FailureKind, FetchFailure, SearchTask, SourceTarget,
)

log = logging.getLogger(__name__)


def load_employers_yaml(filename: str) -> dict:
    """Read ``CONFIG_DIR/{filename}`` and return its ``employers`` block."""
    path = CONFIG_DIR / filename
    if not path.exists():
        log.warning("%s not found at %s", filename, path)
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("employers", {})


def board_target(provider: str, slug: str, employer: dict, origin: str) -> SourceTarget:
    """Lower one local inventory row without fetching or consulting a profile/DB."""
    url = employer.get("endpoint") or employer.get("url") or employer.get("career_url") or ""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    board = employer.get("board") or employer.get("token") or employer.get("tenant") or employer.get("scope")
    if not board:
        if provider == "greenhouse" and (host == "greenhouse.io" or host.endswith(".greenhouse.io")):
            board = (parse_qs(parsed.query).get("for") or [None])[0]
            if not board and "boards" in parts and parts.index("boards") + 1 < len(parts):
                board = parts[parts.index("boards") + 1]
            elif not board and parts and parts[0] != "embed":
                board = parts[0]
        elif provider == "lever" and (host.endswith(".lever.co") or host.endswith(".lever.eu")):
            board = parts[parts.index("postings") + 1] if "postings" in parts and parts.index("postings") + 1 < len(parts) else (parts[0] if parts else None)
        elif provider == "ashby" and host.endswith(".ashbyhq.com"):
            board = parts[parts.index("job-board") + 1] if "job-board" in parts and parts.index("job-board") + 1 < len(parts) else (parts[0] if parts else None)
    from applypilot.discovery.careers.base import public_url, tenant_name
    board = tenant_name(str(board or slug))
    region = employer.get("api_region") or ("eu" if host.endswith(".lever.eu") else "global")
    if provider == "lever":
        region = {"us": "global", "api.lever.co": "global", "api.lever.eu": "eu"}.get(str(region).lower(), str(region).lower())
    if provider not in {"greenhouse", "lever", "ashby"}:
        raise ValueError("Unsupported direct board provider")
    if provider != "lever" and region != "global":
        raise ValueError(f"{provider} only supports the global public board API")
    if provider == "lever" and region not in {"global", "eu"}:
        raise ValueError("Lever api_region must be global or eu")
    if url:
        public_url(url)
        allowed_hosts = {"greenhouse": {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards-api.greenhouse.io"},
                         "lever": {"jobs.lever.co", "api.lever.co"} if region == "global" else {"jobs.lever.eu", "api.lever.eu"},
                         "ashby": {"jobs.ashbyhq.com", "api.ashbyhq.com"}}
        if host not in allowed_hosts[provider]:
            raise PermissionError("Native board URL is outside the provider/region public hosts")
        # Check the URL-derived tenant independently of an explicit token.
        if provider == "greenhouse":
            url_board = (parse_qs(parsed.query).get("for") or [None])[0]
            if not url_board:
                url_board = parts[parts.index("boards") + 1] if "boards" in parts and parts.index("boards") + 1 < len(parts) else (parts[0] if parts else None)
        elif provider == "lever":
            url_board = parts[parts.index("postings") + 1] if "postings" in parts and parts.index("postings") + 1 < len(parts) else (parts[0] if parts else None)
        else:
            url_board = parts[parts.index("job-board") + 1] if "job-board" in parts and parts.index("job-board") + 1 < len(parts) else (parts[0] if parts else None)
        if url_board != board:
            raise PermissionError("Native URL and tenant/token disagree")
    params = {"greenhouse": {"content": True}, "lever": {"mode": "json"},
              "ashby": {"includeCompensation": True}}[provider]
    if provider == "lever":
        params["api_region"] = region
    if employer.get("name") or employer.get("company"):
        params["name"] = employer.get("name") or employer.get("company")
    return SourceTarget(provider=provider, scope=board, kind="board_refresh",
                        request_params=tuple(sorted(params.items())), origins=(origin,))


def failure_page(task: SearchTask, error: Exception, *, metadata=None) -> DiscoveryPage:
    """Keep denial, throttling, schema errors and successful-empty distinct."""
    from applypilot.discovery.transport import TaskDeferred
    if isinstance(error, TaskDeferred):
        return DiscoveryPage(task_id=task.task_id or "", complete=False, coverage=Coverage.UNKNOWN,
                             request_metadata=metadata or {}, failures=(error.failure,))
    status = error.code if isinstance(error, urllib.error.HTTPError) else None
    kind = FailureKind.NETWORK
    if status == 401:
        kind = FailureKind.AUTH
    elif status == 403 or isinstance(error, PermissionError):
        kind = FailureKind.ACCESS_DENIED
    elif status == 429:
        kind = FailureKind.RATE_LIMIT
    elif isinstance(error, (ValueError, TypeError, KeyError, AttributeError)):
        kind = FailureKind.SCHEMA
    elif isinstance(error, TimeoutError):
        kind = FailureKind.TIMEOUT
    retry_after = None
    if isinstance(error, urllib.error.HTTPError):
        try:
            retry_after = float(error.headers.get("Retry-After", ""))
        except (ValueError, TypeError, AttributeError):
            pass
    return DiscoveryPage(task_id=task.task_id or "", complete=False,
                         coverage=Coverage.UNKNOWN, request_metadata=metadata or {},
                         failures=(FetchFailure(kind, str(error), http_status=status,
                                                retry_after=retry_after),))


def execute_board_task(task: SearchTask, context, cursor, *, provider: str,
                       parser: Callable, employers: dict | None = None) -> DiscoveryPage:
    """Fetch exactly one complete public board, without filtering or publication."""
    metadata = {"provider": provider}
    try:
        from applypilot.discovery import transport
        from applypilot.discovery.careers.base import request
        params = transport.task_params(task, (provider,))
        if context is None:
            raise transport.TaskDeferred("A discovery context is required")
        if context.runtime.get("authorized_sources", {}).get(provider) is False:
            raise PermissionError("Native board access denied by runtime policy")
        allowed = {"board", "tenant", "token", "api_region", "content", "mode", "includeCompensation",
                   "name", "company", "endpoint", "url", "career_url"}
        if set(params) - allowed:
            raise transport.TaskDeferred("Unsupported native board task parameters")
        if cursor not in (None, ""):
            raise ValueError("complete-board endpoint does not accept a cursor")
        target = board_target(provider, task.target.scope or "", params, "task")
        board = target.scope
        if not board:
            raise ValueError("board/token/tenant or scope is required")
        region = dict(target.request_params).get("api_region")
        if any(params.get(key) and params[key] != board for key in ("board", "tenant", "token")) or task.target.scope != board:
            raise ValueError("Native board task scope and token/tenant disagree")
        for key in ("endpoint", "url", "career_url"):
            if params.get(key):
                board_target(provider, board, {**params, "endpoint": params[key]}, "validation")
        native = context.runtime.get("native_targets", {}).get(provider)
        if native is not None and not any(value in native for value in (board, region, f"{board}:{region or 'global'}")):
            raise PermissionError("Native board is outside runtime native_targets")
        token = quote(board, safe="")
        if provider == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"
            recovery_url = f"https://boards.greenhouse.io/{board}"
        elif provider == "lever":
            if region not in ("global", "eu"):
                raise ValueError("Lever api_region must be global or eu")
            host = "api.lever.eu" if region == "eu" else "api.lever.co"
            url = f"https://{host}/v0/postings/{token}?mode=json"
            recovery_url = f"https://jobs.lever.co/{board}"
        else:
            url = f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true"
            recovery_url = f"https://jobs.ashbyhq.com/{board}"
        metadata.update(url=url, recovery_url=recovery_url, tenant=board, api_region=region, authoritative=True)
        metadata.update(cursor=cursor, request_key=task.target.request_key(), origins=list(task.target.origins))
        payload = request(context, url)
        if provider == "lever":
            rows = payload
        else:
            if not isinstance(payload, dict) or "jobs" not in payload:
                raise ValueError("board response is missing jobs")
            rows = payload["jobs"]
        if not isinstance(rows, list):
            raise ValueError("board postings must be an array")
        employer = dict((employers or {}).get(board) or {})
        employer.update({"slug": board, "name": params.get("company") or params.get("name") or employer.get("name") or board})
        records, failures = [], []
        for index, raw in enumerate(rows):
            try:
                if not isinstance(raw, dict):
                    raise ValueError("posting must be an object")
                record = parser(raw, employer)
                if record is None:
                    continue  # Explicitly unpublished, not a malformed record.
                if not record.get("url") or not record.get("title"):
                    raise ValueError("posting has no title or listing URL")
                records.append(record)
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                failures.append(FetchFailure(FailureKind.SCHEMA, str(error), provider_field=f"jobs[{index}]"))
        partial = bool(failures)
        return DiscoveryPage(task_id=task.task_id or "", records=tuple(records),
                             exhausted=not partial, complete=not partial, partial=partial,
                             coverage=Coverage.PARTIAL if partial else Coverage.COMPLETE,
                             request_metadata={**metadata, "fetched": len(rows)},
                             failures=tuple(failures))
    except Exception as error:
        return failure_page(task, error, metadata=metadata)

"""Snapshot board inventories and dispatch onto public ATS page executors.

Inventory is not a strategic search source. Native boards retain their provider;
internal boards use provider=ats_registry and params.parser. No publication in
execute_task; all runners publish through the shared repository.
"""
from __future__ import annotations

from applypilot.limits import page_indices

import json
import logging
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass, replace
from urllib.parse import urlsplit

from applypilot.discovery.models import DiscoveryPage, SearchTask, SourceTarget

log = logging.getLogger(__name__)
_NATIVE = {"greenhouse", "lever", "ashby"}
_INTERNAL = {"workable", "smartrecruiters", "bamboohr", "jazzhr", "generic"}
_ATS_FETCHERS = {}


def _lazy_register():
    if not _ATS_FETCHERS:
        from applypilot.discovery import ashby, greenhouse, lever
        from applypilot.discovery.careers import parser_module
        _ATS_FETCHERS.update(greenhouse=greenhouse.execute_task, lever=lever.execute_task,
                             ashby=ashby.execute_task)
        _ATS_FETCHERS.update((name, parser_module(name).execute_task) for name in _INTERNAL)


def internal_target(parser, slug, row, origin):
    from applypilot.discovery.careers.base import public_url, tenant_name
    url = row.get("endpoint") or row.get("url") or row.get("career_url")
    tenant = row.get("tenant") or row.get("board") or row.get("token") or row.get("scope")
    if url:
        public_url(url)
        parsed = urlsplit(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not tenant:
            if parser == "workable":
                if parsed.hostname == "www.workable.com" and parts[:2] == ["api", "accounts"]:
                    tenant = parts[2] if len(parts) > 2 else None
                elif parsed.hostname == "apply.workable.com":
                    tenant = parts[0] if parts else None
                elif parsed.hostname.endswith(".workable.com"):
                    tenant = parsed.hostname.split(".")[0]
            elif parser == "smartrecruiters":
                tenant = parts[2] if parts[:2] == ["v1", "companies"] and len(parts) > 2 else (parts[0] if parts else None)
            elif parser in {"bamboohr", "jazzhr"}:
                tenant = parts[0] if parsed.hostname == "jobs.jazz.co" and parts else parsed.hostname.split(".")[0]
    tenant = tenant_name(tenant or slug)
    params = {"parser": parser, "api_region": row.get("api_region") or "global"}
    for key in ("url", "career_url", "endpoint", "limit"):
        if row.get(key) is not None:
            params[key] = row[key]
    if row.get("name") or row.get("company"):
        params["name"] = row.get("name") or row.get("company")
    return SourceTarget("ats_registry", tenant, "board_refresh", tuple(sorted(params.items())), (origin,))


def union_board_targets(*inventories):
    """Semantic union; native identity is (provider, tenant, canonical API region).

    Call this on planner targets plus registry targets before creating tasks.
    Names and origins never multiply native requests. Internal endpoints/limits
    remain request identity because they can expose different public listings.
    """
    from applypilot.discovery.ats_common import board_target
    union = {}
    for inventory in inventories:
        for target in inventory:
            if not isinstance(target, SourceTarget):
                raise TypeError("Board inventory must contain SourceTargets")
            params = dict(target.request_params)
            if target.provider in _NATIVE and target.kind == "board_refresh":
                normalized = board_target(target.provider, target.scope or "", params, "union")
                target = replace(target, scope=normalized.scope, request_params=normalized.request_params)
                key = (target.provider, target.scope, dict(target.request_params).get("api_region", "global"))
            else:
                identity = replace(target, request_params=tuple((k, v) for k, v in target.request_params if k not in {"name", "company"}))
                key = identity.request_key()
            if key in union:
                old = union[key]
                params = dict(target.request_params)
                params.update(dict(old.request_params))
                target = replace(old, origins=tuple(dict.fromkeys((*old.origins, *target.origins))),
                                 request_params=tuple(sorted(params.items())),
                                 budget_name="preferred" if "preferred" in (old.budget_name, target.budget_name) else old.budget_name)
            union[key] = target
    return tuple(union.values())


def list_board_targets(companies=(), *, configured=None):
    """Local files + supplied rows; no DB/profile/provider IO.

    configured=None reads packaged native YAMLs. configured={} disables file
    reads. Otherwise use {provider: {tenant: metadata} | [metadata, ...]}, including
    ats_registry rows with provider/parser and any of the five internal parsers.
    """
    from applypilot.discovery.ats_common import board_target, load_employers_yaml
    from applypilot.discovery.careers import detect_provider
    if configured is None:
        configured = {provider: load_employers_yaml(f"{provider}_employers.yaml") for provider in sorted(_NATIVE)}
    rows = []
    for provider, entries in configured.items():
        if isinstance(entries, Mapping):
            entries = [dict(value, scope=value.get("scope", key)) for key, value in entries.items()]
        for index, entry in enumerate(entries):
            rows.append({**entry, "provider": entry.get("provider") or entry.get("ats_provider") or provider,
                         "origins": [*entry.get("origins", ()), entry.get("inventory_origin") or f"config:{provider}:{index}"]})
    rows.extend(asdict(row) if is_dataclass(row) else dict(row) for row in companies)
    targets = []
    for company in rows:
        raw = company.get("raw_metadata") or {}
        try:
            raw = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            log.warning("Ignoring malformed registry metadata for %s", company.get("slug"))
            raw = {}
        primary = {**raw, **{key: value for key, value in company.items() if value is not None}}
        slug = primary.get("slug") or primary.get("scope") or primary.get("tenant") or ""
        boards = primary.get("boards") or raw.get("boards") or []
        if not isinstance(boards, (list, tuple)):
            raise ValueError("Registry boards must be an array")
        boards = [primary, *boards]
        for row in boards:
            if not isinstance(row, Mapping):
                raise ValueError("Registry board must be an object")
            url = row.get("endpoint") or row.get("url") or row.get("career_url")
            selector = row.get("parser") or row.get("ats_provider") or row.get("provider")
            provider = selector if selector != "ats_registry" else None
            provider = provider or (detect_provider(url) if url else None)
            provider = {"custom": "generic", "workable.com": "workable", "smartrecruiters.com": "smartrecruiters",
                        "bamboohr.com": "bamboohr", "jazz.co": "jazzhr", "jazzhr.com": "jazzhr"}.get(provider, provider)
            if provider not in _NATIVE | _INTERNAL:
                continue
            origins = tuple(dict.fromkeys((f"registry:{slug or primary.get('name', '')}",
                                          *primary.get("origins", ()), *row.get("origins", ()))))
            metadata = {"name": primary.get("name"), **row}
            try:
                target = (board_target(provider, slug, metadata, origins[0]) if provider in _NATIVE
                          else internal_target(provider, slug, metadata, origins[0]))
            except (ValueError, PermissionError, TypeError) as error:
                log.warning("Invalid registry board %s: %s", slug, error)
                continue
            targets.append(replace(target, origins=origins))
    return union_board_targets(targets)


def list_registry_targets(conn, context):
    """Read existing company_registry rows only; no schema creation or provider IO.

    Includes observed/discovered rows, not just FreeHire/verified rows. Runtime
    access/URL checks occur at execution, not via company geography heuristics.
    Configured files belong to planner.read_inventories(context), not this API.
    """
    if "ats_registry" not in context.intent.sources_enabled:
        return ()
    exists = conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename='company_registry'").fetchone()
    if not exists:
        return ()
    result = conn.execute("SELECT * FROM company_registry ORDER BY slug")
    columns = [column[0] for column in result.description]
    rows = [dict(zip(columns, row)) for row in result.fetchall()]
    rows = [row for row in rows if row.get("verification_status") not in {"rejected", "revoked", "deleted"}]
    return list_board_targets(rows, configured={})


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    """One public page. Params: parser, URL/endpoint, api_region, optional name/token."""
    from applypilot.discovery.ats_common import failure_page
    from applypilot.discovery.transport import task_params
    try:
        _lazy_register()
        params = task_params(task)
        if context is None:
            raise ValueError("Registry execution requires a discovery context")
        if task.target.provider == "ats_registry" and context.runtime.get("authorized_sources", {}).get("ats_registry") is False:
            raise PermissionError("Registry access denied by runtime policy")
        provider = params.get("parser") or params.get("ats_provider") or params.get("provider") or task.target.provider
        if provider not in _ATS_FETCHERS:
            raise ValueError("An implemented public career parser is required")
        if provider in _NATIVE and task.target.provider == "ats_registry":
            target = replace(task.target, provider=provider, request_params=tuple(
                (key, value) for key, value in task.target.request_params if key not in {"parser", "provider", "ats_provider"}))
            delegated = replace(task, target=target, logical_request_key=None)
            page = _ATS_FETCHERS[provider](delegated, context, cursor)
            return replace(page, request_metadata={**page.request_metadata, "request_key": task.target.request_key(), "parser": provider})
        return _ATS_FETCHERS[provider](task, context, cursor)
    except Exception as error:
        return failure_page(task, error)


def fetch_company_jobs(conn, slug, ats_provider=None, career_url=None, *, context=None,
                       api_region=None, company_name=None):
    """Legacy fetch-only API, same task parsers, explicit snapshot and page limits."""
    if context is None:
        raise ValueError("Career fetch requires an explicit discovery context")
    rows = [{"slug": slug, "provider": ats_provider, "career_url": career_url,
             "api_region": api_region, "name": company_name}]
    targets = list_board_targets(rows, configured={})
    if not targets:
        raise ValueError("No valid public board target")
    records = []
    for target in targets:
        task, cursor = SearchTask(target=target), None
        for _ in page_indices(context.runtime.get("max_pages_per_task", -1)):
            page = execute_task(task, context, cursor)
            if page.failures:
                raise RuntimeError("; ".join(f.message for f in page.failures))
            records.extend(page.records)
            if page.next_cursor is None:
                break
            if page.next_cursor == cursor:
                raise ValueError("Career cursor did not advance")
            cursor = page.next_cursor
        else:
            raise RuntimeError("Career page budget reached before enumeration completed")
    return records

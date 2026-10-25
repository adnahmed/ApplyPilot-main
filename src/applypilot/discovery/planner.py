"""Read-only, source-shaped request planning. Never imports provider modules.

Inventories: mapping provider -> sequence of mappings with scope/tenant/slug,
api_region, optional native_targets and request endpoint metadata. ats_registry
entries additionally specify provider (or ats_provider). Files accept this list
under companies, boards or employers; keyed employer mappings are also accepted.
Only configured files and package employer inventories are read, never a DB or
network registry. Passing inventories={} disables all inventory file reads.
"""

from collections.abc import Mapping
from applypilot.limits import page_size

from dataclasses import asdict, replace
import json
from pathlib import Path
import re
from urllib.parse import urlparse

from .capabilities import (
    CAPABILITIES, JOBSPY_BOARDS, INDEED_COUNTRIES, access_reason, capability_for, execution_reason,
)
from .context import DiscoveryContext
from .countries import COUNTRY_NAMES
from .models import SearchTask, SourceTarget
from .preferences import rotate_probes


def read_inventories(context: DiscoveryContext) -> dict:
    from applypilot.config import CONFIG_DIR
    import yaml

    defaults = {"greenhouse": "greenhouse_employers.yaml", "lever": "lever_employers.yaml",
                "ashby": "ashby_employers.yaml", "workday": "employers.yaml"}
    inventories = {}
    cache = {}
    for provider in context.intent.sources_enabled:
        files = context.runtime["inventory_files"].get(provider)
        if files is None:
            files = (str(CONFIG_DIR / defaults[provider]),) if provider in defaults else ()
        entries = []
        for filename in files:
            path = Path(filename).expanduser().resolve()
            if path not in cache:
                cache[path] = yaml.safe_load(path.read_text(encoding="utf-8"))
            raw = cache[path]
            if isinstance(raw, Mapping):
                raw = raw.get("employers", raw.get("companies", raw.get("boards", raw)))
            if isinstance(raw, Mapping):
                raw = [dict(value, scope=value.get("scope", key))
                       if isinstance(value, Mapping) else {"scope": key, "invalid": True}
                       for key, value in raw.items()]
            if not isinstance(raw, list) or any(not isinstance(row, Mapping) for row in raw):
                raise ValueError(f"Inventory {path} must contain employer/board mappings")
            entries.extend(dict(row, inventory_origin=f"inventory:{path}:{i}") for i, row in enumerate(raw))
        inventories[provider] = entries
    return inventories


def rotation_key(target: SourceTarget) -> str:
    params = dict(target.request_params)
    return "probe:" + json.dumps((target.provider, target.scope, params.get("site_name"),
                                  params.get("api_region")), separators=(",", ":"))


def _origins(context, query=None, market=None):
    result = []
    queries = (query,) if query is not None else context.intent.active_queries()
    markets = (market,) if market is not None else context.intent.active_markets()
    result.extend(f"query:{q.effective_id()}" for q in queries)
    result.extend(f"query-tier:{q.tier}" for q in queries)
    labels = {str(m.label.value) for m in markets}
    result.extend(f"market:{m.label.value}" for m in markets)
    result.extend(f"preference:{p.id}" for p in context.intent.preferences if labels.intersection(p.markets))
    return tuple(result)


def _country(value):
    return COUNTRY_NAMES.get(value.casefold(), value.upper()) if value else None


def plan_tasks(context: DiscoveryContext, inventories=None, cursors=None) -> tuple[SearchTask, ...]:
    """Compile candidate requests, including explicit deferrals, without effects.

    Task IDs/generations/leases are assigned only by the live scheduler. Cursors
    are optional persisted probe positions; without them this is an initial-plan
    preview, not a claim that every board is currently due. Exploration quotas
    are applied by the scheduler, not rounded independently per provider.
    """
    if not isinstance(context, DiscoveryContext):
        raise TypeError("plan_tasks requires DiscoveryContext")
    queries, markets = context.intent.active_queries(), context.intent.active_markets()
    from applypilot.targets.intent import tasks as target_tasks
    directed = target_tasks(context)
    if (not queries or not markets or not context.intent.sources_enabled
            or not context.runtime["request_budget"] or not context.runtime["max_pages_per_task"]
            or not context.runtime["request_defaults"]["results_per_site"]):
        return directed if context.runtime["request_budget"] and context.runtime["max_pages_per_task"] else ()
    inventories = read_inventories(context) if inventories is None else inventories
    cursors = {} if cursors is None else cursors
    if not isinstance(inventories, Mapping) or not isinstance(cursors, Mapping):
        raise ValueError("inventories and cursors must be mappings")
    result = {task.logical_request_key: task for task in directed}
    defaults = context.runtime["request_defaults"]

    def add(target, reason=None):
        if reason is None:
            reason = access_reason(target, context.runtime) or execution_reason(target)
        key = target.request_key()
        if key in result:
            previous = result[key]
            merged = tuple(dict.fromkeys(previous.target.origins + target.origins))
            # A native request shared with an active origin is not off-portfolio.
            budget = "preferred" if "preferred" in (target.budget_name, previous.target.budget_name) else target.budget_name
            result[key] = replace(previous, target=replace(previous.target, origins=merged, budget_name=budget))
            return
        result[key] = SearchTask(logical_request_key=key, target=target,
            status=f"deferred:{reason}" if reason else "pending",
            intent_fingerprint=context.intent.fingerprint(), profile_version=context.applicant.fingerprint())

    def defer(provider, reason, scope=None, origins=()):
        add(SourceTarget(provider, scope, "deferred", (("reason", reason),), origins), reason)

    def target(provider, params, *, scope=None, kind="query", origins=(), native=(), explore=False):
        return SourceTarget(provider, scope, kind, tuple(sorted(params.items())), origins,
                            "exploration" if explore else "preferred", tuple(native))

    def probes(provider, scope=None, params=None):
        base = target(provider, params or {}, scope=scope)
        words = tuple(dict.fromkeys(p.query for p in context.intent.probes))
        selected, _ = rotate_probes(words, cursors.get(rotation_key(base), 0), context.runtime["probe_limit"])
        return selected

    # Union actual boards before dispatch, including native boards found only in
    # a registry. Region and tenant are provider identity, not applicant facts.
    for selected in context.intent.sources_enabled:
        if selected not in CAPABILITIES:
            defer(selected, "unknown_provider")
            continue
        cap = CAPABILITIES[selected]
        if selected == "freehire" and "ats_registry" in context.intent.sources_enabled:
            add(target(
                "freehire", {"limit": 100}, scope="companies", kind="company_inventory",
                origins=("inventory:freehire:companies",), native=("companies",),
            ))
        if cap.shape in ("board", "registry") or selected == "workday":
            entries = inventories.get(selected, ())
            if not entries:
                defer(selected, "inventory_empty")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{selected} inventory entries must be mappings")
                provider = entry.get("provider", entry.get("ats_provider", selected)) if selected == "ats_registry" else selected
                origins = _origins(context) + (str(entry.get("inventory_origin", f"inventory:{selected}")),)
                scope = entry.get("tenant", entry.get("board", entry.get("token", entry.get("scope", entry.get("slug")))))
                if provider not in CAPABILITIES or provider not in ("greenhouse", "lever", "ashby", "workday", "ats_registry"):
                    # Internal career parsers stay behind the registry adapter.
                    if selected == "ats_registry" and provider in ("workable", "smartrecruiters", "bamboohr", "jazzhr", "generic"):
                        parser = provider
                        provider = "ats_registry"
                    else:
                        defer(selected, "unsupported_registry_provider", str(scope), origins)
                        continue
                else:
                    parser = None
                if not isinstance(scope, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", scope) or entry.get("invalid"):
                    defer(provider, "invalid_tenant", str(scope), origins)
                    continue
                region = entry.get("api_region", "global" if provider == "lever" else None)
                if provider == "lever" and region not in ("global", "eu"):
                    defer(provider, "invalid_api_region", scope, origins)
                    continue
                params = {"api_region": region} if region is not None else {}
                if provider == "greenhouse":
                    params["content"] = True
                if provider == "lever":
                    params["mode"] = "json"
                if provider == "ashby":
                    params["includeCompensation"] = True
                if parser:
                    params["parser"] = parser
                    add(target(provider, params, scope=scope, kind="board_refresh",
                               origins=origins, native=(parser,)))
                    continue
                if provider == "workday":
                    base_url, site = entry.get("base_url"), entry.get("site_id")
                    host = urlparse(base_url).hostname if isinstance(base_url, str) else None
                    if not host or not host.endswith(".myworkdayjobs.com") or not base_url.startswith("https://") or not isinstance(site, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", site):
                        defer(provider, "invalid_workday_endpoint", scope, origins)
                        continue
                    params.update(base_url=base_url.rstrip("/"), site_id=site, api_region=host, limit=20)
                    if entry.get("name"):
                        params["name"] = entry["name"]
                    # No evidence-backed complete enumeration contract exists yet.
                    for word in probes(provider, scope, params):
                        add(target(provider, dict(params, searchText=word), scope=scope,
                            kind="employer_probe", origins=origins, native=(host,)))
                else:
                    add(target(provider, params, scope=scope, kind="board_refresh", origins=origins,
                               native=(region or "tenant",)))
            continue
        if cap.shape == "fallback":
            defer(selected, "exact_failed_task_required", origins=_origins(context))
            continue
        if cap.shape == "feed":
            add(target(selected, {}, scope="feed", kind="board_refresh", origins=_origins(context), native=("feed",)))
            continue
        if cap.shape == "signal":
            for word in probes(selected):
                if selected in {"linkedin_posts", "twitter"}:
                    from .social_queries import hiring_queries
                    requests = ({"query": query, "query_version": 2} for query in hiring_queries(word))
                else:
                    requests = ({"query": word},)
                for params in requests:
                    add(target(selected, params, kind="signal", origins=_origins(context), native=cap.native_targets))
            continue
        boards = context.intent.jobspy_sites if selected == "jobspy" else (None,)
        for board in boards:
            native_cap = JOBSPY_BOARDS[board] if board else cap
            source_key = f"jobspy:{board}" if board else selected
            supported = native_cap.native_targets
            # Access/resource gates are source-wide. Do not materialize one doomed
            # deferred task for every query/market combination.
            gate_params = {"site_name": board} if board else {}
            blocked = access_reason(target(selected, gate_params), context.runtime)
            if blocked:
                defer(selected, blocked, scope=board, origins=_origins(context))
                continue
            if board == "bayt":
                for query in queries:
                    add(target(selected, {"site_name": board, "search_term": query.query},
                               origins=_origins(context, query), native=("query_only",)))
                continue
            intersections = []
            for market in markets:
                countries = ((_country(market.search_country),) if market.search_country else market.countries)
                if not countries and "global" in supported and "global" in market.regions:
                    countries = ("global",)
                valid = [c for c in countries if c in supported]
                for country in valid:
                    intersections.append((market, country))
                if not valid:
                    defer(selected, f"unsupported_native_target:{board or selected}:{market.label.value}", origins=_origins(context, market=market))
            # Explicit native exploration is labelled, never a fallback location
            # substituted into an unsupported market's request.
            natives = context.runtime["native_targets"].get(source_key, supported if len(supported) <= 2 else ())
            for country in natives:
                if country not in supported:
                    raise ValueError(f"Unsupported runtime native target {country!r} for {source_key}")
            lanes = [(m, c, False) for m, c in intersections]
            if not intersections:
                lanes.extend((None, c, True) for c in natives)
            use_probes = native_cap.shape == "probe" or cap.shape == "employer"
            for market, country, explore in lanes:
                probe_words = probes(selected, params={"site_name": board} if board else {}) if explore or use_probes else ()
                inputs = [(None, w) for w in probe_words] if explore or use_probes else [(q, q.query) for q in queries]
                for query, word in inputs:
                    params = {}
                    if board:
                        params["site_name"] = board
                        if board == "google":
                            params["google_search_term"] = " ".join(filter(None, (word, market.location if market else None,
                                "remote" if market and any(w.value == "remote" for w in market.work_modes) else None,
                                "worldwide" if country == "global" else country)))
                        else:
                            params["search_term"] = word
                            if "location" in native_cap.filters:
                                params["location"] = market.location if market and market.location else country
                            if "country_indeed" in native_cap.filters:
                                params["country_indeed"] = INDEED_COUNTRIES[country]
                        for old, new in (("distance", "distance"), ("hours_old", "hours_old"), ("results_per_site", "results_wanted")):
                            if new in native_cap.filters:
                                params[new] = defaults[old]
                    elif selected == "freehire":
                        params.update(q=word, limit=page_size(defaults["results_per_site"], 100))
                        if country == "global":
                            params["regions"] = ("global",)
                        elif market and market.cities:
                            # FreeHire ORs geographic filters, so a city-targeted
                            # request must not also contain its broader country.
                            params["cities"] = market.cities
                        else:
                            params["countries"] = (country,)
                        if market and market.work_modes:
                            params["work_mode"] = ",".join(mode.value for mode in market.work_modes)
                        kind = "employer_probe" if cap.shape == "employer" else "probe" if explore or use_probes else "query"
                        origins = _origins(context, query, market)
                        add(target(selected, params, kind=kind, origins=origins,
                                   native=(country,), explore=explore))
                        if market and market.cities and country != "global":
                            relaxed = dict(params)
                            relaxed.pop("cities", None)
                            relaxed["countries"] = (country,)
                            add(target(selected, relaxed, kind=kind, origins=origins,
                                       native=(country,), explore=explore))
                        if market:
                            for variant in context.intent.variants:
                                if variant.market.value != market.label.value:
                                    continue
                                variant_params = dict(params)
                                for key in ("salary_min", "salary_currency", "salary_period"):
                                    value = getattr(variant, key)
                                    if value is not None:
                                        variant_params[key] = value
                                if variant_params != params:
                                    add(target(
                                        selected, variant_params, kind=kind,
                                        origins=origins + (f"variant:{variant.name}",),
                                        native=(country,), explore=explore,
                                    ))
                        continue
                    elif selected == "hiring_cafe":
                        params.update(query=word, country=country)
                        if market and market.work_modes:
                            params["work_modes"] = tuple(mode.value for mode in market.work_modes)
                    elif selected == "reed":
                        params.update(keywords=word, locationName=market.location if market and market.location else "United Kingdom",
                                      resultsToTake=page_size(defaults["results_per_site"], 100))
                    else:
                        params.update(query=word, country=country)
                    kind = "employer_probe" if cap.shape == "employer" else "probe" if explore or use_probes else "query"
                    if selected in {"amazon", "costco"}:
                        params["page_size"] = page_size(defaults["results_per_site"], 100)
                    add(target(selected, params, scope=board, kind=kind, origins=_origins(context, query, market), native=(country,), explore=explore))
    return tuple(result.values())


def plan_report(context: DiscoveryContext, tasks: tuple[SearchTask, ...]) -> dict:
    """JSON-safe report; no claim of live access, adapter wiring or due coverage."""
    requests = []
    for task in tasks:
        item = asdict(task)
        if task.target.kind != "deferred":
            cap = capability_for(task.target)
            item["capability"] = asdict(cap)
            item["adapter_contract"] = f"applypilot.discovery.{cap.module}.execute_task"
        requests.append(item)
    return {
        "mode": "read_only_plan", "intent_fingerprint": context.intent.fingerprint(),
        "profile_version": context.applicant.fingerprint(), "requests": requests,
        "planned": sum(t.status == "pending" for t in tasks),
        "deferred": sum(t.status != "pending" for t in tasks),
        "coverage": "not_fetched", "due_state": "unknown_without_scheduler_state",
        "rotation": "initial_cursor_without_scheduler_state",
        "request_budget": context.runtime["request_budget"],
        "exploration_share": context.intent.exploration.discovery_share,
        "limitations": ["Adapter readiness and credentials are not probed.",
                        "Inventories are local files only; network/DB registries are not read.",
                        "Output-only eligibility/pay/engagement filters require common assessment.",
                        "Additive compensation variants require provider lowering; no base lane is replaced."],
    }

"""Compile raw search configuration into a typed :class:`SearchIntent`.

``compile_search_intent`` is the single, network-free entry point that turns
either a legacy v1 ``searches.yaml`` mapping or the v2 overlay (or a mix) into
an immutable :class:`SearchIntent` with a deterministic fingerprint. The legacy
applicant-snapshot helper remains below; connecting the real validated profile
and snapshot revisions to every live adapter is outside this checkpoint.

Rules enforced here come from the v2 handoff (CORE-02 / CFG-01..CFG-06):

- Unknown active labels, duplicate/conflicting market or query IDs, unknown
  providers/boards, invalid enum/type values and contradictory old/new keys
  are rejected with actionable errors.
- Equivalent legacy and v2 market definitions may warn; contradictory ones fail.
- ``requires_relocation`` is legacy market-intent only. Provider relocation
  enum values are never reinterpreted as that flag.
- Missing booleans are unknown/no-preference, not ``false``.
- Query text/IDs/tiers/families are preserved; stable IDs are derived for old
  records without discarding priority.
- Legacy board aliases compile to a board restriction without broadening
  ``jobspy.sites``.
"""

from __future__ import annotations

import logging
import json
import math
import re
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from .models import (
    ApplicantSnapshot,
    EmploymentType,
    MarketLabel,
    MarketId,
    MarketSpec,
    ProbeSpec,
    QuerySpec,
    RoleFilter,
    SearchIntent,
    VariantSpec,
    WorkMode,
)

from .preferences import compile_preferences, compile_exploration
from .countries import COUNTRY_CODES
from .capabilities import CAPABILITIES, JOBSPY_BOARDS

log = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# Validation and execution share the same registered source/board catalog.
KNOWN_SOURCES = frozenset(CAPABILITIES)
KNOWN_BOARDS = frozenset(JOBSPY_BOARDS)

_STANDARD_MARKET_LABELS = {label.value for label in MarketLabel}

_COUNTRY_ALIASES = {
    "pk": "PK",
    "pakistan": "PK",
    "ae": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "sa": "SA",
    "saudi arabia": "SA",
    "us": "US",
    "usa": "US",
    "united states": "US",
    "au": "AU",
    "australia": "AU",
    "nz": "NZ",
    "new zealand": "NZ",
    "ca": "CA",
    "canada": "CA",
    "gb": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "ie": "IE",
    "ireland": "IE",
    "germany": "DE", "netherlands": "NL", "estonia": "EE",
    "singapore": "SG", "qatar": "QA", "bahrain": "BH",
}


class IntentValidationError(ValueError):
    """Raised when the search configuration cannot compile to a valid intent."""


class _Warnings:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def add(self, msg: str) -> None:
        self.messages.append(msg)
        log.warning("search intent: %s", msg)


# --------------------------------------------------------------------------- #
# Small normalization helpers
# --------------------------------------------------------------------------- #


def _as_mapping(value: Any, what: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise IntentValidationError(f"{what} must be a mapping, got {type(value).__name__}")
    return dict(value)


def _as_list(value: Any, what: str) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise IntentValidationError(f"{what} must be a list, got {type(value).__name__}")
    return list(value)


def _as_bool(value: Any, what: str) -> bool | None:
    if value is None or type(value) is bool:
        return value
    raise IntentValidationError(f"{what} must be a YAML boolean, not {value!r}")


def _normalize_country(value: Any, what: str) -> str:
    if not isinstance(value, str):
        raise IntentValidationError(f"{what} must be a country string")
    text = value.strip()
    code = _COUNTRY_ALIASES.get(text.lower(), text.upper())
    if code not in COUNTRY_CODES:
        raise IntentValidationError(f"{what}: unsupported country {value!r}; use an ISO alpha-2 code")
    return code


def _normalize_work_modes(value: Any, what: str) -> tuple[WorkMode, ...]:
    out = []
    for raw in _as_list(value, what):
        try:
            out.append(WorkMode(str(raw).strip().lower()))
        except ValueError:
            raise IntentValidationError(
                f"{what} has unknown work_mode {raw!r}; valid: {[w.value for w in WorkMode]}"
            )
    return tuple(out)


def _normalize_employment_types(value: Any, what: str) -> tuple[EmploymentType, ...]:
    out = []
    for raw in _as_list(value, what):
        try:
            out.append(EmploymentType(str(raw).strip().lower()))
        except ValueError:
            raise IntentValidationError(
                f"{what} has unknown employment_type {raw!r}; valid: {[e.value for e in EmploymentType]}"
            )
    return tuple(out)


# --------------------------------------------------------------------------- #
# Legacy -> v2 translation
# --------------------------------------------------------------------------- #


def _translate_legacy_markets(cfg: Mapping[str, Any], warnings: _Warnings) -> list[dict]:
    """Keep definitions separate from selectors; never manufacture home markets."""
    markets = []
    seen = {}
    for key in ("markets", "locations"):
        for i, raw in enumerate(_as_list(cfg.get(key), key)):
            entry = _as_mapping(raw, f"{key}[{i}]")
            legacy = "label" not in entry
            if legacy:
                location = str(entry.get("location") or "").strip()
                if not location:
                    raise IntentValidationError(f"{key}[{i}].location is required")
                remote = _as_bool(entry.get("remote", cfg.get("remote")), f"{key}[{i}].remote")
                country = entry.get("country", cfg.get("country"))
                cc = _normalize_country(country, f"{key}[{i}].country") if country else None
                worldwide = location.lower() in {"remote", "worldwide", "anywhere", "global"}
                label = "global-remote" if worldwide else (
                    re.sub(r"[^a-z0-9]+", "-", location.lower()).strip("-")
                    + ("-remote" if remote is True else "-nonremote" if remote is False else "-any")
                )
                entry = {
                    "label": label, "location": location,
                    "search_country": cc,
                    "countries": [] if worldwide else ([cc] if cc else []),
                    "regions": ["global"] if worldwide else [],
                    "work_modes": ["remote"] if remote is True or worldwide else
                                  ["onsite", "hybrid"] if remote is False else [],
                    "requires_relocation": entry.get("requires_relocation", cfg.get("requires_relocation")),
                    "requires_clearance": entry.get("requires_clearance", cfg.get("requires_clearance")),
                }
            normalized = _normalize_market_entry(entry)
            label = str(normalized["label"])
            if label in seen:
                if legacy and seen[label] == normalized:
                    warnings.add(f"equivalent legacy market {label!r} deduplicated")
                    continue
                raise IntentValidationError(f"duplicate/conflicting market label {label!r}")
            seen[label] = normalized
            markets.append(normalized)
    return markets


def _normalize_market_entry(entry: Mapping[str, Any]) -> dict:
    try:
        label = MarketId(entry.get("label"))
    except ValueError as exc:
        raise IntentValidationError(str(exc)) from exc
    if "relocation" in entry:
        raise IntentValidationError(f"{label}.relocation is ambiguous; use requires_relocation for movement intent")
    return {
        "label": label,
        "regions": tuple(str(r).strip() for r in _as_list(entry.get("regions"), f"{label}.regions")),
        "countries": tuple(_normalize_country(c, f"{label}.countries") for c in _as_list(entry.get("countries"), f"{label}.countries")),
        "work_modes": _normalize_work_modes(entry.get("work_modes"), f"{label}.work_modes"),
        "employment_types": _normalize_employment_types(entry.get("employment_types"), f"{label}.employment_types"),
        "requires_relocation": _as_bool(entry.get("requires_relocation"), f"{label}.requires_relocation"),
        "requires_clearance": _as_bool(entry.get("requires_clearance"), f"{label}.requires_clearance"),
        "cities": tuple(str(c).strip() for c in _as_list(entry.get("cities"), f"{label}.cities")),
        "location": entry.get("location"),
        "search_country": entry.get("search_country"),
    }


def _default_markets() -> list[dict]:
    """No applicant-specific markets are implicit. Examples supply definitions."""
    return []


def _compile_queries(cfg: Mapping[str, Any]) -> tuple[QuerySpec, ...]:
    out, seen = [], set()
    for i, raw in enumerate(_as_list(cfg.get("queries"), "queries")):
        q = {"query": raw} if isinstance(raw, str) else _as_mapping(raw, f"queries[{i}]")
        text = q.get("query")
        tier = q.get("tier", 1)
        if not isinstance(text, str) or not text.strip():
            raise IntentValidationError(f"queries[{i}].query must be nonempty text")
        if type(tier) is not int or tier < 1:
            raise IntentValidationError(f"queries[{i}].tier must be a positive integer")
        for field in ("id", "family"):
            if field in q and (not isinstance(q[field], str) or not q[field].strip()):
                raise IntentValidationError(f"queries[{i}].{field} must be nonempty text")
        exclude = _as_list(q.get("exclude"), f"queries[{i}].exclude")
        if any(not isinstance(x, str) or not x.strip() for x in exclude):
            raise IntentValidationError(f"queries[{i}].exclude must contain nonempty strings")
        metadata = {k: v for k, v in q.items() if k not in {"query", "tier", "id", "family", "exclude"}}
        try:
            extra = json.dumps(metadata, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise IntentValidationError(f"queries[{i}] has non-JSON or nonfinite metadata") from exc
        spec = QuerySpec(text, tier, q.get("family"), q.get("id"), tuple(exclude), extra)
        if spec.effective_id() in seen:
            raise IntentValidationError(f"duplicate query ID: {spec.effective_id()}")
        seen.add(spec.effective_id())
        out.append(spec)
    return tuple(out)


def _compile_probes(cfg: Mapping[str, Any]) -> tuple[ProbeSpec, ...]:
    if "probes" not in cfg:
        return _derive_probes(cfg.get("queries", []))
    out = []
    for i, raw in enumerate(_as_list(cfg["probes"], "probes")):
        if isinstance(raw, Mapping):
            unknown = set(raw) - {"query", "order"}
            if unknown:
                raise IntentValidationError(f"probes[{i}] unknown fields: {sorted(unknown)}")
            text, order = raw.get("query"), raw.get("order", i)
        else:
            text, order = raw, i
        if not isinstance(text, str) or not text.strip() or type(order) is not int or order < 0:
            raise IntentValidationError(f"probes[{i}] requires text query and nonnegative integer order")
        out.append(ProbeSpec(text, order))
    return tuple(out)


def _derive_probes(queries: list) -> tuple[ProbeSpec, ...]:
    seen = set()
    out = []
    for q in _as_list(queries, "queries"):
        text = q if isinstance(q, str) else (q or {}).get("query")
        if not text:
            continue
        probe = _probe_from_query(str(text))
        if probe and probe not in seen:
            seen.add(probe)
            out.append(ProbeSpec(query=probe, order=len(out)))
        if len(out) >= 8:
            break
    return tuple(out)


def _probe_from_query(query: str) -> str | None:
    low = query.strip().lower()
    if not low:
        return None
    # broad base probes (avoid duplicating every detailed variant)
    for base in ("software engineer", "backend", "full stack", "ai engineer",
                 "platform engineer", "data engineer", "ml engineer"):
        if base in low:
            return base
    return query.strip()


def _compile_role_filter(cfg: Mapping[str, Any]) -> RoleFilter | None:
    rf = cfg.get("role_filter")
    if rf is None:
        # derive from legacy exclude_titles only
        exclusions = cfg.get("exclude_titles")
        if exclusions:
            return RoleFilter(include_any=(), exclude_any=tuple(str(t).strip().lower() for t in _as_list(exclusions, "exclude_titles") if str(t).strip()))
        return RoleFilter()
    rf_m = _as_mapping(rf, "role_filter")
    include = tuple(str(t).strip().lower() for t in _as_list(rf_m.get("include_any"), "role_filter.include_any") if str(t).strip())
    exclude = tuple(str(t).strip().lower() for t in _as_list(rf_m.get("exclude_any"), "role_filter.exclude_any") if str(t).strip())
    return RoleFilter(include_any=include, exclude_any=exclude)


def _compile_variants(cfg: Mapping[str, Any], markets: tuple[MarketSpec, ...]) -> tuple[VariantSpec, ...]:
    valid_labels = {m.label.value for m in markets}
    out = []
    seen_names = set()
    for v in _as_list(cfg.get("variants"), "variants"):
        v_m = _as_mapping(v, "variants entry")
        name = str(v_m.get("name", "")).strip()
        if not name or name in seen_names:
            raise IntentValidationError("variant name must be nonempty and unique")
        seen_names.add(name)
        lower = _as_float(v_m.get("salary_min"), f"variant {name}.salary_min")
        upper = _as_float(v_m.get("salary_max"), f"variant {name}.salary_max")
        if lower is not None and upper is not None and lower > upper:
            raise IntentValidationError(f"variant {name}: salary_min exceeds salary_max")
        market = str(v_m.get("market", "")).strip()
        if market not in valid_labels:
            raise IntentValidationError(f"variant '{name}' references unknown market '{market}'")
        currency = v_m.get("salary_currency")
        period = v_m.get("salary_period")
        if currency is not None and not str(currency).strip():
            raise IntentValidationError(f"variant '{name}' has empty salary_currency")
        if currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency)):
            raise IntentValidationError(f"variant {name}: salary_currency must be an uppercase three-letter code")
        if period is not None and period not in {"year", "month", "week", "day", "hour"}:
            raise IntentValidationError(f"variant {name}: unsupported salary_period")
        if (lower is not None or upper is not None) and (currency is None or period is None):
            raise IntentValidationError(f"variant {name}: salary bounds require currency and period")
        out.append(
            VariantSpec(
                name=name,
                market=MarketId(market),
                salary_currency=currency,
                salary_period=period,
                salary_min=_as_float(v_m.get("salary_min"), f"variant '{name}'.salary_min"),
                salary_max=_as_float(v_m.get("salary_max"), f"variant '{name}'.salary_max"),
            )
        )
    return tuple(out)


def _as_float(value: Any, what: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise IntentValidationError(f"{what} must be a finite nonnegative number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise IntentValidationError(f"{what} must be numeric") from exc
    if not math.isfinite(result) or result < 0:
        raise IntentValidationError(f"{what} must be a finite nonnegative number")
    return result


# --------------------------------------------------------------------------- #
# Sources / boards
# --------------------------------------------------------------------------- #


def compile_source_selection(cfg: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    src = _as_mapping(cfg.get("sources"), "sources")
    js = _as_mapping(cfg.get("jobspy"), "jobspy")
    for path, obj, key in (("sources.enabled", src, "enabled"), ("jobspy.sites", js, "sites")):
        if key in obj and obj[key] is None:
            raise IntentValidationError(f"{path} cannot be null; use [] for none")
    legacy = []
    for path, obj, key in (("sources.sites", src, "sites"), ("sites", cfg, "sites"), ("boards", cfg, "boards")):
        if key in obj:
            if obj[key] is None:
                raise IntentValidationError(f"{path} cannot be null; use [] for none")
            adapters, boards = [], []
            for raw in _as_list(obj[key], path):
                name = _resolve_source_alias(str(raw).strip().lower())
                board = _resolve_board_alias_soft(name)
                if name in KNOWN_SOURCES:
                    adapters.append(name)
                elif board in KNOWN_BOARDS:
                    boards.append(board)
                    adapters.append("jobspy")
                else:
                    raise IntentValidationError(f"{path}: unknown source/board {raw!r}")
            legacy.append((path, tuple(dict.fromkeys(adapters)), tuple(dict.fromkeys(boards)), len(obj[key]) == 0))
    if legacy and any(set(x[1]) != set(legacy[0][1]) or set(x[2]) != set(legacy[0][2]) for x in legacy[1:]):
        raise IntentValidationError("conflicting legacy source/board selections")
    if "enabled" in src:
        adapters = []
        for raw in _as_list(src["enabled"], "sources.enabled"):
            name = _resolve_source_alias(str(raw).strip().lower())
            if name not in KNOWN_SOURCES:
                raise IntentValidationError(f"sources.enabled: unknown adapter {raw!r}; boards belong under jobspy.sites")
            adapters.append(name)
        adapters = tuple(dict.fromkeys(adapters))
        if legacy and set(adapters) != set(legacy[0][1]):
            raise IntentValidationError("sources.enabled conflicts with legacy source selection")
    elif legacy:
        adapters = legacy[0][1]
    else:
        # Match the package's normal default set; browser is a fallback, not an extra crawl.
        adapters = tuple(sorted(KNOWN_SOURCES - {"browseros_linkedin"}))
    if "sites" in js:
        boards = []
        for raw in _as_list(js["sites"], "jobspy.sites"):
            board = _resolve_board_alias(str(raw).strip().lower())
            if board not in KNOWN_BOARDS:
                raise IntentValidationError(f"jobspy.sites: unknown board {raw!r}")
            boards.append(board)
        boards = tuple(dict.fromkeys(boards))
        if legacy and legacy[0][2] and set(boards) != set(legacy[0][2]):
            raise IntentValidationError("jobspy.sites conflicts with legacy board selection")
    elif legacy and (legacy[0][2] or legacy[0][3]):
        boards = legacy[0][2]
    else:
        boards = ("indeed", "linkedin", "google", "naukri", "bayt", "bdjobs") if "jobspy" in adapters else ()
    # Keep the legacy opt-out effective after moving discovery to the scheduler.
    freehire = _as_mapping(cfg.get("freehire"), "freehire")
    if _as_bool(freehire.get("enabled"), "freehire.enabled") is False:
        adapters = tuple(source for source in adapters if source != "freehire")
    return adapters, boards


def _resolve_source_alias(name: str) -> str:
    return _SOURCE_ALIASES.get(name, name)


def _resolve_board_alias(name: str) -> str:
    if name in ("ziprecruiter",):
        raise IntentValidationError(
            "'ziprecruiter' is a standalone adapter; use 'zip_recruiter' for the JobSpy board"        )
    return _BOARD_ALIASES.get(name, name)


def _resolve_board_alias_soft(name: str) -> str:
    """Resolve a board alias without raising; used for legacy mixed lists where
    adapter names are interleaved with boards and are simply ignored."""
    return _BOARD_ALIASES.get(name, name)


# Built-in alias tables (fallbacks).  The shipped config/synonyms.yaml is loaded
# over these so aliases stay editable data, not code.
_BASE_SOURCE_ALIASES = {
    "hn": "hackernews",
    "smart": "smartextract",
    "dice": "smartextract",
    "linkedin_browser": "browseros_linkedin",
}

# Legacy board-source selections map to a board restriction, not an adapter.
_BASE_BOARD_ALIASES = {
    "zip_recruiter": "zip_recruiter",
}


def _load_synonym_aliases(key: str) -> dict[str, str]:
    """Read one alias table from the shipped synonyms.yaml (graceful fallback).

    Compiled intents stay network-free; the file is local and read once at
    import time.  Anything unparseable simply falls back to the built-ins so a
    malformed data file never bricks the compiler.
    """
    try:
        path = Path(__file__).resolve().parent.parent / "config" / "synonyms.yaml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        table = data.get(key) or {}
        if not isinstance(table, dict):
            return {}
        return {
            str(k).strip().lower(): str(v).strip()
            for k, v in table.items()
            if str(k).strip() and str(v).strip()
        }
    except (yaml.YAMLError, OSError, ValueError):
        return {}


_SOURCE_ALIASES: dict[str, str] = dict(_BASE_SOURCE_ALIASES)
_SOURCE_ALIASES.update(_load_synonym_aliases("source_aliases"))

_BOARD_ALIASES: dict[str, str] = dict(_BASE_BOARD_ALIASES)
_BOARD_ALIASES.update(_load_synonym_aliases("board_aliases"))


def _compile_applicant(cfg: Mapping[str, Any]) -> ApplicantSnapshot:
    profile = _as_mapping(cfg.get("applicant"), "applicant")
    country = profile.get("country") or cfg.get("applicant_country")
    if country is not None:
        try:
            country = _normalize_country(country, "applicant.country")
        except IntentValidationError:
            country = str(country).strip().upper()
    return ApplicantSnapshot(
        country=country,
        authorized_countries=tuple(
            _normalize_country(c, "applicant.authorized_countries") for c in _as_list(profile.get("authorized_countries"), "applicant.authorized_countries")
        ),
        citizenship=tuple(str(c).strip() for c in _as_list(profile.get("citizenship"), "applicant.citizenship")),
        clearance=tuple(str(c).strip() for c in _as_list(profile.get("clearance"), "applicant.clearance")),
        seniority=profile.get("seniority"),
        categories=tuple(str(c).strip() for c in _as_list(profile.get("categories"), "applicant.categories")),
    )


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


def compile_search_intent(
    raw_config: Mapping[str, Any] | None,
    applicant_snapshot: ApplicantSnapshot | None = None,
) -> SearchIntent:
    """Compile a raw search-config mapping into a typed :class:`SearchIntent`.

    Network-free and mutation-free.  ``raw_config`` may be a legacy v1 mapping,
    a v2 overlay, or a mix.  Raises :class:`IntentValidationError` on invalid
    or contradictory input.

    Args:
        raw_config: the raw parsed YAML mapping (may be ``None`` -> documented
            defaults).
        applicant_snapshot: optionally override the applicant snapshot derived
            from the config (for callers that hold an already-frozen snapshot).

    Returns:
        The compiled, immutable :class:`SearchIntent`.
    """
    if raw_config is None:
        raw_config = {}
    if not isinstance(raw_config, Mapping):
        raise IntentValidationError(
            f"search config must be a mapping, got {type(raw_config).__name__}"
        )
    cfg = dict(deepcopy(dict(raw_config)))
    warnings = _Warnings()
    _reject_conflicting_legacy(cfg, warnings)

    _validate_schema_version(cfg)

    markets = _translate_legacy_markets(cfg, warnings)
    market_specs = tuple(MarketSpec(**m) for m in markets)
    if any(key in cfg and cfg[key] is None for key in ("location_labels", "tiers")):
        raise IntentValidationError("selectors cannot be null; use [] for none")
    defined_labels = tuple(str(m.label.value) for m in market_specs)
    selected_labels = tuple(_as_list(cfg["location_labels"], "location_labels")) if "location_labels" in cfg else defined_labels
    if any(not isinstance(x, str) for x in selected_labels):
        raise IntentValidationError("location_labels must contain string IDs")
    if len(set(selected_labels)) != len(selected_labels) or set(selected_labels) - set(defined_labels):
        raise IntentValidationError("location_labels has duplicate or undefined market IDs")
    try:
        preferences = compile_preferences(cfg.get("preferences", []), defined_labels)
        exploration = compile_exploration(cfg.get("exploration", {}))
    except ValueError as exc:
        raise IntentValidationError(str(exc)) from exc


    queries = _compile_queries(cfg)
    selected_tiers = tuple(_as_list(cfg["tiers"], "tiers")) if "tiers" in cfg else tuple(sorted({q.tier for q in queries}))
    if any(type(t) is not int or t < 1 for t in selected_tiers):
        raise IntentValidationError("tiers must be positive integers")
    if len(set(selected_tiers)) != len(selected_tiers):
        raise IntentValidationError("tiers contains duplicates")
    probes = _compile_probes(cfg)
    role_filter = _compile_role_filter(cfg)
    variants = _compile_variants(cfg, market_specs)
    sources, boards = compile_source_selection(cfg)

    exclude_titles = tuple(
        str(t).strip().lower()
        for t in _as_list(cfg.get("exclude_titles"), "exclude_titles")
        if str(t).strip()
    )

    intent = SearchIntent(
        schema_version=SCHEMA_VERSION,
        sources_enabled=sources,
        jobspy_sites=boards,
        queries=queries,
        probes=probes,
        markets=market_specs,
        role_filter=role_filter,
        variants=variants,
        exclude_titles=exclude_titles,
        selected_tiers=selected_tiers,
        active_market_labels=selected_labels,
        preferences=preferences,
        exploration=exploration,
    )
    return intent


def _validate_schema_version(cfg: Mapping[str, Any]) -> None:
    if "schema_version" not in cfg:
        return
    raw = cfg["schema_version"]
    if type(raw) is not int or raw not in (1, SCHEMA_VERSION):
        raise IntentValidationError(f"schema_version must be integer 1 or {SCHEMA_VERSION}")


def build_applicant_snapshot(raw_config: Mapping[str, Any] | None) -> ApplicantSnapshot:
    """Build an immutable applicant snapshot from a config (or empty)."""
    if raw_config is None:
        return ApplicantSnapshot()
    if not isinstance(raw_config, Mapping):
        raise IntentValidationError("search config must be a mapping to build an applicant snapshot")
    return _compile_applicant(raw_config)


def _reject_conflicting_legacy(cfg: Mapping[str, Any], warnings: _Warnings) -> None:
    compile_source_selection(cfg)

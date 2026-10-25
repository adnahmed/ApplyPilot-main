"""Typed discovery domain models for ApplyPilot v2.

These records centralize the configuration *intent* and the runtime
*candidate/assessment* contracts so adapters, the planner, the scheduler and
downstream admission can share one vocabulary.

Design rules drawn from the v2 handoff:

- Configuration records are immutable (:class:`dataclasses.frozen`). No mutable
  dictionaries are hidden inside a supposedly-immutable intent record.
- ``unknown`` is a first-class tri-state; it is never collapsed into ``false``
  or ``not_eligible``.
- Office/HQ/work locations are kept distinct from applicant-eligibility
  countries, exclusions, residence, authorization, clearance, timezone,
  sponsorship and relocation support.
- Source listing identity, tenant/API region, posting ID, internal/requisition
  IDs, listing URL, canonical target and application route stay distinct.
- A discovery disposition is a *projection* used for planning/admission; it is
  never a replacement for application-lifecycle state.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
import re

from .preferences import ArrangementFacts, PreferenceDecision, PreferenceRule, ExplorationPolicy

# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class TriState(str, Enum):
    """Explicit three-valued boolean for facts whose absence is ``unknown``."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


class WorkMode(str, Enum):
    REMOTE = "remote"
    ONSITE = "onsite"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class EmploymentType(str, Enum):
    """employment_types is an any-of *intent*; schedule/contract stay separate."""

    FULL_TIME = "full_time"
    PART_TIME = "part_time"
    CONTRACT = "contract"
    PERMANENT = "permanent"
    TEMP = "temp"
    INTERNSHIP = "internship"
    UNKNOWN = "unknown"


class MarketId(str):
    """User-defined stable ID; ``.value`` preserves existing enum-style readers."""

    def __new__(cls, value):
        value = value.value if isinstance(value, Enum) else value
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
            raise ValueError("market ID must match [a-z][a-z0-9_-]{0,63}")
        return super().__new__(cls, value)

    @property
    def value(self) -> str:
        return str(self)


class MarketLabel(str, Enum):
    """The labelled active markets the user pursues."""

    GLOBAL_REMOTE = "global-remote"
    HOME_REMOTE = "home-remote"
    RELOCATION_UAE = "relocation-uae"
    RELOCATION_SAUDI = "relocation-saudi"


class Disposition(str, Enum):
    """Discovery disposition — distinct from application lifecycle state."""

    CANDIDATE = "candidate"
    NEEDS_EVIDENCE = "needs_evidence"
    OFF_PORTFOLIO = "off_portfolio"
    BLOCKED = "blocked"


class Relevance(str, Enum):
    RELEVANT = "relevant"
    IRRELEVANT = "irrelevant"
    UNKNOWN = "unknown"


class Compatibility(str, Enum):
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    UNKNOWN = "unknown"


class RequirementStatus(str, Enum):
    MET = "met"
    CONFLICT = "conflict"
    UNKNOWN = "unknown"


class Coverage(str, Enum):
    """Coverage classification for a fetched page/snapshot."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    CAPPED = "capped"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class FailureKind(str, Enum):
    """Typed fetch failures; successful-empty and coverage are not failures."""

    SCHEMA = "schema"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    ACCESS_DENIED = "access_denied"
    CONFIGURATION = "configuration"
    TIMEOUT = "timeout"
    COORDINATION = "coordination"
    MODEL = "model"
    UNKNOWN = "unknown"


class Authority(str, Enum):
    """Authority category for an evidence fact."""

    EMPLOYER = "employer"  # direct employer restriction/support statement
    AGGREGATOR = "aggregator"  # board/aggregator classification
    MODEL = "model"  # model-extracted guess
    RULE = "rule"  # deterministic project rule
    PROFILE = "profile"  # applicant profile fact


# --------------------------------------------------------------------------- #
# Configuration / intent records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class QuerySpec:
    """A single tiered search query with a stable identity."""

    query: str
    tier: int
    family: str | None = None
    id: str | None = None  # stable id; derived deterministically when absent
    exclude: tuple[str, ...] = ()
    metadata_json: str = "{}"  # immutable, lossless storage for extra query metadata

    def effective_id(self) -> str:
        if self.id:
            return self.id
        return _stable_id(f"query|{self.tier}|{self.query}".lower())


@dataclass(frozen=True)
class ProbeSpec:
    """A broad, cheap search probe (as opposed to a detailed tiered query)."""

    query: str
    order: int = 0

    def effective_id(self) -> str:
        return _stable_id(f"probe|{self.query}".lower())


@dataclass(frozen=True)
class MarketSpec:
    """One labelled active-market definition."""

    label: MarketId | MarketLabel
    regions: tuple[str, ...] = ()
    countries: tuple[str, ...] = ()  # applicant-eligible target countries
    work_modes: tuple[WorkMode, ...] = ()
    employment_types: tuple[EmploymentType, ...] = ()
    requires_relocation: bool | None = None  # candidate movement intent, not employer support
    requires_clearance: bool | None = None
    cities: tuple[str, ...] = ()
    location: str | None = None  # search target text, never residence evidence
    search_country: str | None = None  # legacy country-bound index target


@dataclass(frozen=True)
class VariantSpec:
    """An additive compensation/eligibility discovery shot."""

    name: str
    market: MarketId | MarketLabel
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None


@dataclass(frozen=True)
class RoleFilter:
    """Cheap shared role gate: positive admission + explicit exclusions."""

    include_any: tuple[str, ...] = ()
    exclude_any: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.include_any and not self.exclude_any


@dataclass(frozen=True)
class SearchIntent:
    """The fully compiled, immutable search intent for one run.

    ``sources.enabled`` are the enabled *adapters*; ``jobspy.sites`` are the
    enabled *boards*. Missing keys are represented by explicit empty vs None so
    callers can distinguish "use documented defaults" from "explicitly none".
    """

    schema_version: int
    sources_enabled: tuple[str, ...]  # canonical adapter names
    jobspy_sites: tuple[str, ...]  # board aliases (already resolved)
    queries: tuple[QuerySpec, ...]
    probes: tuple[ProbeSpec, ...]
    markets: tuple[MarketSpec, ...]
    role_filter: RoleFilter | None = None
    variants: tuple[VariantSpec, ...] = ()
    exclude_titles: tuple[str, ...] = ()
    salary_currency: str | None = None
    salary_period: str | None = None
    salary_min: float | None = None
    selected_tiers: tuple[int, ...] = ()
    active_market_labels: tuple[str, ...] = ()
    preferences: tuple[PreferenceRule, ...] = ()
    exploration: ExplorationPolicy = field(default_factory=ExplorationPolicy)
    target_policies_json: str = "[]"

    def active_queries(self) -> tuple[QuerySpec, ...]:
        return tuple(q for q in self.queries if q.tier in self.selected_tiers)

    def active_markets(self) -> tuple[MarketSpec, ...]:
        definitions = {str(m.label.value): m for m in self.markets}
        return tuple(definitions[label] for label in self.active_market_labels)

    def fingerprint(self) -> str:
        """All intent fields affect the revision; observations do not live here."""
        return _stable_id(json.dumps(asdict(self), sort_keys=True,
                                     separators=(",", ":"), allow_nan=False))


@dataclass(frozen=True)
class ApplicantSnapshot:
    """Immutable applicant facts frozen for the whole run.

    ``country`` remains the residence field for existing callers. Empty fact
    collections mean unknown, not "none exist". Unscoped answers never grant
    country-specific rights, and citizenship never implies authorization here.
    """

    country: str | None = None  # applicant/residence country (alpha-2)
    authorized_countries: tuple[str, ...] = ()
    citizenship: tuple[str, ...] = ()
    clearance: tuple[str, ...] = ()
    seniority: str | None = None
    categories: tuple[str, ...] = ()
    metrics: tuple[dict[str, Any], ...] = ()
    city: str | None = None
    province_state: str | None = None
    legally_authorized_to_work: TriState = TriState.UNKNOWN  # unscoped legacy answer
    require_sponsorship: TriState = TriState.UNKNOWN  # unscoped legacy answer
    work_permit_type: str | None = None
    authorization_by_country: tuple[tuple[str, TriState], ...] = ()
    sponsorship_by_country: tuple[tuple[str, TriState], ...] = ()

    def __post_init__(self) -> None:
        from collections.abc import Mapping
        from types import MappingProxyType
        from .countries import COUNTRY_CODES

        def country(value):
            if not isinstance(value, str) or value.strip().upper() not in COUNTRY_CODES:
                raise ValueError("ApplicantSnapshot countries must be ISO alpha-2 codes")
            return value.strip().upper()

        def sequence(value, name):
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"ApplicantSnapshot.{name} must be a list or tuple")
            return value

        def state(value):
            if not isinstance(value, (str, TriState)):
                raise ValueError("ApplicantSnapshot states must be yes, no or unknown")
            return TriState(value)

        def freeze(value):
            if isinstance(value, Mapping):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("ApplicantSnapshot.metrics keys must be strings")
                return MappingProxyType({key: freeze(item) for key, item in value.items()})
            if isinstance(value, (list, tuple)):
                return tuple(freeze(item) for item in value)
            if value is None or type(value) in (str, bool, int, float):
                json.dumps(value, allow_nan=False)
                return value
            raise ValueError("ApplicantSnapshot.metrics must contain JSON values")

        if self.country is not None:
            object.__setattr__(self, "country", country(self.country))
        for name in ("authorized_countries", "citizenship", "clearance", "categories"):
            values = sequence(getattr(self, name), name)
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise ValueError(f"ApplicantSnapshot.{name} must contain nonempty strings")
            values = [country(value) if name in ("authorized_countries", "citizenship")
                      else value.strip() for value in values]
            object.__setattr__(self, name, tuple(sorted(set(values))))
        for name in ("seniority", "city", "province_state", "work_permit_type"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"ApplicantSnapshot.{name} must be a string or null")
            object.__setattr__(self, name, value.strip() or None if value is not None else None)
        for name in ("legally_authorized_to_work", "require_sponsorship"):
            object.__setattr__(self, name, state(getattr(self, name)))
        for name in ("authorization_by_country", "sponsorship_by_country"):
            facts = {}
            for entry in sequence(getattr(self, name), name):
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    raise ValueError(f"ApplicantSnapshot.{name} entries must be country/state pairs")
                code, answer = country(entry[0]), state(entry[1])
                if code in facts:
                    raise ValueError(f"ApplicantSnapshot.{name} has duplicate countries")
                facts[code] = answer
            if name == "authorization_by_country":
                for code in self.authorized_countries:
                    if code in facts and facts[code] != TriState.YES:
                        raise ValueError("authorized_countries conflicts with authorization_by_country")
                    facts[code] = TriState.YES
                object.__setattr__(self, "authorized_countries", tuple(sorted(
                    code for code, answer in facts.items() if answer == TriState.YES
                )))
            object.__setattr__(self, name, tuple(sorted(facts.items())))
        metrics = sequence(self.metrics, "metrics")
        if any(not isinstance(item, Mapping) for item in metrics):
            raise ValueError("ApplicantSnapshot.metrics must contain mappings")
        object.__setattr__(self, "metrics", tuple(freeze(item) for item in metrics))

    def authorization_for(self, alpha2: str) -> TriState:
        return dict(self.authorization_by_country).get(alpha2.upper(), TriState.UNKNOWN)

    def sponsorship_for(self, alpha2: str) -> TriState:
        return dict(self.sponsorship_by_country).get(alpha2.upper(), TriState.UNKNOWN)

    def fingerprint(self) -> str:
        """Canonical SHA-256 of all snapshot facts, excluding unrelated profile PII."""
        from collections.abc import Mapping
        from dataclasses import fields

        def plain(value):
            if isinstance(value, Mapping):
                return {key: plain(item) for key, item in value.items()}
            if isinstance(value, tuple):
                return [plain(item) for item in value]
            return value

        payload = {item.name: plain(getattr(self, item.name)) for item in fields(self)}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def residence_is(self, alpha2: str) -> bool:
        return (self.country or "").upper() == alpha2.upper()


# --------------------------------------------------------------------------- #
# Runtime records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceFact:
    """One decision-bearing fact with provenance, not an eligibility assessment.

    IDs are supplied by the producer and retained across observations, not
    generated from mutable values or observation times. An absent arrangement
    or country scope is unknown applicability, not support for every alternative.
    ``value`` must contain normalized JSON values, not provider NA sentinels.
    """

    value: Any
    kind: str
    authority: Authority
    source_observation: str | None = None
    provider_field_path: str | None = None
    excerpt: str | None = None
    observed_at: str | None = None
    source_time: str | None = None
    rule_version: str | None = None
    id: str | None = None
    arrangement_id: str | None = None
    countries: tuple[str, ...] = ()  # scope of this evidence, not eligible countries
    source_url: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None


@dataclass(frozen=True)
class WorkArrangement:
    """One supported alternative, never a country x work-mode cartesian product.

    ``id`` is a producer-owned stable ID within the posting, not a content hash
    or a list position. None means no stable identity has been established.
    Empty collections mean unknown, not unrestricted. Countries use ISO alpha-2;
    location/requirement text retains subnational and unparsed constraints.
    Office geography and remote mode never populate applicant eligibility.

    Schedule (e.g. full_time) does not establish engagement (direct_employee,
    contractor or eor). Pay basis (global/localized) needs applicable policy
    evidence, not currency or HQ. Relocation required describes movement under
    this option; it does not assess whether a particular applicant must move.
    Evidence IDs refer to JobCandidate.facts; nothing here grants admission.
    """

    work_mode: WorkMode = WorkMode.UNKNOWN
    countries: tuple[str, ...] = ()  # applicant-eligible/allowed countries
    regions: tuple[str, ...] = ()
    office_locations: tuple[str, ...] = ()  # distinct from eligibility
    focus: str = "applicant"  # which arrangement this describes
    id: str | None = None
    cities: tuple[str, ...] = ()  # explicitly eligible cities, not offices
    eligible_scope: str | None = None  # worldwide / restricted, only with evidence
    excluded_countries: tuple[str, ...] = ()
    excluded_regions: tuple[str, ...] = ()
    excluded_locations: tuple[str, ...] = ()  # cities/states or original exclusion text
    office_countries: tuple[str, ...] = ()
    residency_required: TriState = TriState.UNKNOWN
    residency_countries: tuple[str, ...] = ()
    residency_requirements: tuple[str, ...] = ()
    authorization_required: TriState = TriState.UNKNOWN
    authorization_countries: tuple[str, ...] = ()
    authorization_requirements: tuple[str, ...] = ()
    citizenship_required: TriState = TriState.UNKNOWN
    citizenship_countries: tuple[str, ...] = ()
    clearance_required: TriState = TriState.UNKNOWN
    clearance_requirements: tuple[str, ...] = ()
    timezone_requirements: tuple[str, ...] = ()  # zones, offset windows, overlap text
    schedule: str | None = None
    engagement: str | None = None
    pay_basis: str | None = None
    relocation_required: TriState = TriState.UNKNOWN
    relocation_supported: TriState = TriState.UNKNOWN
    sponsorship_supported: TriState = TriState.UNKNOWN
    legal_employer: str | None = None
    legal_employer_country: str | None = None
    payroll_intermediary: str | None = None
    evidence_ids: tuple[str, ...] = ()

    # Each object stays attached to its source alternative; never zip flattened
    # names/countries or multiply office geography by remote eligibility.
    office_details: tuple[dict[str, Any], ...] = ()
    metric_requirements: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CompensationRange:
    """Compensation with explicit currency/period; no silent FX/annualization.

    An absent arrangement ID means applicability is unresolved, not that every
    alternative gets this range. Pay basis is a supported global/localized
    policy, never inferred from amount, currency, period, or employer location.
    """

    currency: str | None = None
    period: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    components: tuple[str, ...] = ()  # base/equity/etc. preserve tiers
    original_text: str | None = None
    arrangement_id: str | None = None
    pay_basis: str | None = None
    evidence_ids: tuple[str, ...] = ()

    def disclosed(self) -> bool:
        """True when a figure, component or original compensation text was given."""
        return (
            self.min_value is not None
            or self.max_value is not None
            or bool(self.components)
            or (self.original_text is not None and self.original_text.strip() != "")
        )

    def is_concrete(self) -> bool:
        """True when a bounded, currency-aware range is present."""
        return (
            self.currency is not None
            and self.min_value is not None
            and self.max_value is not None
        )


@dataclass(frozen=True)
class SourceIdentity:
    """Strict source scope + posting identity."""

    provider: str
    tenant: str | None = None  # board token / tenant / site subdomain
    api_region: str | None = None  # e.g. lever namespace, workday subdomain
    posting_id: str | None = None
    requisition_id: str | None = None
    internal_id: str | None = None
    listing_url: str | None = None

    def scope_key(self) -> tuple[str, str | None, str | None]:
        """Provider + tenant + api region — identifies a board scope."""
        return (self.provider, self.tenant, self.api_region)

    def posting_key(self) -> tuple[str, str | None, str | None, str | None]:
        """Provider + tenant + region + posting id — strict posting identity."""
        return self.scope_key() + (self.posting_id,)


@dataclass(frozen=True)
class JobCandidate:
    """Normalized, versioned candidate facts for one observation.

    ``facts`` holds decision-bearing :class:`EvidenceFact` entries; raw values
    and provider field paths are preserved inside the facts, so adapters do not
    lose the original evidence. ``company`` retains the source display label;
    it does not establish the hiring organization or a legal employer. HQ is
    geography of the hiring organization, never role eligibility or pay policy.
    Job-level legal/payroll fields require posting-wide support; option-specific
    entities belong on WorkArrangement and are not automatically inherited.
    """

    title: str | None = None
    company: str | None = None
    description: str | None = None
    identity: SourceIdentity | None = None
    work_arrangements: tuple[WorkArrangement, ...] = ()
    compensation: tuple[CompensationRange, ...] = ()
    requirements: dict[str, TriState] = field(default_factory=dict)
    application_url: str | None = None
    canonical_target: str | None = None
    source_posted_at: str | None = None
    source_updated_at: str | None = None
    facts: tuple[EvidenceFact, ...] = ()
    normalizer_version: str | None = None
    hiring_organization: str | None = None
    hiring_organization_id: str | None = None
    hiring_organization_hq_country: str | None = None
    hiring_organization_hq_location: str | None = None
    legal_employer: str | None = None
    legal_employer_country: str | None = None
    payroll_intermediary: str | None = None

    def content_hash(self) -> str:
        """Revision fence over every normalized field, including nested evidence.

        Only EvidenceFact.observed_at (retrieval time) is excluded. Source posted/
        updated times, evidence source_time and validity windows are retained:
        they can affect freshness, source revisions or policy applicability.
        Evidence identity, provenance, scope and normalizer version all count.
        Mapping order is ignored; sequence order is preserved conservatively.
        Non-JSON values and NaN/Infinity fail rather than get stringified.
        Adding a model field includes it automatically unless explicitly omitted.
        """
        payload = asdict(self)
        # Retrieval snapshots and their query origins are provenance, not job
        # content. Paths/IDs can change when the same statement is enriched;
        # preserve its actual value, scope, authority and validity instead.
        def semantic_fact(fact):
            return {**{key: value for key, value in fact.items() if key not in {
                "id", "observed_at", "source_observation", "provider_field_path", "excerpt",
            }}, "supported_locator": bool(fact.get("provider_field_path") or fact.get("excerpt")),
                "identity_count": id_counts[fact["id"]] if fact["id"] else 0}
        facts = payload["facts"]
        from collections import Counter
        id_counts = Counter(fact["id"] for fact in facts)
        payload["evidence_identity_validity"] = {
            "missing_ids": sum(not fact["id"] for fact in facts),
            "duplicate_ids": sorted(count for count in id_counts.values() if count > 1),
            "cross_option_references": sum(
                not any(fact["id"] == eid and fact.get("arrangement_id") == option.get("id") for fact in facts)
                for option in payload["work_arrangements"] for eid in option["evidence_ids"]),
        }
        by_id = {fact["id"]: fact for fact in facts}
        for option in payload["work_arrangements"] + payload["compensation"]:
            option["evidence_ids"] = [semantic_fact(by_id[eid]) if eid in by_id else {"missing": eid}
                                      for eid in option["evidence_ids"]]
            option["evidence_ids"].sort(key=lambda value: json.dumps(value, sort_keys=True))
        payload["facts"] = [semantic_fact(fact) for fact in facts
                            if fact["kind"] not in {"raw_record", "normalized_record"}]
        payload["facts"].sort(key=lambda value: json.dumps(value, sort_keys=True))
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MarketResult:
    """Per-market assessment for one candidate."""

    label: MarketId | MarketLabel
    compatibility: Compatibility
    reasons: tuple[str, ...] = ()
    evidence: tuple[EvidenceFact, ...] = ()
    unresolved: tuple[str, ...] = ()  # material unresolved questions


@dataclass(frozen=True)
class CandidateAssessment:
    """The full deterministic assessment of one candidate against an intent.

    ``disposition`` is a discovery projection for planning/admission — never an
    application-lifecycle replacement.
    """

    role: Relevance
    role_reasons: tuple[str, ...] = ()
    market_results: tuple[MarketResult, ...] = ()
    requirements: dict[str, RequirementStatus] = field(default_factory=dict)
    disposition: Disposition = Disposition.OFF_PORTFOLIO
    unresolved: tuple[str, ...] = ()
    intent_fingerprint: str | None = None
    profile_version: str | None = None
    rules_version: str | None = None

    content_hash: str | None = None
    preference: PreferenceDecision | None = None
    arrangement_results: tuple[ArrangementFacts, ...] = ()
    prepare_allowed: bool = False
    apply_allowed: bool = False
    assumptions: tuple[dict[str, Any], ...] = ()
    admission_policy_version: str | None = None

    def labeled_markets(self) -> tuple[MarketLabel, ...]:
        """Markets with positive compatible support (a projection, not proof)."""
        return tuple(m.label for m in self.market_results if m.compatibility is Compatibility.COMPATIBLE)


# --------------------------------------------------------------------------- #
# Task / page / ingestion records
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceTarget:
    """A concrete thing to fetch (board refresh, query page, signal probe)."""

    provider: str
    scope: str | None = None  # tenant / token / subdomain
    kind: str = "query"  # query | board_refresh | employer_probe | signal
    request_params: tuple[tuple[str, Any], ...] = ()
    origins: tuple[str, ...] = ()  # contributing query/market/variant keys
    budget_name: str | None = None
    native_targets: tuple[str, ...] = ()

    def request_key(self) -> str:
        """Logical request identity. Local market labels are excluded unless they
        actually change the request, so add only parameters/scope here."""
        params = dict(self.request_params)
        if len(params) != len(self.request_params) or any(not isinstance(key, str) for key in params):
            raise ValueError("request parameters require unique string keys")
        payload = {
            "provider": self.provider,
            "scope": self.scope,
            "kind": self.kind,
            # only JSON-safe, provider-affecting values belong in the key
            "params": _jsonable(sorted(params.items())),
        }
        return _stable_id(json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False))


@dataclass(frozen=True)
class SearchTask:
    """A scheduled discovery task bound to a logical request and refresh generation."""

    task_id: str | None = None
    logical_request_key: str | None = None
    refresh_generation: int | None = None
    target: SourceTarget | None = None
    status: str = "pending"  # pending | running | done | failed | deferred
    next_eligible_at: float | None = None  # numeric UTC
    attempts: int = 0
    lease_token: str | None = None
    lease_expires_at: float | None = None
    intent_fingerprint: str | None = None
    profile_version: str | None = None


@dataclass(frozen=True)
class TaskLease:
    task_id: str
    token: str
    expires_at: float
    revision: str | None = None


@dataclass(frozen=True)
class FetchFailure:
    kind: FailureKind
    message: str
    http_status: int | None = None
    retry_after: float | None = None
    provider_field: str | None = None


@dataclass(frozen=True)
class DiscoveryPage:
    """One fetched page/snapshot with records, cursor, coverage and metadata."""

    task_id: str
    records: tuple[dict[str, Any], ...] = ()
    next_cursor: str | None = None
    exhausted: bool = False
    coverage: Coverage = Coverage.COMPLETE
    complete: bool = True
    partial: bool = False
    capped: bool = False
    request_metadata: dict[str, Any] = field(default_factory=dict)
    snapshot_identity: str | None = None
    failures: tuple[FetchFailure, ...] = ()


@dataclass(frozen=True)
class IngestionResult:
    """Counters from one page publication — distinguishes outcome classes."""

    fetched: int = 0
    valid: int = 0
    new: int = 0
    updated: int = 0
    strict_duplicates: int = 0
    compatible: int = 0
    unresolved: int = 0
    off_portfolio: int = 0
    blocked: int = 0
    admitted: int = 0
    checkpoint_advanced: bool = False

    def merge(self, other: IngestionResult) -> IngestionResult:
        return IngestionResult(
            fetched=self.fetched + other.fetched,
            valid=self.valid + other.valid,
            new=self.new + other.new,
            updated=self.updated + other.updated,
            strict_duplicates=self.strict_duplicates + other.strict_duplicates,
            compatible=self.compatible + other.compatible,
            unresolved=self.unresolved + other.unresolved,
            off_portfolio=self.off_portfolio + other.off_portfolio,
            blocked=self.blocked + other.blocked,
            admitted=self.admitted + other.admitted,
            checkpoint_advanced=self.checkpoint_advanced or other.checkpoint_advanced,
        )


@dataclass(frozen=True)
class StageResult:
    """Structured result for a completed stage/pass."""

    stage: str
    ok: bool = True
    counts: dict[str, int] = field(default_factory=dict)
    errors: tuple[str, ...] = ()
    next_due_at: float | None = None
    message: str | None = None

    def progress(self) -> int:
        if not self.counts:
            return 0
        return int(self.counts.get("new", 0) + self.counts.get("updated", 0))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _stable_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _variant_dict(v: VariantSpec) -> dict[str, Any]:
    return {
        "name": v.name,
        "market": v.market.value,
        "salary_currency": v.salary_currency,
        "salary_period": v.salary_period,
        "salary_min": v.salary_min,
        "salary_max": v.salary_max,
    }


def _jsonable(pairs: Iterable[tuple[str, Any]]) -> Any:
    out = []
    for k, v in pairs:
        if isinstance(v, Enum):
            v = v.value
        out.append((k, v))
    return out

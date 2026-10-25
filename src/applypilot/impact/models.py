"""Typed impact-evidence models.

These are the canonical in-memory shapes for the Impact Evidence Ledger.
JSON exists only at the repository adapter boundary (``ledger.py``) — every
downstream consumer (matcher, draft, validator, tailor, cover letter) works
with these dataclasses, never raw dicts.

Design lineage (2026-09-01, CLAUDE.md decision #65):
- business-impact layers follow the McKinsey 5-layer chain
  technical -> adoption -> operational -> customer -> financial
- ``claimability`` bounds how far a claim can be projected from the evidence;
  ``hypothesis``/``unknown`` outcomes are never resume-eligible.
- ``measurement_status`` is separate from the value itself so a number that the
  source states (even approximately, e.g. "~25%") is ``measured`` with
  ``value_qualifier`` capturing the approximation, distinguishable from an
  ``estimated`` number the source never stated.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

# ── Enums ─────────────────────────────────────────────────────────────────

LAYERS = ("technical", "adoption", "operational", "customer", "financial")
MEASUREMENT_STATUSES = ("measured", "calculated", "estimated", "unmeasured")

# How the numeric value was bounded in the source text.
VALUE_QUALIFIERS = ("exact", "approximate", "range", "less_than", "at_most",
                    "greater_than", "at_least")
CLAIMABILITY_VALUES = ("direct", "derived", "supported", "hypothesis", "unknown")
ATTRIBUTION_VALUES = (
    "direct_owner", "primary_contributor", "team_contributor", "correlation_only",
)
CONFIDENCE_VALUES = ("high", "medium", "low")
ATTRIBUTION_CONFIDENCE_VALUES = ("high", "medium", "low", "correlational")
DECISION_CONSEQUENCE_TYPES = ("scaled", "adopted", "standardized", "expanded", "stopped", "none")
BRIEF_READINESS_VALUES = ("strong", "usable", "weak")
REVIEW_STATUSES = ("drafted", "auto_approved", "reviewed", "rejected")
SCOPE_TYPES = ("employment", "project", "education", "volunteer", "other")

# Canonical placeholder values that LLM parsers emit as scope fields but that
# are not real entity names.  Normalize to None *before* validation so they
# never become approved ledger data.
_INVALID_SCOPE_VALUES = frozenset({
    "", ":", ":unknown", "unknown", "n/a", "na", "none", "null", "-",
    "self", "personal", "various", "multiple", "misc", "miscellaneous",
})

# Outcome claim values that are NOT real claims — they are
# claimability classifications or parser artifacts the model
# sometimes emits in the claim field instead of the claimability field.
_INVALID_OUTCOME_CLAIMS = frozenset({
    "direct", "derived", "supported", "hypothesis", "unknown",
    "measured", "calculated", "estimated", "unmeasured",
    "yes", "no", "true", "false",
})

# Recommended representations for a measurement's numeric value. Custom values
# are tolerated on load but flagged: matching/linting code operates on these.
#
# The representation determines which numeric field the value lives in (this is
# enforced by ``normalize_measurement`` in stages.py, which derives ``value`` /
# ``delta_percent`` from a single ``numeric_value`` so the LLM JSON schema and
# ``Measurement.validate()`` can never disagree):
#
# - ``absolute_value``     value = the stated level/rate ("5% payment failure rate")
# - ``percent_change``     delta_percent = the percentage change; direction
#                          (decrease/increase) carries the sign
#                          ("payment failures fell 25%")
# - ``baseline_to_result`` value = the result/post-change number
# - ``ratio``              value = the multiplier ("2x throughput")
# - ``per_time``           value = the per-time number ("12 hrs -> 3 hrs")
REPRESENTATIONS = (
    "absolute_value",     # "the system handles 800 RPS"
    "percent_change",     # "reduced X by 25%" / "raised conversion 12%"
    "baseline_to_result", # "30s -> <1s"
    "ratio",              # "2x throughput"
    "per_time",           # "12 hrs -> 3 hrs"
)

# Grounded claimability — an outcome in this set is assertable on a resume.
GROUNDED_CLAIMABILITY = ("direct", "derived", "supported")

# Grounded measurement statuses — a measurement in this set anchors a layer.
GROUNDED_MEASUREMENT_STATUSES = ("measured", "calculated")

# Ordinal anchors for business depth, capped by GROUNDED evidence (matcher):
BUSINESS_DEPTH_ORDINAL = {
    "technical": 0.20,
    "adoption": 0.40,
    "operational": 0.60,
    "customer": 0.80,
    "financial": 1.00,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _ensure(value, allowed: tuple[str, ...], label: str) -> str:
    """Return ``value`` if it is one of ``allowed``, else raise ValueError."""
    if value not in allowed:
        raise ValueError(f"Invalid {label}: {value!r} (expected one of {allowed})")
    return value


def _sanitize_scope_field(value: str | None) -> str | None:
    """Normalize placeholder/garbage scope values to None.

    LLM parsers frequently emit `:`, `:unknown`, `unknown`, `n/a`, etc. as
    company/role/project values.  These are truthy strings that pass
    ``Scope.validate()`` but are not real entity names and should never
    appear in approved ledger data.
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized in _INVALID_SCOPE_VALUES:
        return None
    return value


# ── Sub-structures ────────────────────────────────────────────────────────

@dataclass
class Measurement:
    """One quantitative claim, canonically bound to its intervention.

    Semantic binding (validator contract): a rendered bullet matches this
    measurement only when number + unit + metric semantics + direction all
    align — never by raw number alone.

    Canonical meaning of ``value`` (model invariant, not a prompt convention):
    it is THE current/result numeric value of the KPI, the single number the
    verifier checks exists whenever a number is grounded in the text. Per
    ``representation``:

    - ``absolute_value``      value = the stated value (its own unit)
    - ``baseline_to_result``  value = the result/post-change number; ``baseline``
                              and ``result`` hold the full stated strings
                              (e.g. value 3, baseline "9 hours",
                              result "3 hours" from "from 9 hours to 3 hours")
    - ``percent_change``      value = None; ``delta_percent`` = the change, and
                              ``direction`` (decrease/increase) carries the sign
                              ("payment failures fell 25%" -> delta 25, decrease)
    - ``ratio`` / ``per_time``
                              value = the multiplier or per-time number
    - ``unmeasured``          value = null (qualitative claim only)
    """
    name: str
    claim: str
    layer: str = "technical"
    measurement_status: str = "unmeasured"
    representation: str = "absolute_value"
    direction: str = "none"            # increase | decrease | none
    value: float | None = None          # canonical current/result number of the KPI (see class docstring)
    unit: str | None = None             # "%", "ms", "sec", "RPS", "$", ...
    quantity_text: str | None = None    # exact quantitative phrase copied from source
    delta_percent: float | None = None
    value_qualifier: str = "exact"      # exact | approximate | range | less_than | at_most | greater_than | at_least
    value_min: float | None = None      # for ranges: lower bound (e.g. 50000 for "50k-100k")
    value_max: float | None = None      # for ranges: upper bound (e.g. 100000 for "50k-100k")
    baseline: str | None = None         # "30+ seconds"
    result: str | None = None           # "<1 second"
    source_ref: str | None = None       # "resume.txt:L42" — provenance binding
    source_text: str | None = None      # the verbatim input the claim restates
    method: "MeasurementMethod | None" = None   # how THIS KPI was measured (per-KPI)

    def validate(self) -> list[str]:
        errors: list[str] = []
        try:
            _ensure(self.layer, LAYERS, "measurement.layer")
            _ensure(self.measurement_status, MEASUREMENT_STATUSES, "measurement.measurement_status")
        except ValueError as exc:
            errors.append(str(exc))
        if self.direction not in ("increase", "decrease", "none"):
            errors.append(f"Invalid measurement.direction: {self.direction!r}")
        if not self.name.strip():
            errors.append("measurement.name is required")
        if not self.claim.strip():
            errors.append("measurement.claim is required")
        if self.value_qualifier not in VALUE_QUALIFIERS:
            errors.append(f"Invalid measurement.value_qualifier: {self.value_qualifier!r}")
        if self.value_qualifier == "range":
            if self.value_min is None or self.value_max is None:
                errors.append(f"measurement {self.name}: range qualifier requires value_min and value_max")
            if self.value is not None:
                errors.append(f"measurement {self.name}: range qualifier should not have a value (use value_min/value_max)")
        if self.delta_percent is not None:
            # delta_percent is ONLY a percentage CHANGE; it lives only on
            # percent_change. Any other representation that sets delta_percent
            # is malformed — e.g. an absolute_value level/rate must carry its
            # number in ``value`` ("5% payment failure rate" -> value=5,
            # delta=None).
            if self.representation != "percent_change":
                errors.append(
                    f"measurement {self.name}: delta_percent set on "
                    f"representation {self.representation!r}, which is not a "
                    f"percentage change (put the number in value instead)"
                )
        if self.method:
            errors += self.method.validate()
        return errors

    def has_numeric_evidence(self) -> bool:
        """Whether this measurement carries grounded quantitative evidence.

        ``quantity_text`` is the canonical evidence surface for natural-language
        quantities that should not be coerced to an exact float (for example,
        "thousands of engineering hours" or "multi-million-row table").
        """
        if self.quantity_text and self.quantity_text.strip():
            return True
        if self.value is not None:
            return True
        if self.delta_percent is not None:
            return True
        if self.value_qualifier == "range" and self.value_min is not None and self.value_max is not None:
            return True
        return False

    def to_dict(self) -> dict:
        def _num(v):
            return float(v) if v is not None else None
        d = {
            "name": self.name,
            "claim": self.claim,
            "layer": self.layer,
            "measurement_status": self.measurement_status,
            "representation": self.representation,
            "direction": self.direction,
            "value": _num(self.value),
            "unit": self.unit,
            "quantity_text": self.quantity_text,
            "delta_percent": _num(self.delta_percent),
            "value_qualifier": self.value_qualifier,
            "value_min": _num(self.value_min),
            "value_max": _num(self.value_max),
            "baseline": self.baseline,
            "result": self.result,
        }
        if self.source_ref:
            d["source_ref"] = self.source_ref
        if self.source_text:
            d["source_text"] = self.source_text
        if self.method:
            d["method"] = self.method.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Measurement":
        return cls(
            name=str(data.get("name", "")),
            claim=str(data.get("claim", "")),
            layer=data.get("layer", "technical"),
            measurement_status=data.get("measurement_status", "unmeasured"),
            representation=data.get("representation", "absolute_value"),
            direction=data.get("direction", "none"),
            value=_to_float(data.get("value")),
            unit=data.get("unit"),
            quantity_text=data.get("quantity_text"),
            delta_percent=_to_float(data.get("delta_percent")),
            value_qualifier=data.get("value_qualifier", "exact"),
            value_min=_to_float(data.get("value_min")),
            value_max=_to_float(data.get("value_max")),
            baseline=data.get("baseline"),
            result=data.get("result"),
            source_ref=data.get("source_ref"),
            source_text=data.get("source_text"),
            method=(
                MeasurementMethod.from_dict(data["method"])
                if data.get("method") else None
            ),
        )


@dataclass
class Outcome:
    """A projected consequence on a business layer, gated by claimability.

    Grounded outcomes (claimability in GROUNDED_CLAIMABILITY) carry the same
    provenance contract as measurements: ``source_ref`` + the verbatim
    ``source_text`` that exists in a known source and restates the claim, and
    the source block must be a subset of the accomplishment's skeleton blocks
    (parent binding). Hypothesis/unknown outcomes are never resume-eligible and
    may omit provenance.
    """
    layer: str
    claim: str
    claimability: str = "hypothesis"
    source_ref: str | None = None       # "resume.txt:L42" — provenance binding
    source_text: str | None = None      # the verbatim input the claim restates

    def validate(self) -> list[str]:
        errors: list[str] = []
        try:
            _ensure(self.layer, LAYERS, "outcome.layer")
            _ensure(self.claimability, CLAIMABILITY_VALUES, "outcome.claimability")
        except ValueError as exc:
            errors.append(str(exc))
        if not self.claim.strip():
            errors.append("outcome.claim is required")
        elif self.claim.strip().lower() in _INVALID_OUTCOME_CLAIMS:
            errors.append(
                f"outcome.claim {self.claim!r} is a claimability classification "
                f"or parser artifact, not a real outcome claim"
            )
        return errors

    @property
    def grounded(self) -> bool:
        return self.claimability in GROUNDED_CLAIMABILITY

    def to_dict(self) -> dict:
        d = {"layer": self.layer, "claim": self.claim, "claimability": self.claimability}
        if self.source_ref:
            d["source_ref"] = self.source_ref
        if self.source_text:
            d["source_text"] = self.source_text
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Outcome":
        return cls(
            layer=data.get("layer", "technical"),
            claim=str(data.get("claim", "")),
            claimability=data.get("claimability", "hypothesis"),
            source_ref=data.get("source_ref"),
            source_text=data.get("source_text"),
        )


@dataclass
class Intervention:
    """What was done, and how it is traceable to a source document."""
    summary: str = ""
    skills: list[str] = field(default_factory=list)
    provenance: str | None = None     # e.g. "resume.txt: Experience > HackOnTech"

    def to_dict(self) -> dict:
        d = {"summary": self.summary, "skills": list(self.skills)}
        if self.provenance:
            d["provenance"] = self.provenance
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Intervention":
        skills = data.get("skills") or []
        if isinstance(skills, str):
            skills = [s.strip() for s in re.split(r"[,;]", skills) if s.strip()]
        return cls(
            summary=str(data.get("summary", "")),
            skills=[str(s) for s in skills if str(s).strip()],
            provenance=data.get("provenance"),
        )


@dataclass
class Scope:
    """Where the record happened — enough to disambiguate, bounded to known fields."""
    type: str = "other"
    company: str | None = None
    role: str | None = None
    project: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.type not in SCOPE_TYPES:
            errors.append(f"Invalid scope.type: {self.type!r}")
        if not any([self.company, self.role, self.project]):
            errors.append("scope needs at least one of company/role/project")
        return errors

    def to_dict(self) -> dict:
        d = {"type": self.type}
        for key in ("company", "role", "project"):
            val = getattr(self, key)
            if val:
                d[key] = val
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Scope":
        return cls(
            type=data.get("type", "other"),
            company=_sanitize_scope_field(data.get("company")),
            role=_sanitize_scope_field(data.get("role")),
            project=_sanitize_scope_field(data.get("project")),
        )


# ── Interview defense sub-structures ──────────────────────────────────────

@dataclass
class MeasurementMethod:
    """How a metric was measured — methodology, not the value itself.

    ``method=None`` on a Measurement means the source states NO methodology;
    present-only fields are populated, and ``attribution_confidence`` may be
    None when the source does not state attribution confidence (the model is
    never forced to invent it). Only a real method object is created when at
    least one methodology field is actually populated.
    """
    source: str | None = None
    baseline_period: str | None = None
    result_period: str | None = None
    denominator: str | None = None
    comparison_method: str | None = None   # before_after | control | staged_rollout | a_b_test
    sample_size: str | None = None
    confounders: list[str] = field(default_factory=list)
    attribution_confidence: str | None = None  # high | medium | low | correlational | None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.attribution_confidence and self.attribution_confidence not in ATTRIBUTION_CONFIDENCE_VALUES:
            errors.append(f"Invalid measurement_method.attribution_confidence: {self.attribution_confidence!r}")
        return errors

    @property
    def has_content(self) -> bool:
        """Whether this method carries at least one populated methodology field."""
        return bool(
            self.source or self.baseline_period or self.result_period
            or self.denominator or self.comparison_method or self.sample_size
            or self.confounders or self.attribution_confidence
        )

    def to_dict(self) -> dict:
        d: dict = {}
        for key in ("source", "baseline_period", "result_period", "denominator",
                     "comparison_method", "sample_size"):
            val = getattr(self, key)
            if val is not None:
                d[key] = val
        if self.confounders:
            d["confounders"] = list(self.confounders)
        if self.attribution_confidence:
            d["attribution_confidence"] = self.attribution_confidence
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "MeasurementMethod":
        return cls(
            source=data.get("source"),
            baseline_period=data.get("baseline_period"),
            result_period=data.get("result_period"),
            denominator=data.get("denominator"),
            comparison_method=data.get("comparison_method"),
            sample_size=data.get("sample_size"),
            confounders=[str(c) for c in (data.get("confounders") or [])],
            attribution_confidence=data.get("attribution_confidence"),
        )


@dataclass
class DecisionConsequence:
    """What happened after the result was demonstrated."""
    type: str = "none"  # scaled | adopted | standardized | expanded | stopped | none
    description: str = ""
    source: str | None = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.type and self.type not in DECISION_CONSEQUENCE_TYPES:
            errors.append(f"Invalid decision_consequence.type: {self.type!r}")
        return errors

    def to_dict(self) -> dict:
        d: dict = {"type": self.type}
        if self.description:
            d["description"] = self.description
        if self.source:
            d["source"] = self.source
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "DecisionConsequence":
        return cls(
            type=data.get("type", "none"),
            description=str(data.get("description", "")),
            source=data.get("source"),
        )


@dataclass
class Tradeoff:
    """What the improvement cost or sacrificed."""
    dimension: str = ""   # cost | latency | complexity | reliability | engineering_time
    description: str = ""
    guardrail: str | None = None

    def to_dict(self) -> dict:
        d: dict = {}
        if self.dimension:
            d["dimension"] = self.dimension
        if self.description:
            d["description"] = self.description
        if self.guardrail:
            d["guardrail"] = self.guardrail
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "Tradeoff":
        return cls(
            dimension=str(data.get("dimension", "")),
            description=str(data.get("description", "")),
            guardrail=data.get("guardrail"),
        )


@dataclass
class InterviewEvidence:
    """Interview preparation data attached to an ImpactRecord.

    All fields are optional. Missing fields produce evidence-gap warnings
    during brief generation, not validation errors.
    """
    impact_headline: str = ""
    business_context: dict = field(default_factory=dict)
    ownership: dict = field(default_factory=dict)
    target: dict = field(default_factory=dict)
    decision_rationale: dict = field(default_factory=dict)
    tradeoffs: list[Tradeoff] = field(default_factory=list)
    guardrail_metrics: list[str] = field(default_factory=list)
    decision_consequence: DecisionConsequence | None = None
    limitations: list[str] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    # Transient fallback for legacy top-level measurement_method (migration):
    # never serialized/persisted; consumed only by defense.py as a last resort
    # when no measured KPI carries its own method.
    _legacy_measurement_method: "MeasurementMethod | None" = None

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.decision_consequence:
            errors += self.decision_consequence.validate()
        return errors

    def to_dict(self) -> dict:
        d: dict = {}
        if self.impact_headline:
            d["impact_headline"] = self.impact_headline
        if self.business_context:
            d["business_context"] = dict(self.business_context)
        if self.ownership:
            d["ownership"] = dict(self.ownership)
        if self.target:
            d["target"] = dict(self.target)
        if self.decision_rationale:
            d["decision_rationale"] = dict(self.decision_rationale)
        if self.tradeoffs:
            d["tradeoffs"] = [t.to_dict() for t in self.tradeoffs]
        if self.guardrail_metrics:
            d["guardrail_metrics"] = list(self.guardrail_metrics)
        if self.decision_consequence:
            d["decision_consequence"] = self.decision_consequence.to_dict()
        if self.limitations:
            d["limitations"] = list(self.limitations)
        if self.lessons:
            d["lessons"] = list(self.lessons)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "InterviewEvidence":
        return cls(
            impact_headline=str(data.get("impact_headline", "")),
            business_context=dict(data.get("business_context") or {}),
            ownership=dict(data.get("ownership") or {}),
            target=dict(data.get("target") or {}),
            decision_rationale=dict(data.get("decision_rationale") or {}),
            tradeoffs=[Tradeoff.from_dict(t) for t in (data.get("tradeoffs") or [])],
            guardrail_metrics=[str(g) for g in (data.get("guardrail_metrics") or [])],
            decision_consequence=(
                DecisionConsequence.from_dict(data["decision_consequence"])
                if data.get("decision_consequence") else None
            ),
            limitations=[str(item) for item in (data.get("limitations") or [])],
            lessons=[str(item) for item in (data.get("lessons") or [])],
        )


# ── Top-level record ──────────────────────────────────────────────────────

@dataclass
class ImpactRecord:
    """One significant accomplishment: problem -> intervention -> measurement
    chain -> (possibly unknown) business outcomes."""

    id: str = ""
    scope: Scope = field(default_factory=Scope)
    problem: str = ""
    intervention: Intervention = field(default_factory=Intervention)
    measurements: list[Measurement] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    context: dict = field(default_factory=dict)
    attribution: str = "primary_contributor"
    confidence: str = "medium"
    recency: str = ""                    # free text, e.g. "2024-2025"
    resume_eligible: bool = True
    review_status: str = "drafted"
    created_at: str = ""
    updated_at: str = ""
    provenance: dict | None = None       # fallback overall-source block
    interview_evidence: InterviewEvidence | None = None
    career_detail: dict | None = None  # private myCareer claim, confirmation and disclosure state

    def validate(self) -> list[str]:
        errors: list[str] = []
        errors += self.scope.validate()
        try:
            _ensure(self.attribution, ATTRIBUTION_VALUES, "attribution")
            _ensure(self.confidence, CONFIDENCE_VALUES, "confidence")
            _ensure(self.review_status, REVIEW_STATUSES, "review_status")
        except ValueError as exc:
            errors.append(str(exc))
        if not self.id.strip():
            errors.append("record id is required")
        if not self.problem.strip():
            errors.append("problem is required")
        for m in self.measurements:
            errors += m.validate()
        for o in self.outcomes:
            errors += o.validate()
        if self.interview_evidence:
            errors += self.interview_evidence.validate()
        if self.career_detail is not None and not isinstance(self.career_detail, dict):
            errors.append("career_detail must be an object")
        return errors

    @property
    def grounded_layer(self) -> str | None:
        """Highest business layer backed by GROUNDED measuring evidence.

        Only measurements ground a layer: an outcome's groundedness is derived
        from a measurement (or outcome) at or above it, never from itself. A
        technical-measured / financial-unknown record grounds at technical,
        never at financial.
        """
        best = None
        for m in self.measurements:
            if m.measurement_status in GROUNDED_MEASUREMENT_STATUSES:
                order = LAYERS.index(m.layer)
                if best is None or order > LAYERS.index(best):
                    best = m.layer
        return best

    @property
    def grounded(self) -> bool:
        return self.grounded_layer is not None

    def to_dict(self) -> dict:
        d: dict = {
            "id": self.id,
            "scope": self.scope.to_dict(),
            "problem": self.problem,
            "intervention": self.intervention.to_dict(),
            "measurements": [m.to_dict() for m in self.measurements],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "context": dict(self.context),
            "attribution": self.attribution,
            "confidence": self.confidence,
            "recency": self.recency,
            "resume_eligible": bool(self.resume_eligible),
            "review_status": self.review_status,
        }
        if self.created_at:
            d["created_at"] = self.created_at
        if self.updated_at:
            d["updated_at"] = self.updated_at
        if self.provenance:
            d["provenance"] = self.provenance
        if self.interview_evidence:
            d["interview_evidence"] = self.interview_evidence.to_dict()
        if self.career_detail is not None:
            import copy
            d["career_detail"] = copy.deepcopy(self.career_detail)
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "ImpactRecord":
        interview_raw = data.get("interview_evidence")
        return cls(
            id=str(data.get("id", "")),
            scope=Scope.from_dict(data.get("scope") or {}),
            problem=str(data.get("problem", "")),
            intervention=Intervention.from_dict(data.get("intervention") or {}),
            measurements=[Measurement.from_dict(m) for m in (data.get("measurements") or [])],
            outcomes=[Outcome.from_dict(o) for o in (data.get("outcomes") or [])],
            context=data.get("context") or {},
            attribution=data.get("attribution", "primary_contributor"),
            confidence=data.get("confidence", "medium"),
            recency=data.get("recency", ""),
            resume_eligible=bool(data.get("resume_eligible", True)),
            review_status=data.get("review_status", "drafted"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            provenance=data.get("provenance"),
            career_detail=data.get("career_detail"),
            interview_evidence=(
                InterviewEvidence.from_dict(interview_raw)
                if interview_raw else None
            ),
        )

    def claim_snapshot(self) -> dict:
        """Fixed public view; private recollection and attester identities never leave the ledger."""
        if self.career_detail is not None:
            from applypilot.career.projection import claim_snapshot
            return claim_snapshot(self)
        return {
            "id": self.id,
            "scope": self.scope.to_dict(),
            "problem": self.problem,
            "intervention": self.intervention.to_dict(),
            "measurements": [m.to_dict() for m in self.measurements],
            "outcomes": [o.to_dict() for o in self.outcomes],
            "context": dict(self.context),
            "attribution": self.attribution,
            "recency": self.recency,
            "resume_eligible": self.resume_eligible,
        }


# ── Matcher-facing models ─────────────────────────────────────────────────

@dataclass
class ImpactIntent:
    """Non-persisted, per-job extraction: what this employer wants to see change."""
    dimensions: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.dimensions and not self.keywords

    def to_dict(self) -> dict:
        return {"dimensions": list(self.dimensions), "keywords": list(self.keywords)}

    @classmethod
    def from_dict(cls, data: dict) -> "ImpactIntent":
        return cls(
            dimensions=[str(d) for d in (data.get("dimensions") or [])],
            keywords=[str(k) for k in (data.get("keywords") or [])],
        )


@dataclass
class SelectedImpact:
    """One chosen record inside a ClaimPack, with its score and immutable snapshot."""
    record: ImpactRecord
    score: float = 0.0
    score_components: dict = field(default_factory=dict)
    ledger_revision: str = ""
    rank: int = 0

    def snapshot(self) -> dict:
        return {
            "id": self.record.id,
            "ledger_revision": self.ledger_revision,
            "score": round(self.score, 3),
            "score_components": {k: round(float(v), 3) for k, v in self.score_components.items()},
            "claim_snapshot": self.record.claim_snapshot(),
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "SelectedImpact":
        record_data = dict(data.get("claim_snapshot") or {})
        record_data["id"] = str(data.get("id") or record_data.get("id") or "")
        record = ImpactRecord.from_dict(record_data)
        return cls(
            record=record,
            score=float(data.get("score", 0.0)),
            score_components=dict(data.get("score_components") or {}),
            ledger_revision=data.get("ledger_revision", ""),
        )


@dataclass
class ClaimPack:
    """Top-3 verified evidence selection for one job (runtime object)."""
    schema_version: int = 1
    selected_at: str = ""
    intent: ImpactIntent | None = None
    weights: dict = field(default_factory=dict)
    records: list[SelectedImpact] = field(default_factory=list)
    job_revision: str = ""
    selection_input_revision: str = ""

    @property
    def has_records(self) -> bool:
        return bool(self.records)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "selected_at": self.selected_at,
            "intent": self.intent.to_dict() if self.intent else None,
            "weights": dict(self.weights),
            "records": [r.snapshot() for r in self.records],
            "job_revision": self.job_revision,
            "selection_input_revision": self.selection_input_revision,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClaimPack":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            selected_at=data.get("selected_at", ""),
            intent=ImpactIntent.from_dict(data["intent"]) if data.get("intent") else None,
            weights=dict(data.get("weights") or {}),
            records=[SelectedImpact.from_snapshot(s) for s in (data.get("records") or [])],
            job_revision=data.get("job_revision", ""),
            selection_input_revision=data.get("selection_input_revision", ""),
        )


@dataclass
class SelectedEvidenceSnapshot:
    """Immutable per-job selection persisted to ``jobs.selected_evidence``.

    The ledger is mutable truth; this snapshot freezes the exact claims a
    resume/cover letter was built from so later edits/deletes never break
    reproducibility.
    """
    schema_version: int = 1
    selected_at: str = ""
    intent: ImpactIntent | None = None
    weights: dict = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)  # SelectedImpact.snapshot()
    job_revision: str = ""
    selection_input_revision: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "selected_at": self.selected_at,
            "intent": self.intent.to_dict() if self.intent else None,
            "weights": dict(self.weights),
            "records": [dict(r) for r in self.records],
            "job_revision": self.job_revision,
            "selection_input_revision": self.selection_input_revision,
        }

    @classmethod
    def from_pack(cls, pack: ClaimPack) -> "SelectedEvidenceSnapshot":
        return cls(
            schema_version=pack.schema_version,
            selected_at=pack.selected_at,
            intent=pack.intent,
            weights=dict(pack.weights),
            records=[r.snapshot() for r in pack.records],
            job_revision=pack.job_revision,
            selection_input_revision=pack.selection_input_revision,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "SelectedEvidenceSnapshot":
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            selected_at=data.get("selected_at", ""),
            intent=ImpactIntent.from_dict(data["intent"]) if data.get("intent") else None,
            weights=dict(data.get("weights") or {}),
            records=[dict(r) for r in (data.get("records") or [])],
            job_revision=data.get("job_revision", ""),
            selection_input_revision=data.get("selection_input_revision", ""),
        )


def _to_float(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_revision(record: ImpactRecord) -> str:
    """Stable hash of a record's canonical dict — detects edits/deletes."""
    import hashlib
    blob = json.dumps(record.to_dict(), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

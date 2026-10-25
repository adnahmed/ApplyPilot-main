"""Evidence-aware ordinal preferences, independent of providers and applicant country.

This ranks already-assessed alternatives. It does NOT infer eligibility, legal
status, pay policy, or employer location from search queries or provider labels.
Each field used in a decision must carry an upstream evidence/assessment ID.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import isfinite
from collections.abc import Mapping, Sequence
import re


@dataclass(frozen=True)
class PreferenceRule:
    id: str
    markets: tuple[str, ...]
    employer_scope: str | None = None
    engagements: tuple[str, ...] = ()
    pay_basis: str | None = None
    employer_regions: tuple[str, ...] = ()
    eligible_scope: str | None = None


@dataclass(frozen=True)
class ExplorationPolicy:
    # Zero is deliberately the generic default, not an invented risk appetite.
    discovery_share: float = 0.0
    application_share: float = 0.0


def _strings(value, path: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ValueError(f"{path} must be a list of nonempty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{path} contains duplicate values")
    return tuple(value)


def compile_preferences(raw, market_ids: Sequence[str]) -> tuple[PreferenceRule, ...]:
    if not isinstance(raw, (list, tuple)):
        raise ValueError("preferences must be an ordered list")
    result, seen = [], set()
    allowed = {"markets", "employer_scope", "engagements", "pay_basis", "employer_regions", "eligible_scope"}
    for i, item in enumerate(raw):
        path = f"preferences[{i}]"
        if not isinstance(item, Mapping) or set(item) - {"id", "match"}:
            raise ValueError(f"{path} accepts only id and match")
        rid = item.get("id")
        if not isinstance(rid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", rid) or rid in seen:
            raise ValueError(f"{path}.id must be a unique stable identifier")
        seen.add(rid)
        match = item.get("match")
        if not isinstance(match, Mapping) or set(match) - allowed:
            raise ValueError(f"{path}.match contains unknown predicates")
        markets = _strings(match.get("markets", []), path + ".match.markets")
        if not markets or set(markets) - set(market_ids):
            raise ValueError(f"{path}.match.markets must reference defined markets")
        scope = match.get("employer_scope")
        pay = match.get("pay_basis")
        eligible = match.get("eligible_scope")
        if scope not in (None, "foreign", "domestic"):
            raise ValueError(f"{path}.employer_scope must be foreign or domestic")
        if pay not in (None, "global", "localized"):
            raise ValueError(f"{path}.pay_basis must be global or localized")
        if eligible not in (None, "worldwide"):
            raise ValueError(f"{path}.eligible_scope must be worldwide when supplied")
        engagements = _strings(match.get("engagements", []), path + ".engagements")
        if set(engagements) - {"contractor", "eor", "direct_employee"}:
            raise ValueError(f"{path}.engagements has an unknown engagement")
        regions = _strings(match.get("employer_regions", []), path + ".employer_regions")
        result.append(PreferenceRule(rid, markets, scope, engagements, pay, regions, eligible))
    return tuple(result)


def compile_exploration(raw) -> ExplorationPolicy:
    if not isinstance(raw, Mapping) or set(raw) - {"discovery_share", "application_share"}:
        raise ValueError("exploration accepts only discovery_share and application_share")
    values = {}
    for key in ("discovery_share", "application_share"):
        value = raw.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"exploration.{key} must be a finite number between 0 and 1")
        values[key] = float(value)
    return ExplorationPolicy(**values)


@dataclass(frozen=True)
class ArrangementFacts:
    """One alternative, populated by matching/enrichment, never query provenance.

    ``markets`` lists confirmed matches. ``possible_markets`` lists unresolved
    matches. An eligible job may still have unknown preference-only fields.
    A list of evidence pairs maps each field name to an upstream evidence ID.
    """
    id: str
    role: str = "unknown"
    eligibility: str = "unknown"
    markets: tuple[str, ...] = ()
    possible_markets: tuple[str, ...] = ()
    employer_scope: str | None = None
    engagement: str | None = None
    pay_basis: str | None = None
    employer_regions: tuple[str, ...] = ()
    eligible_scope: str | None = None
    evidence: tuple[tuple[str, str], ...] = ()

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("arrangement id must be nonempty")
        if self.role not in {"relevant", "irrelevant", "unknown"}:
            raise ValueError("invalid role assessment")
        if self.eligibility not in {"eligible", "blocked", "unknown"}:
            raise ValueError("invalid eligibility assessment")
        if self.employer_scope not in {None, "foreign", "domestic"}:
            raise ValueError("invalid employer_scope")
        if self.engagement not in {None, "contractor", "eor", "direct_employee"}:
            raise ValueError("invalid engagement")
        if self.pay_basis not in {None, "global", "localized"}:
            raise ValueError("invalid pay_basis")
        if self.eligible_scope not in {None, "worldwide", "restricted"}:
            raise ValueError("invalid eligible_scope")
        # Frozen records must not retain caller-owned mutable lists.
        for key in ("markets", "possible_markets", "employer_regions"):
            object.__setattr__(self, key, _strings(getattr(self, key), key))
        pairs = []
        for pair in self.evidence:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2 or any(not isinstance(x, str) or not x for x in pair):
                raise ValueError("evidence entries must be [field, nonempty evidence ID]")
            pairs.append(tuple(pair))
        object.__setattr__(self, "evidence", tuple(pairs))

    def evidence_for(self, field: str) -> tuple[str, ...]:
        return tuple(eid for name, eid in self.evidence if name == field)


@dataclass(frozen=True)
class PreferenceDecision:
    status: str  # ranked | explore | off_portfolio | needs_evidence | blocked
    priority: int | None = None
    preference_id: str | None = None
    arrangement_id: str | None = None
    possible_preferences: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()


def _match(rule: PreferenceRule, facts: ArrangementFacts, active: set[str]):
    wanted_markets = set(rule.markets) & active
    if not wanted_markets:
        return "no", (), ()
    evidence, unknown = [], []
    if set(facts.markets) & wanted_markets and facts.evidence_for("markets"):
        evidence.extend(facts.evidence_for("markets"))
    elif set(facts.possible_markets) & wanted_markets or not facts.evidence_for("markets"):
        unknown.append("markets")
    else:
        return "no", (), ()
    checks = (("employer_scope", rule.employer_scope), ("engagement", rule.engagements),
              ("pay_basis", rule.pay_basis), ("employer_regions", rule.employer_regions),
              ("eligible_scope", rule.eligible_scope))
    for field, wanted in checks:
        if not wanted:
            continue
        actual, refs = getattr(facts, field), facts.evidence_for(field)
        if not actual or not refs:
            unknown.append(field)
            continue
        wanted_values = set(wanted) if isinstance(wanted, tuple) else {wanted}
        actual_values = set(actual) if isinstance(actual, tuple) else {actual}
        if not wanted_values.intersection(actual_values):
            return "no", (), ()
        evidence.extend(refs)
    return ("unknown" if unknown else "yes"), tuple(dict.fromkeys(evidence)), tuple(unknown)


def rank_opportunity(rules: Sequence[PreferenceRule], alternatives: Sequence[ArrangementFacts],
                     active_market_ids: Sequence[str]) -> PreferenceDecision:
    """Best confirmed ordinal rank, without mixing facts across alternatives.

    Unknown pay/engagement is NOT unknown eligibility. A confirmed lower rank
    remains usable while an earlier potential rank is awaiting more evidence.
    """
    active = set(active_market_ids)
    confirmed, possible, reasons = [], [], []
    has_eligible, has_unresolved = False, False
    for facts in alternatives:
        if ((facts.role == "irrelevant" and facts.evidence_for("role"))
                or (facts.eligibility == "blocked" and facts.evidence_for("eligibility"))):
            continue
        gate_refs = facts.evidence_for("role") + facts.evidence_for("eligibility")
        if facts.role != "relevant" or facts.eligibility != "eligible" or not facts.evidence_for("role") or not facts.evidence_for("eligibility"):
            has_unresolved = True
            continue
        has_eligible = True
        for index, rule in enumerate(rules, start=1):
            status, refs, missing = _match(rule, facts, active)
            if status == "yes":
                confirmed.append((index, rule.id, facts.id, tuple(dict.fromkeys(gate_refs + refs))))
            elif status == "unknown":
                possible.append(rule.id)
                reasons.extend(f"{facts.id}:{rule.id}:missing:{field}" for field in missing)
    possible_ids = tuple(dict.fromkeys(possible))
    if confirmed:
        priority, rule_id, arrangement_id, refs = min(confirmed, key=lambda row: (row[0], row[2]))
        return PreferenceDecision("ranked", priority, rule_id, arrangement_id, possible_ids, refs, tuple(reasons))
    if has_eligible:
        return PreferenceDecision("explore" if possible_ids else "off_portfolio", possible_preferences=possible_ids, reasons=tuple(reasons))
    return PreferenceDecision("needs_evidence" if has_unresolved or not alternatives else "blocked")


def exploration_split(total: int, share: float, credit: float = 0.0) -> tuple[int, int, float]:
    """Allocation primitive only; scheduler must persist returned fractional credit.

    Returns preferred slots, exploration slots, carry. Zero allocation is zero
    work. This function does not change provider limits or authorize submission.
    """
    if type(total) is not int or total < 0:
        raise ValueError("total must be a nonnegative integer")
    for name, value in (("share", share), ("credit", credit)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError(f"{name} must be finite")
    if not 0 <= share <= 1 or not 0 <= credit < 1:
        raise ValueError("share must be in [0,1]; credit in [0,1)")
    exact = total * Fraction(str(share)) + Fraction(str(credit))
    explore = min(total, exact.numerator // exact.denominator)
    return total - explore, explore, float(exact - explore)


def rotate_probes(probes: Sequence[str], cursor: int, limit: int) -> tuple[tuple[str, ...], int]:
    from applypilot.limits import work_limit
    work_limit(limit, "probe_limit")
    if type(cursor) is not int or cursor < 0:
        raise ValueError("cursor must be a nonnegative integer")
    if not probes or not limit:
        return (), cursor
    count = len(probes) if limit == -1 else min(limit, len(probes))
    return tuple(probes[(cursor + i) % len(probes)] for i in range(count)), (cursor + count) % len(probes)

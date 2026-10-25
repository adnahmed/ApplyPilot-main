"""Pure, revisioned role/arrangement assessment before ordinal preferences.

No profile reads, provider calls, database access or model inference. Evidence
kinds use WorkArrangement field names. Revisioned operator policy may assume
omitted requirements; unsupported prose and explicit unknowns remain unresolved. This is not an
exhaustive natural-language restriction parser or a legal authorization opinion.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
import hashlib
import json
import re
from urllib.parse import urlsplit

from .countries import COUNTRY_CODES
from .models import (
    ApplicantSnapshot, Authority, CandidateAssessment, Compatibility, Disposition,
    EvidenceFact, JobCandidate, MarketResult, Relevance, RequirementStatus,
    SearchIntent, TriState, WorkArrangement, WorkMode,
)
from .normalization import scalar_number, scalar_tristate
from .preferences import ArrangementFacts, rank_opportunity

RULES_VERSION = "discovery-matching-4"
_EU = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE".split())
_REGIONS = {"eu": _EU, "european union": _EU,
            "eea": _EU | {"IS", "LI", "NO"},
            "gcc": frozenset({"AE", "BH", "KW", "OM", "QA", "SA"})}


def _phrase(text: str, phrase: str) -> bool:
    # Token boundaries prevent incidental matches such as AI in payroll or VP
    # in an acronym. Hyphen/space variants remain the same title phrase.
    tokens = re.findall(r"[\w+#.]+", phrase.lower())
    return bool(tokens) and re.search(
        r"(?<!\w)" + r"[\s-]+".join(re.escape(token) for token in tokens) + r"(?!\w)",
        text.lower(),
    ) is not None


def _role(candidate: JobCandidate, intent: SearchIntent, origins: tuple[str, ...]):
    title = candidate.title
    if not isinstance(title, str) or not title.strip():
        return Relevance.UNKNOWN, ("missing title/role scope",)
    role_filter = intent.role_filter
    excludes = intent.exclude_titles + (role_filter.exclude_any if role_filter else ())
    if any(_phrase(title, term) for term in excludes):
        return Relevance.IRRELEVANT, ("explicit global title exclusion",)
    active = intent.active_queries()
    if not active:
        return Relevance.IRRELEVANT, ("no selected query tiers",)
    # Query origins identify retrieval, not which other active roles may match.
    positive = bool(role_filter and any(_phrase(title, term) for term in role_filter.include_any))
    if role_filter and role_filter.include_any and not positive:
        # Keep this gate deterministic/cheap: multi-token configured role terms
        # may match despite punctuation/order variants, but unrelated titles are
        # persisted as irrelevant instead of consuming model-scoring capacity.
        positive = any(
            len(tokens := re.findall(r"[\w+#.]+", term.lower())) >= 2
            and all(_phrase(title, token) for token in tokens)
            for term in role_filter.include_any
        )
        if not positive:
            return Relevance.IRRELEVANT, ("title outside configured positive role filter",)
    accepted, excluded = [], []
    for query in active:
        # A query origin is retrieval provenance, not proof of relevance. Match
        # the title independently. Drop only generic seniority modifiers.
        terms = [term for term in re.findall(r"[\w+#.]+", query.query.lower())
                 if term not in {"senior", "junior", "mid", "level"}]
        matches = bool(terms) and all(_phrase(title, term) for term in terms)
        if not matches:
            continue
        if any(_phrase(title, term) for term in query.exclude):
            excluded.append(query.effective_id())
        else:
            accepted.append(query.effective_id())
    if accepted:
        return Relevance.RELEVANT, tuple("accepted query:" + key for key in accepted)
    if excluded:
        return Relevance.IRRELEVANT, tuple("excluded query:" + key for key in excluded)
    if positive:
        # Feed admission is deliberately broader than exact search phrases.
        # Do not fabricate an accepted query ID or tier when only this gate fits.
        return Relevance.RELEVANT, ("positive role gate; no exact query title match",)
    return Relevance.UNKNOWN, ("no selected query independently matches the title",)


def _same(value, expected) -> bool:
    if isinstance(expected, TriState):
        return scalar_tristate(value) == expected
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return False


def assess_candidate(candidate: JobCandidate, intent: SearchIntent,
                     applicant: ApplicantSnapshot,
                     origins: tuple[str, ...] = (), *, admission_policy=None,
                     role_evaluation: str | None = None) -> CandidateAssessment:
    """Assess each stable alternative, then call rank_opportunity exactly once.

    Country authorization is never inferred from citizenship/residence. Remote
    contracting can use explicit home-country/worldwide scope without assuming
    foreign employee work rights. Office/direct-employment options need scoped
    authorization or applicable visa sponsorship. An explicit pre-existing
    authorization requirement cannot be waived by a general sponsorship benefit.

    ``prepare_allowed``/``apply_allowed`` are discovery eligibility gates only:
    ranked alternatives qualify; eligible exploration also requires a nonzero
    application exploration policy. Identity/artifact/lease/lifecycle checks and
    exploration-budget reservation remain the downstream admission layer's job.
    """
    if intent.target_policies_json != "[]":
        from applypilot.targets.intent import assess
        return assess(candidate, intent, applicant, origins, admission_policy, role_evaluation)
    if not isinstance(candidate, JobCandidate) or not isinstance(intent, SearchIntent) or not isinstance(applicant, ApplicantSnapshot):
        raise TypeError("assess_candidate requires JobCandidate, SearchIntent and ApplicantSnapshot")
    if not isinstance(origins, tuple) or any(not isinstance(x, str) for x in origins):
        raise TypeError("origins must be a tuple of strings")
    content = candidate.content_hash()
    from .admission_policy import omitted_requirement, policy_revision
    policy = admission_policy or {}
    policy_version = policy_revision(policy)
    assumptions = []
    profile_revision, intent_revision = applicant.fingerprint(), intent.fingerprint()
    revision = hashlib.sha256(json.dumps(
        [RULES_VERSION, content, profile_revision, intent_revision, policy_version, role_evaluation]
    ).encode()).hexdigest()[:20]
    role, role_reasons = _role(candidate, intent, origins)
    if role_evaluation not in (None, "relevant", "irrelevant", "unknown"):
        raise ValueError("invalid role evaluation")
    if role != Relevance.IRRELEVANT and role_evaluation in {"relevant", "irrelevant"}:
        same_role = role.value == role_evaluation
        role = Relevance(role_evaluation)
        role_reasons = (role_reasons if same_role else ()) + (
            "description/profile scoring evaluation:" + role_evaluation,)
    role_ref = "assessment:" + revision + ":role"
    ids = Counter(f.id for f in candidate.facts if isinstance(f, EvidenceFact) and f.id)
    option_ids = Counter(a.id for a in candidate.work_arrangements if isinstance(a, WorkArrangement) and a.id)
    facts_by_id = {f.id: f for f in candidate.facts if isinstance(f, EvidenceFact) and f.id}
    results, unresolved, requirements = [], [], {}
    market_checks = {str(m.label.value): [] for m in intent.active_markets()}

    for index, option in enumerate(candidate.work_arrangements):
        if not isinstance(option, WorkArrangement):
            unresolved.append("malformed work arrangement")
            results.append(ArrangementFacts("invalid-" + str(index), role=role.value,
                                           evidence=(("role", role_ref),)))
            continue
        aid = option.id if isinstance(option.id, str) and option.id.strip() else "unidentified-" + str(index)
        prefix = "assessment:" + revision + ":" + aid
        missing, conflicts, used = [], [], []
        if not option.id or option_ids[option.id] != 1:
            missing.append("missing or duplicate stable arrangement id")
            # Duplicate option IDs must also be distinct in the ranker projection.
            aid = "invalid-" + str(index) + ":" + aid
        scoped = [f for f in candidate.facts if isinstance(f, EvidenceFact) and f.arrangement_id == option.id]
        for fact in candidate.facts:
            if isinstance(fact, EvidenceFact) and fact.kind == "normalization_issue" and fact.arrangement_id in (None, option.id):
                missing.append(str(fact.value))
        if not isinstance(option.evidence_ids, tuple):
            missing.append("malformed evidence_ids")
        for eid in option.evidence_ids:
            fact = facts_by_id.get(eid)
            if fact is None or ids[eid] != 1 or fact.arrangement_id != option.id:
                missing.append("missing, duplicate or cross-option evidence: " + str(eid))

        def evidence(key, value, job_level=False):
            pool = candidate.facts if job_level else scoped
            applicable = []
            ambiguous = False
            for item in pool:
                if not isinstance(item, EvidenceFact) or item.kind != key:
                    continue
                if job_level and item.arrangement_id is not None:
                    continue
                if not job_level and item.id not in option.evidence_ids:
                    continue
                if not item.id or ids[item.id] != 1:
                    missing.append("duplicate or missing evidence id for " + key)
                    ambiguous = True
                    continue
                if item.authority not in (Authority.EMPLOYER, Authority.AGGREGATOR):
                    continue
                if not (item.provider_field_path or item.excerpt):
                    continue
                # A pure snapshot has no assessment date. A dated policy must be
                # resolved by upstream enrichment, never compared with wall time.
                if item.valid_from or item.valid_until:
                    missing.append("unresolved evidence validity window: " + item.id)
                    ambiguous = True
                    continue
                if item.countries:
                    target = applicant.country if option.work_mode == WorkMode.REMOTE else (
                        option.office_countries[0] if len(option.office_countries) == 1 else None)
                    if target not in item.countries:
                        continue
                if item.value in (None, "unknown") and value not in (None, "unknown", TriState.UNKNOWN):
                    # Enriched positive/negative evidence can resolve a previous
                    # explicit unknown, but never replace a known restriction.
                    continue
                if not _same(item.value, value):
                    missing.append("conflicting evidence for " + key)
                    ambiguous = True
                    continue
                applicable.append(item.id)
            if ambiguous:
                return ()
            used.extend(applicable)
            return tuple(applicable)

        def known(key, job_level=False):
            value = getattr(candidate if job_level else option, key)
            refs = evidence(key, value, job_level)
            return value if refs else None

        def check(name, state, detail=None):
            previous = requirements.get(aid + ":" + name)
            if previous == RequirementStatus.CONFLICT or previous == RequirementStatus.UNKNOWN and state == RequirementStatus.MET:
                return
            requirements[aid + ":" + name] = state
            if state == RequirementStatus.UNKNOWN:
                applicable = [f for f in candidate.facts if isinstance(f, EvidenceFact)
                              and f.arrangement_id in (None, option.id)]
                if (policy.get("optimistic_missing_requirements") and candidate.description
                        and omitted_requirement(option, applicable, name)):
                    assumptions.append({"arrangement_id": aid, "requirement": name,
                                        "reason": "omitted_requirement", "policy_version": policy_version})
                    return
                missing.append(detail or name)
            elif state == RequirementStatus.CONFLICT:
                conflicts.append(detail or name)

        # Validate supported fields even if they are not used by a preference.
        for key in ("countries", "office_countries", "excluded_countries", "residency_countries",
                    "authorization_countries", "citizenship_countries"):
            value = getattr(option, key)
            if not isinstance(value, tuple) or any(not isinstance(x, str) or x not in COUNTRY_CODES for x in value):
                missing.append("malformed " + key)
        for key in ("residency_required", "authorization_required", "citizenship_required",
                    "clearance_required", "relocation_required", "relocation_supported", "sponsorship_supported"):
            if not isinstance(getattr(option, key), TriState):
                missing.append("malformed " + key)
        mode = known("work_mode")
        countries = known("countries")
        offices = known("office_countries") if mode in (WorkMode.ONSITE, WorkMode.HYBRID) else None
        scope = known("eligible_scope")
        engagement = known("engagement")
        if scope not in (None, "worldwide", "restricted"):
            missing.append("malformed eligible_scope")
            scope = None
        if scope == "worldwide" and (countries or option.regions or option.cities):
            # Worldwide and an exhaustive allowed-country list cannot both
            # describe the same option without resolving their applicability.
            missing.append("worldwide scope conflicts with a restricted country list")
            scope = None
        if engagement not in (None, "contractor", "eor", "direct_employee"):
            missing.append("malformed engagement")
            engagement = None
        target = None
        allowed_regions = known("regions")

        def region_membership(regions, country):
            answers = [True if region.casefold() in {"global", "worldwide"} else
                       country in _REGIONS[region.casefold()] if region.casefold() in _REGIONS and country else None
                       for region in regions]
            return True if True in answers else None if None in answers or not answers else False

        if mode == WorkMode.REMOTE:
            target = applicant.country
            if not target:
                check("location", RequirementStatus.UNKNOWN, "applicant residence unknown")
            elif countries and target not in countries:
                check("location", RequirementStatus.CONFLICT, "home country outside explicit allowed countries")
            elif scope == "worldwide" or countries and target in countries or allowed_regions and region_membership(allowed_regions, target) is True:
                check("location", RequirementStatus.MET)
            else:
                check("location", RequirementStatus.UNKNOWN, "remote does not establish home-country eligibility")
        elif mode in (WorkMode.ONSITE, WorkMode.HYBRID):
            locations = known("office_locations")
            if offices and len(offices) == 1 and locations and len(locations) == 1:
                target = offices[0]
                check("location", RequirementStatus.MET)
                if countries and (not applicant.country or applicant.country not in countries):
                    check("location_scope", RequirementStatus.CONFLICT if applicant.country else RequirementStatus.UNKNOWN,
                          "applicant residence outside allowed applicant scope")
            else:
                check("location", RequirementStatus.UNKNOWN, "office alternative needs one explicit location and country")
        else:
            check("location", RequirementStatus.UNKNOWN, "work mode unknown")
        if mode not in (WorkMode.REMOTE, WorkMode.ONSITE, WorkMode.HYBRID) and countries:
            check("location_scope", RequirementStatus.UNKNOWN if not applicant.country else
                  RequirementStatus.MET if applicant.country in countries else RequirementStatus.CONFLICT)

        for key in ("countries", "eligible_scope", "engagement", "work_mode"):
            value = getattr(option, key)
            if value and value != WorkMode.UNKNOWN and not evidence(key, value):
                missing.append("unsupported " + key)

        excluded = known("excluded_countries")
        # These are applicant exclusions, not the prospective office country's
        # eligibility. Relocation does not silently change current residence.
        exclusion_target = applicant.country
        if option.excluded_countries and excluded is None:
            check("exclusions", RequirementStatus.UNKNOWN, "unsupported country exclusions")
        elif excluded:
            check("exclusions", RequirementStatus.UNKNOWN if not exclusion_target else
                  RequirementStatus.CONFLICT if exclusion_target in excluded else RequirementStatus.MET)
        if option.regions:
            answer = region_membership(allowed_regions, applicant.country) if allowed_regions else None
            check("regions", RequirementStatus.UNKNOWN if answer is None else
                  RequirementStatus.MET if answer else RequirementStatus.CONFLICT)
        if option.excluded_regions:
            regions = known("excluded_regions")
            answer = region_membership(regions, applicant.country) if regions else None
            check("excluded_regions", RequirementStatus.UNKNOWN if answer is None else
                  RequirementStatus.CONFLICT if answer else RequirementStatus.MET)
        if option.excluded_locations:
            locations = known("excluded_locations")
            actual = {x.casefold() for x in (applicant.city, applicant.province_state) if x}
            check("excluded_locations", RequirementStatus.CONFLICT if locations and any(
                  x.casefold() in actual for x in locations) else RequirementStatus.UNKNOWN,
                  "subnational exclusion needs unambiguous applicant/location scope")
        for key in ("timezone_requirements", "residency_requirements", "authorization_requirements"):
            if getattr(option, key):
                # No timezone availability exists in ApplicantSnapshot. Do not
                # substitute the UTC offset of the applicant's residence.
                check(key, RequirementStatus.UNKNOWN, "unresolved " + key)
        if option.cities:
            allowed_cities = known("cities")
            if mode == WorkMode.REMOTE and applicant.city and allowed_cities:
                check("cities", RequirementStatus.MET if applicant.city.casefold() in
                      {x.casefold() for x in allowed_cities} else RequirementStatus.CONFLICT)
            else:
                check("cities", RequirementStatus.UNKNOWN)

        if option.metric_requirements:
            constraints = known("metric_requirements")
            if not constraints:
                check("metrics", RequirementStatus.UNKNOWN, "unsupported applicant metric requirements")
            for index, constraint in enumerate(constraints or ()):
                state = RequirementStatus.UNKNOWN
                if isinstance(constraint, dict) and not set(constraint) - {"name", "unit", "minimum", "maximum"}:
                    name, unit = constraint.get("name"), constraint.get("unit")
                    minimum, maximum = scalar_number(constraint.get("minimum")), scalar_number(constraint.get("maximum"))
                    answers = [scalar_number(metric.get("value")) for metric in applicant.metrics
                               if metric.get("name") == name and metric.get("unit") == unit]
                    if (isinstance(name, str) and name and isinstance(unit, str) and unit
                            and (minimum is not None or maximum is not None)
                            and all(constraint.get(k) is None or scalar_number(constraint[k]) is not None for k in ("minimum", "maximum"))
                            and not (minimum is not None and maximum is not None and minimum > maximum)
                            and answers and None not in answers and len(set(answers)) == 1):
                        met = (minimum is None or answers[0] >= minimum) and (maximum is None or answers[0] <= maximum)
                        state = RequirementStatus.MET if met else RequirementStatus.CONFLICT
                check("metric:" + str(index), state)

        for name, country_field, applicant_values in (
            ("residency", "residency_countries", (applicant.country,) if applicant.country else ()),
            ("citizenship", "citizenship_countries", applicant.citizenship),
            ("clearance", "clearance_requirements", applicant.clearance),
        ):
            required = known(name + "_required")
            details = getattr(option, country_field)
            provided = any(f.kind == name + "_required" for f in scoped) or getattr(option, name + "_required") != TriState.UNKNOWN
            if required == TriState.NO:
                if details:
                    check(name, RequirementStatus.UNKNOWN, name + " requirement contradicts its details")
                else:
                    check(name, RequirementStatus.MET)
            elif required == TriState.YES or details:
                values = known(country_field)
                if required not in (None, TriState.YES) or not values or not applicant_values:
                    check(name, RequirementStatus.UNKNOWN)
                else:
                    expected = {x.casefold() for x in values}
                    actual = {x.casefold() for x in applicant_values}
                    met = expected <= actual if name == "clearance" else bool(expected & actual)
                    # Empty applicant collections are unknown, not an exhaustive
                    # declaration of no citizenships or clearances.
                    check(name, RequirementStatus.MET if met else RequirementStatus.UNKNOWN)
                    if name == "residency" and not met:
                        check(name, RequirementStatus.CONFLICT)
            elif provided:
                check(name, RequirementStatus.UNKNOWN)
            # No assertion of 'complete' or invented negative requirement is
            # needed: unstated constraints are not known applicable constraints.

        required_auth = known("authorization_required")
        sponsor = known("sponsorship_supported")
        auth_countries = known("authorization_countries")
        if option.authorization_countries and not auth_countries:
            check("authorization", RequirementStatus.UNKNOWN, "unsupported authorization country scope")
        elif required_auth == TriState.YES or auth_countries and required_auth in (None, TriState.UNKNOWN):
            if not auth_countries:
                check("authorization", RequirementStatus.UNKNOWN, "required authorization country unknown")
            else:
                answers = [applicant.authorization_for(code) for code in auth_countries]
                check("authorization", RequirementStatus.MET if all(x == TriState.YES for x in answers)
                      else RequirementStatus.CONFLICT if TriState.NO in answers else RequirementStatus.UNKNOWN)
        elif required_auth == TriState.NO and option.authorization_countries:
            check("authorization", RequirementStatus.UNKNOWN, "authorization requirement contradicts country scope")
        elif (any(f.kind == "authorization_required" for f in scoped) or option.authorization_required != TriState.UNKNOWN) and required_auth not in (TriState.YES, TriState.NO):
            check("authorization", RequirementStatus.UNKNOWN)
        elif target and mode == WorkMode.REMOTE and engagement == "contractor":
            check("authorization", RequirementStatus.MET)
        elif target:
            authorized = applicant.authorization_for(target)
            if authorized == TriState.YES or sponsor == TriState.YES:
                check("authorization", RequirementStatus.MET)
            elif authorized == TriState.NO and sponsor == TriState.NO:
                check("authorization", RequirementStatus.CONFLICT)
            else:
                check("authorization", RequirementStatus.UNKNOWN, "country-scoped authorization or sponsor support required")
        else:
            check("authorization", RequirementStatus.UNKNOWN)
        employee_route = mode in (WorkMode.ONSITE, WorkMode.HYBRID) or engagement != "contractor"
        if target and employee_route and applicant.sponsorship_for(target) == TriState.YES:
            if sponsor == TriState.NO:
                check("sponsorship", RequirementStatus.CONFLICT)
            elif sponsor != TriState.YES:
                check("sponsorship", RequirementStatus.UNKNOWN)
        if (mode == WorkMode.REMOTE and engagement is None
                and (not target or applicant.authorization_for(target) != TriState.YES)):
            check("engagement", RequirementStatus.UNKNOWN, "remote engagement route unknown without scoped authorization")
        if engagement == "eor" and not (countries and target in countries and any(
                f.id in option.evidence_ids and f.kind == "engagement" and f.value == "eor" and
                (not f.countries or target in f.countries) for f in scoped)):
            check("eor", RequirementStatus.UNKNOWN, "EOR needs explicit role/country coverage")
        relocation = known("relocation_required")
        if relocation == TriState.YES and mode == WorkMode.REMOTE:
            check("relocation", RequirementStatus.UNKNOWN, "relocation conflicts with a home-remote assessment")
        elif relocation == TriState.YES:
            check("relocation", RequirementStatus.UNKNOWN, "applicant relocation availability is not established")
        elif any(f.kind == "relocation_required" for f in scoped) and relocation != TriState.NO:
            check("relocation", RequirementStatus.UNKNOWN)
        # Conflicting enrichment never erases a supported earlier constraint.
        # Evaluate restrictive claims independently; they are not permission to
        # combine alternative benefits, and an unknown scope stays unresolved.
        for item in scoped:
            if (item.kind != "conflicting_statement" or item.id not in option.evidence_ids
                    or ids[item.id] != 1 or item.authority not in (Authority.EMPLOYER, Authority.AGGREGATOR)
                    or not isinstance(item.value, dict) or not (item.provider_field_path or item.excerpt)):
                continue
            if item.valid_from or item.valid_until or item.countries and target not in item.countries:
                check("statement:" + item.id, RequirementStatus.UNKNOWN, "conflicting statement applicability unresolved")
                continue
            used.append(item.id)
            constraint = item.value
            for key in ("countries", "residency_countries", "excluded_countries"):
                values = constraint.get(key)
                if isinstance(values, list) and values and all(code in COUNTRY_CODES for code in values) and applicant.country:
                    blocked = applicant.country in values if key == "excluded_countries" else applicant.country not in values
                    if blocked:
                        check("statement:" + item.id + ":" + key, RequirementStatus.CONFLICT, "retained conflicting " + key)
            if scalar_tristate(constraint.get("authorization_required")) == TriState.YES:
                values = constraint.get("authorization_countries")
                if isinstance(values, list) and values and all(code in COUNTRY_CODES for code in values):
                    answers = [applicant.authorization_for(code) for code in values]
                    check("statement:" + item.id + ":authorization", RequirementStatus.CONFLICT if TriState.NO in answers
                          else RequirementStatus.MET if all(answer == TriState.YES for answer in answers) else RequirementStatus.UNKNOWN)
                else:
                    check("statement:" + item.id + ":authorization", RequirementStatus.UNKNOWN)
        for key, value in candidate.requirements.items():
            # Old job-wide booleans have no country/alternative/evidence scope.
            if value != TriState.NO:
                check("job_requirement:" + key, RequirementStatus.UNKNOWN, "unscoped job requirement: " + key)

        eligibility = "blocked" if conflicts else "unknown" if missing else "eligible"
        confirmed, possible = [], []
        market_evidence = []
        for market in intent.active_markets():
            label = str(market.label.value)
            states = []
            if market.work_modes:
                states.append(None if mode in (None, WorkMode.UNKNOWN) else mode in market.work_modes)
            if market.countries:
                states.append(None if target is None else target in market.countries)
            if market.regions:
                states.append(scope == "worldwide" if set(market.regions) <= {"global", "worldwide"}
                              and scope is not None else None)
            if market.cities:
                locations = known("office_locations") if mode != WorkMode.REMOTE else (
                    (applicant.city,) if applicant.city else None)
                states.append(None if not locations else any(_phrase(location, city)
                              for location in locations for city in market.cities))
            if market.employment_types:
                schedule = known("schedule")
                labels = {schedule} if schedule else set()
                if engagement == "contractor":
                    labels.add("contract")
                wanted = {item.value for item in market.employment_types}
                states.append(True if labels & wanted else None if not labels else False)
            if market.requires_relocation is not None:
                relocation = None if target is None or applicant.country is None else target != applicant.country
                if relocation is False and market.cities:
                    relocation = None if applicant.city is None else not any(
                        _phrase(applicant.city, city) for city in market.cities)
                states.append(None if relocation is None else relocation == market.requires_relocation)
            if market.requires_clearance is not None:
                clearance = known("clearance_required")
                states.append(None if clearance in (None, TriState.UNKNOWN) else
                              (clearance == TriState.YES) == market.requires_clearance)
            status = Compatibility.INCOMPATIBLE if False in states or eligibility == "blocked" else (
                Compatibility.UNKNOWN if None in states or eligibility == "unknown" else Compatibility.COMPATIBLE)
            if status == Compatibility.COMPATIBLE:
                confirmed.append(label)
            elif status == Compatibility.UNKNOWN:
                possible.append(label)
            ref = prefix + ":market:" + label
            market_evidence.append(("markets", ref))
            assessment_fact = EvidenceFact(
                value={"compatibility": status.value, "evidence_ids": sorted(set(used))},
                kind="market_assessment", authority=Authority.RULE, id=ref,
                arrangement_id=option.id, rule_version=RULES_VERSION,
            )
            market_checks[label].append((status, assessment_fact))

        # Optional preference evidence can add uncertainty/conflicts of its own;
        # it must not poison a separately established eligibility result.
        hq = known("hiring_organization_hq_country", job_level=True)
        employer_scope = ("domestic" if hq == applicant.country else "foreign") if hq and applicant.country else None
        regions = ("uk",) if hq == "GB" else ("eu",) if hq in _EU else ()
        pay = known("pay_basis")
        if pay not in (None, "global", "localized"):
            pay = None
        refs = [("role", role_ref), ("eligibility", prefix + ":eligibility"), *market_evidence]
        for key, value in (("engagement", engagement), ("pay_basis", pay), ("eligible_scope", scope)):
            if value:
                refs.extend((key, eid) for eid in evidence(key, value))
        if employer_scope:
            refs.extend(("employer_scope", eid) for eid in evidence("hiring_organization_hq_country", hq, True))
        if regions:
            refs.extend(("employer_regions", eid) for eid in evidence("hiring_organization_hq_country", hq, True))
        refs.extend(("eligibility", eid) for eid in sorted(set(used)))
        unresolved.extend(aid + ": " + item for item in dict.fromkeys(missing))
        unresolved.extend(aid + ": conflict: " + item for item in dict.fromkeys(conflicts))
        results.append(ArrangementFacts(
            id=aid, role=role.value, eligibility=eligibility,
            markets=tuple(confirmed), possible_markets=tuple(possible),
            employer_scope=employer_scope, engagement=engagement, pay_basis=pay,
            employer_regions=regions, eligible_scope=scope, evidence=tuple(dict.fromkeys(refs)),
        ))

    preference = rank_opportunity(intent.preferences, results, intent.active_market_labels)
    eligible = sorted((a for a in results if a.role == "relevant" and a.eligibility == "eligible"), key=lambda a: a.id)
    if preference.arrangement_id is None and eligible:
        selected = eligible[0]
        preference = replace(preference, arrangement_id=selected.id,
                             evidence_ids=tuple(dict.fromkeys(eid for _, eid in selected.evidence)))
    selected = next((a for a in eligible if a.id == preference.arrangement_id), None)
    market_results = []
    for market in intent.active_markets():
        checks = market_checks[str(market.label.value)]
        statuses = [status for status, _ in checks]
        compatibility = Compatibility.COMPATIBLE if Compatibility.COMPATIBLE in statuses else (
            Compatibility.UNKNOWN if not statuses or Compatibility.UNKNOWN in statuses else Compatibility.INCOMPATIBLE)
        market_results.append(MarketResult(market.label, compatibility,
                              evidence=tuple(fact for _, fact in checks),
                              unresolved=("one or more alternatives need evidence",) if compatibility == Compatibility.UNKNOWN else ()))
    disposition = {"ranked": Disposition.CANDIDATE, "explore": Disposition.CANDIDATE,
                   "off_portfolio": Disposition.OFF_PORTFOLIO, "blocked": Disposition.BLOCKED,
                   "needs_evidence": Disposition.NEEDS_EVIDENCE}[preference.status]
    allowed = selected is not None and (
        preference.status == "ranked" or
        preference.status in {"explore", "off_portfolio"} and
        (intent.exploration.application_share > 0 or policy.get("fill_idle_capacity")))
    # Pure discovery does not resolve or invent an application URL from a generic
    # board listing. Final identity and lifecycle fencing remain downstream.
    target_url = candidate.canonical_target or candidate.application_url
    try:
        parsed = urlsplit(target_url or "")
        valid_target = parsed.scheme in {"https", "http"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    except ValueError:
        valid_target = False
    if not valid_target:
        unresolved.append("application target is unresolved")
    return CandidateAssessment(
        role=role, role_reasons=role_reasons, market_results=tuple(market_results),
        requirements=requirements, disposition=disposition,
        unresolved=tuple(dict.fromkeys(unresolved)), intent_fingerprint=intent_revision,
        profile_version=profile_revision, rules_version=RULES_VERSION, content_hash=content,
        preference=preference, arrangement_results=tuple(results),
        prepare_allowed=allowed, apply_allowed=allowed and valid_target,
        assumptions=tuple(assumptions), admission_policy_version=policy_version,
    )

"""Pure provider normalization, not a generic provider-to-eligibility mapper.

Adapters must separately establish field meaning and scope with evidence. In
particular, remote is not worldwide, office is not applicant geography, USD is
not global pay, full_time is not direct employment, and relocation support is
not a requirement. This module performs no assessment or live adapter wiring.

Invalid or unsupported scalar values return None/UNKNOWN. Containers are not
stringified or reduced by truthiness. NumPy/pandas are imported lazily only for
their own values; ordinary provider JSON needs only the standard library.
"""

from __future__ import annotations

from decimal import Decimal
from math import isfinite
from numbers import Real
from collections.abc import Mapping
from dataclasses import fields
from datetime import date, datetime
from enum import Enum
from html import unescape
import hashlib
import json
import re

from .countries import COUNTRY_CODES, COUNTRY_NAMES
from .models import (
    Authority, CompensationRange, EvidenceFact, JobCandidate, SourceIdentity,
    TriState, WorkArrangement, WorkMode,
)

NORMALIZER_VERSION = "discovery-normalization-3"


def _scalar(value: object) -> object:
    """Unbox NumPy scalars only, never arrays/Series (including length one)."""
    if type(value).__module__.split(".")[0] == "numpy":
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
    return value


def is_missing(value: object) -> bool:
    """Recognize null/NaN/NA and blank or explicit textual null markers.

    Literal 'NA' is deliberately retained (it can be a geographic code). Empty
    containers are not missing scalars. Infinity is invalid numeric data, not
    a null sentinel; scalar_number rejects it separately.
    """
    value = _scalar(value)
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"", "null", "none", "nan", "<na>", "nat", "n/a"}
    if type(value).__module__.split(".")[0] == "pandas":
        import pandas as pd

        return pd.isna(value) is True
    if isinstance(value, Decimal):
        return value.is_nan()
    if isinstance(value, Real):
        return value != value
    return False


def scalar_text(value: object) -> str | None:
    """Trim strings or retain finite numeric IDs as text; reject booleans/objects."""
    value = _scalar(value)
    if is_missing(value) or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (Real, Decimal)):
        try:
            if isfinite(value):
                return str(value)
        except (OverflowError, ValueError):
            pass
    return None


def scalar_number(value: object) -> float | None:
    """Finite numeric scalars/plain numeric strings only; preserve zero.

    No currency stripping, thousands-separator guessing, FX or annualization.
    Booleans never become salary bounds of zero or one.
    """
    value = _scalar(value)
    if is_missing(value) or isinstance(value, bool) or not isinstance(value, (str, Real, Decimal)):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return number if isfinite(number) else None


def scalar_bool(value: object) -> bool | None:
    """Parse explicit true/false, yes/no, t/f, y/n, or exact numeric 1/0.

    Unknown strings, missing values and other numbers stay unknown. Never use
    bool(value) on scraped evidence: even the string 'false' is truthy.
    """
    value = _scalar(value)
    if is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().casefold()
        if token in {"true", "yes", "t", "y", "1"}:
            return True
        if token in {"false", "no", "f", "n", "0"}:
            return False
        return None
    if isinstance(value, (Real, Decimal)):
        if value == 1:
            return True
        if value == 0:
            return False
    return None


def scalar_tristate(value: object) -> TriState:
    """Represent explicit scalar booleans in the existing evidence vocabulary."""
    answer = scalar_bool(value)
    if answer is True:
        return TriState.YES
    if answer is False:
        return TriState.NO
    return TriState.UNKNOWN


def country_code(value: object) -> str | None:
    """Exact country names/codes only. Never infer a country from a city."""
    text = scalar_text(value)
    if text is None:
        return None
    code = COUNTRY_NAMES.get(text.casefold(), text.upper())
    return code if code in COUNTRY_CODES else None


def _json_value(value: object) -> object:
    value = _scalar(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if is_missing(value):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (Real, Decimal)):
        return scalar_number(value)
    # Do not stringify arbitrary objects (including Series/arrays) as evidence.
    return {"unsupported_type": f"{type(value).__module__}.{type(value).__name__}"}


def _plain_text(value: object) -> str | None:
    text = scalar_text(value)
    if text is None:
        return None
    text = unescape(text)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"</?(?:p|div|li|br|h[1-6])\b[^>]*>", "\n", text, flags=re.I)
    return re.sub(r"<[^>]*>", " ", text).strip() or None


def _mode(value: object) -> WorkMode:
    text = scalar_text(value)
    return {"remote": WorkMode.REMOTE, "onsite": WorkMode.ONSITE,
            "on-site": WorkMode.ONSITE, "on_site": WorkMode.ONSITE,
             "hybrid": WorkMode.HYBRID}.get((text or "").lower(), WorkMode.UNKNOWN)


def _country_list(text: str) -> list[str] | None:
    whole = country_code(text.removeprefix("the "))
    if whole:
        return [whole]
    parts = re.split(r",\s*(?:and\s+|or\s+)?|\s+(?:or|and)\s+", text)
    codes = [country_code(part.strip().removeprefix("the ")) for part in parts]
    return list(dict.fromkeys(codes)) if codes and all(codes) else None


def _eligibility_clause(clause: str) -> tuple[dict, bool]:
    """Recognize complete legal/location clauses, not generic skill obligations."""
    lower = clause.casefold()
    statements = {
        "no security clearance required": {"clearance_required": "no"},
        "no citizenship requirement": {"citizenship_required": "no"},
        "no residency requirement": {"residency_required": "no"},
        "no work authorization required": {"authorization_required": "no"},
        "visa sponsorship is available for this role": {"sponsorship_supported": "yes"},
        "visa sponsorship is not available for this role": {"sponsorship_supported": "no"},
        "this is an independent contractor role": {"engagement": "contractor"},
        "this is a direct employment role": {"engagement": "direct_employee"},
        "compensation is the same regardless of location": {"pay_basis": "global"},
        "compensation is based on your location": {"pay_basis": "localized"},
        "relocation is required for this role": {"relocation_required": "yes"},
        "relocation support is available for this role": {"relocation_supported": "yes"},
    }
    if lower in statements:
        return statements[lower], True
    if re.fullmatch(r"(?:this (?:role|position) is )?(?:fully )?remote", lower):
        return {"work_mode": "remote"}, False
    if re.fullmatch(r"this (?:role|position) is open to (?:candidates|applicants) (?:worldwide|globally|anywhere in the world)", lower):
        return {"eligible_scope": "worldwide"}, True
    if re.fullmatch(r"(?:no (?:visa )?sponsorship(?: (?:is )?available)?|(?:visa )?sponsorship (?:is )?not (?:available|provided))", lower):
        return {"sponsorship_supported": "no"}, True
    if re.fullmatch(r"(?:a )?security clearance (?:is )?required", lower):
        return {"clearance_required": "yes"}, True
    match = re.fullmatch(
        r"(?:this (?:role|position) is |we hire )?(?:fully )?remote "
        r"(?:worldwide|globally|anywhere in the world)"
        r"(?: (?:as |for )?(?:an? )?(?:independent )?contractors?)?"
        r"(?: (?:except|excluding) (.+))?", clause, re.I)
    if match:
        parsed = {"work_mode": "remote", "eligible_scope": "worldwide"}
        if re.search(r"\bcontractors?\b", lower):
            parsed["engagement"] = "contractor"
        if match[1]:
            codes = _country_list(match[1])
            if not codes:
                return {}, True
            parsed["excluded_countries"] = codes
        return parsed, True
    match = re.fullmatch(
        r"(?:(?:all )?(?:candidates|applicants) (?:must|may) (?:reside|be based|be located)|"
        r"remote (?:work )?(?:is )?(?:allowed|available)) in (.+)", clause, re.I)
    if match:
        codes = _country_list(match[1])
        if codes:
            parsed = {"countries": codes, "eligible_scope": "restricted"}
            if lower.startswith("remote"):
                parsed["work_mode"] = "remote"
            elif "must" in lower:
                parsed.update(residency_required="yes", residency_countries=codes)
            return parsed, True
        return {}, True
    match = re.fullmatch(r"(?:(?:all )?(?:candidates|applicants) )?must (?:already )?(?:be (?:legally )?authori[sz]ed to work|have (?:the )?right to work) in (.+)", clause, re.I)
    if match:
        codes = _country_list(match[1])
        return ({"authorization_required": "yes", "authorization_countries": codes} if codes else {}), True
    match = re.fullmatch(r"(?:all )?(?:candidates|applicants) must be citizens of (.+)", clause, re.I)
    if match:
        codes = _country_list(match[1])
        return ({"citizenship_required": "yes", "citizenship_countries": codes} if codes else {}), True
    match = re.fullmatch(r"(?:candidates|applicants) (?:in|from) (.+) are not eligible(?: for this role)?", clause, re.I)
    if match:
        codes = _country_list(match[1])
        return ({"excluded_countries": codes} if codes else {}), True
    match = re.fullmatch(r"(?:this role (?:supports|uses|can be hired (?:through|via))|we hire for this role (?:through|via)) (?:an? )?(?:eor|employer of record) in (.+)", clause, re.I)
    if match:
        codes = _country_list(match[1])
        return ({"engagement": "eor", "countries": codes, "eligible_scope": "restricted"} if codes else {}), True
    # 'Must know Python', incidental sales titles, API authorization and product
    # timezone handling are role prose, not applicant geography restrictions.
    obligation = re.search(r"\b(?:must|required?|only|except|excluding|restricted|within|between|cannot|not available|need|at least|subject to)\b", lower)
    inclusive = re.search(
        r"\b(?:regardless of|without regard to)\b[^.;]{0,160}"
        r"\b(?:citizenship|nationality|national origin|residency|immigration status)\b",
        lower,
    )
    if inclusive and not obligation:
        return {}, False
    legal = re.search(r"\b(?:work authori[sz]ation|authori[sz]ed to work|right to work|citizenship|citizens? of|residency|residence requirement|security clearance|visa|sponsorship|export control|it ar|itar|sanction\w*)\b", lower)
    personal = re.search(r"\b(?:candidates?|applicants?|you|employees?|workers?)\b", lower)
    personal_restriction = personal and re.search(
        r"\b(?:reside in|residents? (?:of|in)|eligible to work|work eligibility|sponsorship)\b", lower)
    sponsor_restriction = re.search(
        r"\b(?:cannot|can't|do not|don't|will not|won't|unable to|not able to)\s+(?:\w+\s+){0,3}sponsor\b", lower)
    citizenship_restriction = re.search(
        r"\bcitizens?\s+only\b|\bonly\s+(?:\w+\s+){0,3}citizens?\b|"
        r"\bmust be\s+(?:\w+\s+){0,3}citizens?\b", lower)
    relocation = re.search(r"^relocation (?:is )?(?:required|mandatory)\b|\bmust (?:be willing to )?relocate(?: to| for|$)\b", lower)
    geographic = re.search(r"\b(?:remote|work from|based in|located in|time\s?zones?|utc|gmt|overlap|office attendance)\b", lower)
    return {}, bool(legal or personal_restriction or sponsor_restriction or citizenship_restriction
                    or relocation or geographic and obligation or
                    re.search(r"\b(?:remote|onsite|on-site|hybrid)\b.*\b(?:or|alternatively)\b", lower))


def normalize_record(record: dict, provider: str, tenant: str | None = None,
                     api_region: str | None = None) -> JobCandidate:
    """Detach one adapter dict and its ``raw_record`` without IO.

    Explicit alternatives use WorkArrangement field names and a stable ``id``.
    Optional ``evidence``/``facts`` are EvidenceFact-shaped dicts; ``evidence_ids``
    binds an alternative to those facts. Kinds are the exact model field names.
    Without explicit references, structured fields get path-scoped evidence.
    ``requirements_complete: true`` is an explicit producer assertion that the
    supplied requirement fields are exhaustive, not something inferred from prose.
    Unparsed requirements and malformed fields are retained as normalization_issue
    facts and cannot authorize an application. Raw snapshots are provenance only.
    """
    if not isinstance(record, dict) or not isinstance(provider, str) or not provider.strip():
        raise ValueError("normalize_record requires a dict and a nonempty provider")
    provider = provider.strip().lower()
    data = _json_value(record)
    raw = data.get("raw_record")
    raw = raw if isinstance(raw, dict) else {}
    facts: list[EvidenceFact] = []
    direct = provider in {"greenhouse", "lever", "ashby", "workday", "amazon",
                          "workable", "smartrecruiters", "bamboohr", "jazzhr"}
    authority = Authority.EMPLOYER if direct else Authority.AGGREGATOR

    def pick(*names):
        for source, prefix in ((data, ""), (raw, "raw_record.")):
            for name in names:
                if name in source and source[name] is not None:
                    return source[name], prefix + name
        return None, names[0]

    listing, _ = pick("url", "job_url", "absolute_url", "hostedUrl", "jobUrl")
    listing = scalar_text(listing)
    observation = scalar_text(data.get("source_observation")) or listing
    observed = scalar_text(data.get("observed_at"))

    def fact(kind, value, path, aid=None, excerpt=None, source_authority=None):
        # Path + option identity, not value or retrieval time, defines a fact ID.
        seed = json.dumps([provider, tenant, api_region, observation, aid, path, kind])
        eid = "fact-" + hashlib.sha256(seed.encode()).hexdigest()[:20]
        item = EvidenceFact(
            value=_json_value(value), kind=kind, authority=source_authority or authority,
            source_observation=observation, provider_field_path=path, excerpt=excerpt,
            observed_at=observed, id=eid, arrangement_id=aid, source_url=listing,
            rule_version=NORMALIZER_VERSION,
        )
        # Reassessment reuses frozen facts. Do not manufacture duplicate IDs for
        # an unchanged clause; retain changed values as distinct conflict facts.
        existing = next((f for f in facts if f.id == eid), None)
        if existing is not None:
            if existing.value == item.value and existing.kind == item.kind:
                return eid
            eid += "-" + hashlib.sha256(json.dumps(item.value, sort_keys=True).encode()).hexdigest()[:12]
            item = EvidenceFact(**{**item.__dict__, "id": eid})
        if not any(f.id == eid for f in facts):
            facts.append(item)
        return eid

    def issue(message, path, aid=None):
        optional = re.search(r"\b(?:office_\w+|pay_basis|schedule|relocation_supported|hiring_organization\w*)\b", message)
        fact("normalization_note" if optional else "normalization_issue", message, path, aid,
             source_authority=Authority.RULE)

    if "raw_record" in data:
        if not isinstance(data["raw_record"], dict):
            issue("raw_record must be a mapping", "raw_record")

    supplied, supplied_path = pick("evidence", "facts")
    supplied = [] if supplied is None else supplied
    if not isinstance(supplied, list):
        issue("evidence must be a list", supplied_path)
        supplied = []
    evidence_fields = {item.name for item in fields(EvidenceFact)}
    for entry in supplied:
        if not isinstance(entry, dict):
            issue("malformed evidence entry", supplied_path)
            continue
        try:
            if set(entry) - evidence_fields:
                raise ValueError("unknown evidence fields")
            values = dict(entry)
            if values.get("kind") in {"raw_record", "normalized_record"}:
                continue
            values["authority"] = Authority(values.get("authority"))
            for key in evidence_fields - {"value", "authority", "countries"}:
                if key in values and values[key] is not None and not isinstance(values[key], str):
                    raise ValueError("evidence metadata must be strings")
            if not values.get("id") or not values.get("kind") or "value" not in values:
                raise ValueError("evidence requires id, kind and value")
            countries = values.get("countries", [])
            if not isinstance(countries, list) or any(country_code(x) is None for x in countries):
                raise ValueError("malformed evidence country scope")
            values["countries"] = tuple(country_code(x) for x in countries)
            facts.append(EvidenceFact(**values))
        except (ValueError, TypeError) as error:
            issue(str(error), supplied_path, scalar_text(entry.get("arrangement_id")))

    title, title_path = pick("title", "job_title", "jobTitle", "position", "text")
    title = scalar_text(title)
    if title:
        fact("title", title, title_path)
    company, _ = pick("company", "employer_name", "company_name", "employerName")
    if isinstance(company, dict):
        company = company.get("name")
    description, description_path = pick("full_description", "description", "descriptionPlain",
                                          "description_text", "jobDescription", "content")
    description = _plain_text(description)
    if description:
        fact("description", description, description_path)

    arrangement_fields = {item.name: item for item in fields(WorkArrangement)}
    default = WorkArrangement()
    paths = {}
    option_paths = {}
    categories = raw.get("categories") if isinstance(raw.get("categories"), dict) else {}
    explicit, explicit_path = pick("work_arrangements")
    explicit_present = "work_arrangements" in data or "work_arrangements" in raw
    options = []
    if explicit_present:
        if not isinstance(explicit, list) or not explicit:
            issue("work_arrangements must be a nonempty list", explicit_path)
        else:
            options = [(dict(entry) if isinstance(entry, dict) else entry, explicit_path) for entry in explicit]
    else:
        option = {"id": "default"}
        paths = {}

        def put(key, value, path):
            if value is not None:
                option[key], paths[key] = value, path

        # Only explicit model fields are shared. Generic countries/location are
        # not eligible geography without a documented role-specific meaning.
        for key in sorted(arrangement_fields.keys() - {
                "id", "focus", "office_locations", "office_details",
                "legal_employer", "legal_employer_country", "payroll_intermediary"}):
            # Bare provider countries may describe offices or an inventory.
            # Top-level model fields are the adapter's normalized contract.
            value, path = (data.get(key), key) if key in {"countries", "regions", "cities"} else pick(key)
            put(key, value, path)
        value, path = pick("work_mode", "workplaceType", "workplace_type")
        put("work_mode", value, path)
        if "work_mode" not in option:
            value, path = pick("is_remote", "isRemote")
            if scalar_bool(value) is True:
                put("work_mode", "remote", path)
        value, path = pick("schedule", "job_type", "employment_type", "employmentType")
        if value is None:
            value, path = categories.get("commitment"), "raw_record.categories.commitment"
        if isinstance(value, str):
            schedule = value.lower().replace("-", "_").replace(" ", "_")
            schedule = {"fulltime": "full_time", "parttime": "part_time"}.get(schedule, schedule)
            put("schedule", schedule, path)
            if schedule in {"contract", "contractor", "freelance"}:
                put("engagement", "contractor", path)
        if provider == "remotive":
            put("work_mode", "remote", "provider.remotive.work_mode")
            value, path = pick("candidate_required_location")
            if value is None:
                value, path = pick("location")
            if isinstance(value, str):
                if value.lower() in {"worldwide", "anywhere", "global"}:
                    put("eligible_scope", "worldwide", path)
                else:
                    parts = re.split(r"\s*[,;]\s*", value)
                    codes = [country_code(part) for part in parts]
                    if all(codes):
                        put("countries", codes, path)
                        put("eligible_scope", "restricted", path)
                    else:
                        issue("unparsed candidate location: " + value, path)
        value, path = pick("requirements_complete")
        put("requirements_complete", value, path)
        options = [(option, "inferred")]

    # Prefer the original paired provider objects over an adapter's display
    # string. Lever's posting.country describes its primary location only.
    locations, location_path = pick("office_details", "office_locations", "office_location")
    if provider == "greenhouse" and raw.get("offices"):
        locations, location_path = raw["offices"], "raw_record.offices"
    elif provider == "lever" and (categories or raw.get("allLocations")):
        all_locations = categories.get("allLocations", raw.get("allLocations", []))
        locations = list(all_locations) if isinstance(all_locations, list) else []
        primary = categories.get("location")
        if primary and primary not in locations:
            locations.insert(0, primary)
        locations = [{"location": item, "country": raw.get("country") if item == primary else None}
                     if isinstance(item, str) else item for item in locations]
        location_path = "raw_record.categories.allLocations"
    elif provider == "ashby" and (raw.get("location") or raw.get("address") or raw.get("secondaryLocations")):
        locations = []
        if raw.get("location") or raw.get("address"):
            locations.append({"location": raw.get("location"), "address": raw.get("address")})
        if isinstance(raw.get("secondaryLocations"), list):
            locations.extend(raw["secondaryLocations"])
        location_path = "raw_record.primary_secondary_locations"
    if not locations:
        location, location_path = pick("location", "locationName")
        locations = [location] if location is not None else []
    elif isinstance(locations, (dict, str)):
        locations = [locations]

    def office(item):
        if isinstance(item, str):
            return office({"location": item})
        if not isinstance(item, dict):
            return None, None, None
        address = item.get("address")
        address = address if isinstance(address, dict) else {}
        postal = address.get("postalAddress")
        postal = postal if isinstance(postal, dict) else address
        location = item.get("location")
        nested = location if isinstance(location, dict) else {}
        name = scalar_text(nested.get("name")) or scalar_text(location) or scalar_text(item.get("name"))
        name = name or scalar_text(postal.get("addressLocality")) or scalar_text(postal.get("city"))
        code = country_code(item.get("country")) or country_code(nested.get("country"))
        code = code or country_code(postal.get("addressCountry")) or country_code(postal.get("country"))
        # An exact terminal country in an address is explicit geography, not a
        # city-to-country lookup. Missing country remains missing.
        code = code or country_code(name) or (country_code(name.rsplit(",", 1)[-1]) if name and "," in name else None)
        return item, name, code

    if not explicit_present and isinstance(locations, list):
        alternatives, seen = [], set()
        for item in locations:
            detail, name, code = office(item)
            if detail is None:
                fact("office_metadata", item, location_path)
                continue
            key = scalar_text(detail.get("id")) or name or "unspecified-office"
            if key in seen:
                continue
            seen.add(key)
            aid = "office-" + hashlib.sha256(key.encode()).hexdigest()[:16]
            entry = dict(option, id=aid, office_details=[detail])
            local_mode = detail.get("work_mode", detail.get("workplaceType"))
            if local_mode is not None:
                entry["work_mode"] = local_mode
            if name:
                entry["office_locations"] = [name]
            if code:
                entry["office_countries"] = [code]
            elif len(locations) > 1:
                # A separately flattened country list has no pairing contract.
                entry.pop("office_countries", None)
            elif "office_countries" not in entry:
                value, country_path = pick("office_country")
                if country_code(value):
                    entry["office_countries"] = [country_code(value)]
                    paths["office_countries"] = country_path
            if _mode(name) != WorkMode.UNKNOWN and "work_mode" not in entry:
                entry["work_mode"] = _mode(name).value
            option_paths[aid] = {**paths, **{k: location_path + "[" + key + "]" for k in
                                          ("office_details", "office_locations", "office_countries")}}
            alternatives.append((entry, location_path))
        if alternatives:
            options = alternatives

    # Use the same extraction on initial normalization and frozen reassessment.
    # Default-valued fields in serialized arrangements are absence, unless an
    # explicit evidence reference says they were actually observed as unknown.
    for entry, path in options:
        if not isinstance(entry, dict):
            continue
        aid = scalar_text(entry.get("id"))
        refs = entry.get("evidence_ids")
        if isinstance(refs, list) and refs:
            for key in list(entry):
                if key in arrangement_fields and key not in {"id", "evidence_ids"}:
                    if entry[key] == _json_value(getattr(default, key)) and not any(
                            f.id in refs and f.kind == key for f in facts):
                        del entry[key]
        if "office_locations" in entry and isinstance(entry["office_locations"], (list, dict)):
            items = entry["office_locations"]
            items = [items] if isinstance(items, dict) else items
            if any(isinstance(x, dict) for x in items):
                parsed_offices = [office(x) for x in items]
                entry["office_details"] = [x[0] for x in parsed_offices if x[0] is not None]
                entry["office_locations"] = [x[1] for x in parsed_offices if x[1]]
                if len(parsed_offices) == 1 and parsed_offices[0][2]:
                    entry.setdefault("office_countries", [parsed_offices[0][2]])
        if isinstance(entry.get("office_details"), list):
            parsed_offices = [office(x) for x in entry["office_details"]]
            entry.setdefault("office_locations", [x[1] for x in parsed_offices if x[1]])
            if len(parsed_offices) == 1 and parsed_offices[0][2]:
                entry.setdefault("office_countries", [parsed_offices[0][2]])
        if isinstance(entry.get("legal_employer"), dict):
            entity = entry["legal_employer"]
            entry["legal_employer"] = scalar_text(entity.get("name"))
            if country_code(entity.get("country")):
                entry.setdefault("legal_employer_country", country_code(entity["country"]))

    resolved_clauses = {}
    for clause in re.split(r"[\n!?;]+|\.(?:\s|$)", description or ""):
        clause = re.sub(r"\s+", " ", clause).strip(" \t-*:")
        if not clause:
            continue
        body = clause
        selector = re.match(r"for (?:the )?(remote|onsite|on-site|hybrid) (?:option|arrangement)[,:] ?(.+)", clause, re.I)
        if selector:
            body = selector[2]
        parsed, restrictive = _eligibility_clause(body)
        if not parsed and not restrictive:
            continue
        valid_options = [entry for entry, _ in options if isinstance(entry, dict)]
        targets = []
        scoped_ids = {f.arrangement_id for f in facts if f.excerpt and f.arrangement_id and
                      clause.casefold() == re.sub(r"\s+", " ", f.excerpt).strip().casefold()}
        if selector:
            targets = [entry for entry in valid_options if _mode(entry.get("work_mode")) == _mode(selector[1])]
        elif scoped_ids:
            targets = [entry for entry in valid_options if entry.get("id") in scoped_ids]
        elif len(valid_options) == 1:
            targets = valid_options
        elif parsed.get("work_mode"):
            targets = [entry for entry in valid_options if _mode(entry.get("work_mode")) == _mode(parsed["work_mode"])]
        elif re.search(r"\b(?:all (?:candidates|applicants)|for this role|this is an? .* role)\b", body, re.I):
            targets = valid_options
        if not targets:
            issue("unscoped posting restriction: " + clause, description_path)
            continue
        for entry in targets:
            aid = scalar_text(entry.get("id"))
            clause_path = description_path + ":clause:" + hashlib.sha256(clause.encode()).hexdigest()[:16]
            if not parsed:
                issue("unresolved eligibility statement: " + clause, clause_path, aid)
                continue
            for key, value in parsed.items():
                old = entry.get(key)
                absent = key not in entry or old is None or old == "unknown" or old == []
                if absent:
                    entry[key] = value
                elif old != value:
                    issue("conflicting statement for " + key, clause_path + ":" + key, aid)
                    eid = fact("conflicting_statement", parsed, clause_path, aid, clause, Authority.EMPLOYER)
                    # Retain this hard evidence in the frozen reassessment refs.
                    entry.setdefault("_conflict_refs", []).append(eid)
                    continue
                eid = fact(key, value, clause_path + ":" + key, aid, clause, Authority.EMPLOYER)
                # Initial fields are still generated below. Frozen arrangements
                # already have refs, so explicitly attach the enriched clause.
                if entry.get("evidence_ids"):
                    entry["evidence_ids"] = list(dict.fromkeys([*entry["evidence_ids"], eid]))
            resolved_clauses.setdefault(clause.casefold(), set()).add(aid)

    # Old normalization failures are not hard legal facts. Reclassify only
    # demonstrably non-eligibility prose/office metadata; keep every conflict and
    # every unresolved eligibility clause, including clauses absent from enrichment.
    for index, item in enumerate(facts):
        if item.kind != "normalization_issue" or not isinstance(item.value, str):
            continue
        message = item.value
        irrelevant = message.startswith(("malformed office_", "unresolved office_", "multiple office locations"))
        if message.startswith("unresolved statement: "):
            parsed, restrictive = _eligibility_clause(message.split(": ", 1)[1])
            irrelevant = not parsed and not restrictive
        if message.startswith(("unresolved statement: ", "unscoped posting restriction: ", "unresolved eligibility statement: ")):
            resolved = resolved_clauses.get(message.split(": ", 1)[1].casefold(), set())
            expected = {item.arrangement_id} if item.arrangement_id else {
                entry.get("id") for entry, _ in options if isinstance(entry, dict)}
            irrelevant = irrelevant or bool(expected and expected <= resolved)
        if irrelevant:
            facts[index] = EvidenceFact(**{**item.__dict__, "kind": "normalization_note"})

    arrangements = []
    for entry, path in options:
        if not isinstance(entry, dict):
            issue("malformed work arrangement", path)
            continue
        aid = scalar_text(entry.get("id"))
        if not isinstance(entry.get("id"), str) or not aid:
            issue("arrangement requires a stable string id", path)
            aid = None
        values = {"id": aid}
        refs = entry.get("evidence_ids", [])
        if not isinstance(refs, list) or any(not isinstance(x, str) or not x for x in refs):
            issue("malformed evidence_ids", path, aid)
            refs = []
        refs = list(refs)
        conflict_refs = entry.pop("_conflict_refs", [])
        unknown_keys = set(entry) - arrangement_fields.keys() - {"requirements_complete"}
        if unknown_keys:
            issue("unrecognized arrangement fields: " + ", ".join(sorted(unknown_keys)), path, aid)
        for key, value in entry.items():
            if key in {"id", "evidence_ids"} or key in unknown_keys:
                continue
            field_path = option_paths.get(aid, paths).get(key, key) if not explicit_present else f"{path}[{aid}].{key}"
            if key == "requirements_complete":
                normalized = scalar_tristate(value)
            elif key == "work_mode":
                normalized = _mode(value)
            elif isinstance(getattr(default, key), TriState):
                normalized = scalar_tristate(value)
            elif key in {"office_details", "metric_requirements"}:
                if not isinstance(value, list) or any(not isinstance(x, dict) for x in value):
                    issue("malformed " + key, field_path, aid)
                    continue
                normalized = tuple(value)
            elif isinstance(getattr(default, key), tuple):
                if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
                    issue("malformed " + key, field_path, aid)
                    continue
                if key == "countries" or key.endswith("_countries"):
                    if any(country_code(x) is None for x in value):
                        issue("invalid country in " + key, field_path, aid)
                        continue
                    value = [country_code(x) for x in value]
                normalized = tuple(dict.fromkeys(x.strip() for x in value))
            else:
                normalized = scalar_text(value)
                if key.endswith("_country") and normalized:
                    normalized = country_code(normalized)
                allowed = {"engagement": {"contractor", "eor", "direct_employee"},
                           "pay_basis": {"global", "localized"},
                           "eligible_scope": {"worldwide", "restricted"}}
                if key in allowed and normalized not in allowed[key]:
                    if normalized not in (None, "unknown"):
                        issue("unknown " + key, field_path, aid)
                    normalized = None
            # An explicit unknown requirement is evidence of unresolved
            # applicability; optional preference/office metadata isn't a veto.
            if key != "requirements_complete":
                values[key] = normalized
            if not entry.get("evidence_ids"):
                refs.append(fact(key, normalized, field_path, aid))
        refs.extend(f.id for f in facts if f.arrangement_id == aid and f.excerpt and
                    f.kind in arrangement_fields and f.id not in refs)
        refs.extend(eid for eid in conflict_refs if eid not in refs)
        arrangements.append(WorkArrangement(**values, evidence_ids=tuple(refs)))

    compensation = []
    salary, salary_path = pick("salary")
    salary_data = salary if isinstance(salary, dict) else {}
    if provider == "lever" and isinstance(raw.get("salaryRange"), dict):
        salary_data = raw["salaryRange"]
    if provider == "freehire" and isinstance(raw.get("enrichment"), dict):
        salary_data = raw["enrichment"]
    bounds = {}
    for key, aliases in {
        "currency": ("currency", "salary_currency"), "period": ("period", "interval", "salary_period"),
        "min_value": ("min_value", "min_amount", "salary_min", "minimumSalary", "min"),
        "max_value": ("max_value", "max_amount", "salary_max", "maximumSalary", "max"),
    }.items():
        value = next((salary_data[name] for name in aliases if salary_data.get(name) is not None), None)
        if value is None:
            value, _ = pick(*aliases)
        bounds[key] = scalar_number(value) if key.endswith("_value") else scalar_text(value)
    original = scalar_text(salary)
    if original or any(value is not None for value in bounds.values()):
        eid = fact("compensation", {**bounds, "original_text": original}, salary_path)
        compensation.append(CompensationRange(**bounds, original_text=original, evidence_ids=(eid,)))
    # Keep richer provider compensation intact, even when its tiers cannot yet be
    # assigned to one option. It is not used for ranking or a salary floor.
    if isinstance(raw.get("compensation"), dict):
        fact("provider_compensation", raw["compensation"], "raw_record.compensation")

    organization = {}
    for key in ("hiring_organization", "hiring_organization_id", "hiring_organization_hq_country",
                "hiring_organization_hq_location", "legal_employer", "legal_employer_country",
                "payroll_intermediary"):
        value, path = pick(key)
        entity_name = "hiring_organization" if key.startswith("hiring_organization") else "legal_employer"
        entity, entity_path = pick(entity_name)
        if isinstance(entity, dict):
            nested_key = {entity_name: "name", entity_name + "_id": "id",
                          entity_name + "_hq_country": "hq_country",
                          entity_name + "_hq_location": "hq_location",
                          entity_name + "_country": "country"}.get(key)
            if nested_key and (value is None or isinstance(value, dict)):
                value, path = entity.get(nested_key), entity_path + "." + nested_key
        value = country_code(value) if key.endswith("_country") else scalar_text(value)
        organization[key] = value
        if value:
            fact(key, value, path)
    posting, _ = pick("source_posting_id", "posting_id", "id", "jobId", "public_slug")
    internal, _ = pick("source_internal_id", "internal_job_id", "internal_id")
    requisition, _ = pick("source_requisition_id", "requisition_id")
    application, _ = pick("application_url", "apply_url", "applyUrl", "job_url_direct")
    canonical, _ = pick("canonical_target")
    posted, _ = pick("posted_at", "first_published", "publishedAt", "publication_date", "date_posted", "date")
    updated, _ = pick("updated_at", "source_updated_at")
    requirements, _ = pick("requirements")
    if requirements is not None and not isinstance(requirements, dict):
        issue("requirements must be a mapping", "requirements")
        requirements = {}
    return JobCandidate(
        title=title, company=scalar_text(company), description=description,
        identity=SourceIdentity(provider, tenant, api_region, scalar_text(posting),
                                scalar_text(requisition), scalar_text(internal), listing),
        work_arrangements=tuple(arrangements), compensation=tuple(compensation),
        requirements={key: scalar_tristate(value) for key, value in (requirements or {}).items()},
        application_url=scalar_text(application), canonical_target=scalar_text(canonical),
        source_posted_at=scalar_text(posted), source_updated_at=scalar_text(updated),
        facts=tuple(facts), normalizer_version=NORMALIZER_VERSION, **organization,
    )

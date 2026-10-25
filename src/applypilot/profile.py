"""Pure projection of the local profile schema into discovery applicant facts.

Unscoped work_authorization answers are retained, never assigned to residence
or a search target. Optional work_authorization.by_country maps ISO country
codes to objects using the same authorization/sponsorship answer keys.
"""

from collections.abc import Mapping
import re
from typing import Any

from applypilot.discovery.countries import COUNTRY_CODES, COUNTRY_NAMES
from applypilot.discovery.models import ApplicantSnapshot, TriState


def _country(value: Any, path: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{path} must be a country string or null")
    value = value.strip()
    if value.lower() in ("", "unknown", "your country"):
        return None
    # Explicit names only; no city inference, demonym guessing or search config.
    code = COUNTRY_NAMES.get(value.casefold(), value.upper())
    if code not in COUNTRY_CODES:
        raise ValueError(f"{path} must be an ISO alpha-2 code or supported country name")
    return code


def _answer(value: Any, path: str) -> TriState:
    if value is None:
        return TriState.UNKNOWN
    if type(value) is bool:
        return TriState.YES if value else TriState.NO
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("", "unknown"):
            return TriState.UNKNOWN
        if normalized in ("yes", "true"):
            return TriState.YES
        if normalized in ("no", "false"):
            return TriState.NO
    raise ValueError(f"{path} must be yes/no, true/false, unknown or null")


def _aliased_answer(data: Mapping, keys: tuple[str, str], path: str) -> TriState:
    answers = {_answer(data[key], f"{path}.{key}") for key in keys if key in data}
    answers.discard(TriState.UNKNOWN)
    if len(answers) > 1:
        raise ValueError(f"{path} has conflicting {keys[0]} and {keys[1]} answers")
    return next(iter(answers), TriState.UNKNOWN)


def citizenship_countries(value: Any) -> tuple[str, ...]:
    """Normalize only explicitly supplied citizenship countries."""
    if value is None:
        return ()
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (list, tuple)):
        raise ValueError("personal.citizenship must be a country string, list or null")
    return tuple(dict.fromkeys(
        code for country in value
        if (code := _country(country, "personal.citizenship")) is not None
    ))


def apply_citizenship_defaults(profile: dict, explicit_work: Mapping | None = None) -> dict:
    """Materialize editable defaults, retaining explicit country-specific answers.

    Origin markers let a citizenship change remove only untouched defaults.
    Residence, names and search destinations never supply citizenship.
    """
    citizens = citizenship_countries((profile.get('personal') or {}).get('citizenship'))
    work = profile.get('work_authorization') or {}
    countries = work.get('by_country') or {}
    if not isinstance(countries, dict):
        raise ValueError('work_authorization.by_country must be a mapping or null')
    defaults = {'legally_authorized_to_work': True, 'require_sponsorship': False}
    aliases = {'legally_authorized_to_work': 'legally_authorized', 'require_sponsorship': 'needs_sponsorship'}
    explicit = (explicit_work or {}).get('by_country') or {}
    for code, facts in list(countries.items()):
        if not isinstance(facts, dict):
            raise ValueError('work_authorization.by_country entries must be mappings')
        supplied = explicit.get(code) or {}
        inherited = [key for key in facts.get('citizenship_defaults', [])
                     if key in defaults and key not in supplied and aliases[key] not in supplied]
        if code not in citizens:
            for key in inherited:
                if facts.get(key) is defaults[key]:
                    facts.pop(key, None)
            inherited = []
        if inherited:
            facts['citizenship_defaults'] = inherited
        else:
            facts.pop('citizenship_defaults', None)
        if not facts:
            countries.pop(code, None)
    for code in citizens:
        facts = countries.setdefault(code, {})
        inherited = list(facts.get('citizenship_defaults') or [])
        for key, value in defaults.items():
            if key not in facts and aliases[key] not in facts:
                facts[key] = value
                inherited.append(key)
        if inherited:
            facts['citizenship_defaults'] = inherited
    if countries or 'work_authorization' in profile:
        work['by_country'] = countries
        profile['work_authorization'] = work
    return profile


def build_applicant_snapshot(profile: Mapping[str, Any]) -> ApplicantSnapshot:
    """Validate and detach relevant facts; never read files or retain credentials.

    Supports both profile.example.json and the setup wizard's boolean aliases.
    personal.citizenship accepts a country string or a list of country strings.
    Missing lists mean no known facts, not an exhaustive negative declaration.
    Country-scoped answers require explicit by_country entries; work_permit_type
    is descriptive only and does not establish authorization or citizenship.
    """
    if not isinstance(profile, Mapping):
        raise ValueError("profile must be a mapping")
    sections = {}
    for key in ("personal", "work_authorization"):
        value = profile.get(key)
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError(f"profile.{key} must be a mapping or null")
        sections[key] = value
    personal = sections["personal"]
    work = sections["work_authorization"]
    citizens = citizenship_countries(personal.get("citizenship"))
    authorization_keys = ("legally_authorized_to_work", "legally_authorized")
    sponsorship_keys = ("require_sponsorship", "needs_sponsorship")
    by_country = work.get("by_country")
    if by_country is None:
        by_country = {}
    if not isinstance(by_country, Mapping):
        raise ValueError("work_authorization.by_country must be a mapping or null")
    authorization = {}
    sponsorship = {}
    for country, facts in by_country.items():
        code = _country(country, "work_authorization.by_country key")
        if code is None or code in authorization:
            raise ValueError("work_authorization.by_country has an unknown or duplicate country")
        if not isinstance(facts, Mapping):
            raise ValueError("work_authorization.by_country entries must be mappings")
        authorization[code] = _aliased_answer(facts, authorization_keys, f"work_authorization.by_country.{code}")
        sponsorship[code] = _aliased_answer(facts, sponsorship_keys, f"work_authorization.by_country.{code}")
    return ApplicantSnapshot(
        country=_country(personal.get("country"), "personal.country"),
        citizenship=citizens,
        city=personal.get("city"),
        province_state=personal.get("province_state"),
        legally_authorized_to_work=_aliased_answer(work, authorization_keys, "work_authorization"),
        require_sponsorship=_aliased_answer(work, sponsorship_keys, "work_authorization"),
        work_permit_type=work.get("work_permit_type"),
        authorization_by_country=tuple(authorization.items()),
        sponsorship_by_country=tuple(sponsorship.items()),
    )



def resume_evidence_entries(profile: Mapping[str, Any]) -> list[dict]:
    """Detached prompt projection, honoring both entry and per-claim decisions.

    Imported approval is respected, not presented as fresh source verification.
    When structured claims exist they are authoritative: a revoked claim cannot
    re-enter through an out-of-date allowed_claims mirror. Legacy string-only
    entries remain supported. This function never changes stored evidence.
    """
    from copy import deepcopy

    result = []
    for raw in profile.get("public_evidence") or []:
        if not isinstance(raw, Mapping) or raw.get("resume_eligible") is not True:
            continue
        private = raw.get("private") is True or raw.get("visibility") in {"private", "internal"}
        if private and raw.get("disclosure_approved") is not True:
            continue
        entry = deepcopy(dict(raw))
        if "claims" in raw:
            claims = [deepcopy(dict(c)) for c in (raw.get("claims") or [])
                      if isinstance(c, Mapping) and c.get("resume_eligible") is True
                      and isinstance(c.get("allowed_resume_claim"), str)
                      and c["allowed_resume_claim"].strip()]
            allowed = [c["allowed_resume_claim"].strip() for c in claims]
            entry["claims"] = claims
        else:
            allowed = [c.strip() for c in (raw.get("allowed_claims") or [])
                       if isinstance(c, str) and c.strip()]
        if not allowed:
            continue
        entry["allowed_claims"] = list(dict.fromkeys(allowed))
        if private:
            safe_claims = [{key: claim[key] for key in (
                "id", "allowed_resume_claim", "source_revision", "resume_eligible") if key in claim}
                for claim in entry.get("claims", [])]
            entry = {key: entry[key] for key in (
                "id", "provider", "scope", "version", "allowed_claims", "resume_eligible") if key in entry}
            if "claims" in raw:
                entry["claims"] = safe_claims
            entry.update(title="Private project", url="", visibility="private", disclosure_approved=True)
        result.append(entry)
    return result


def public_evidence_context(profile: Mapping[str, Any], job: Mapping[str, Any] | None = None,
                            limit: int | None = None) -> str:
    """Format usable project claims. Optional overlap ranks retrieval, NOT proof.

    Never grant skills, employment tenure, deployment or verification from
    language statistics or a search match. Score only decides prompt order.
    """
    import re

    entries = resume_evidence_entries(profile)
    if job:
        stop = {"with", "this", "that", "from", "your", "have", "will", "work", "using",
                "and", "the", "for", "our", "are", "you", "built", "developed", "engineer"}
        def terms(text):
            return set(re.findall(r"[a-z][a-z0-9.+#-]{2,}", str(text).lower())) - stop
        wanted = terms(str(job.get("title") or "") + " " + str(job.get("full_description") or ""))
        entries.sort(key=lambda e: len(wanted & terms(" ".join(e["allowed_claims"]))), reverse=True)
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError("evidence limit must be a nonnegative integer or None")
        entries = entries[:limit]
    if not entries:
        return "No usable public-project claims supplied."
    lines = ["USER-CURATED PROJECT CLAIMS (not independent verification; not employer experience):"]
    for entry in entries:
        lines.append(f"{entry.get('id', '?')} | {entry.get('title', 'Project')} | {entry.get('url', '')}")
        lines.extend(f"- {claim}" for claim in entry["allowed_claims"])
    return "\n".join(lines)


def fit_requirement_context(job: Mapping[str, Any]) -> str:
    """Pass the stored fit map to generation without promoting its assertions."""
    import json

    rows = job.get("fit_requirements") or []
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except (ValueError, TypeError):
            rows = []
    if not isinstance(rows, list):
        rows = []
    clean = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("kind") not in {"defining", "mandatory", "preferred"}:
            continue
        if row.get("evidence_class") not in {"EXACT", "DIRECT", "ADJACENT", "WEAK", "NONE"}:
            continue
        clean.append({k: row[k] for k in ("requirement", "kind", "evidence_class", "source_ids") if k in row})
    if not clean:
        return "FIT MAP: unavailable. Compare the JD to source facts; do not invent a match."
    return ("FIT MAP (ranking annotations, not new candidate facts):\n"
            + json.dumps(clean, ensure_ascii=False)
            + "\nADJACENT is transferable experience, not permission to claim the requested tool. "
              "EXACT/DIRECT still require support in the supplied resume or project claims. "
              "Keep professional experience and project-only experience separate.")


def selected_arrangement_data(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Decode the persisted discovery alternative without combining alternatives."""
    selected_id = job.get("discovery_selected_arrangement")
    payload = job.get("discovery_normalized_json")
    if selected_id and payload:
        if isinstance(payload, str):
            try:
                from applypilot.discovery.repository import read_job_document
                payload = read_job_document(job, "discovery_normalized_json")
            except (ValueError, TypeError):
                return None
        if isinstance(payload, Mapping):
            rows = [row for row in payload.get("work_arrangements", [])
                    if isinstance(row, Mapping) and row.get("id") == selected_id]
            return rows[0] if len(rows) == 1 else None
        return None
    arrangement = job.get("selected_arrangement") or job.get("_selected_arrangement")
    return arrangement if isinstance(arrangement, Mapping) else None


def selected_work_country(job: Mapping[str, Any]) -> str | None:
    """Return only an explicitly selected/posted work country, never employer HQ."""
    for key in ("work_country", "selected_work_country", "country"):
        if job.get(key):
            try:
                return _country(job[key], f"job.{key}")
            except ValueError:
                pass
    arrangement = selected_arrangement_data(job)
    if isinstance(arrangement, Mapping):
        countries = arrangement.get("countries") or []
        if isinstance(countries, str):
            countries = [countries]
        resolved = []
        for value in countries:
            try:
                code = _country(value, "job.selected_arrangement.countries")
            except ValueError:
                continue
            if code:
                resolved.append(code)
        if len(set(resolved)) == 1:
            return resolved[0]
    # A job location is work-arrangement evidence. Company/headquarters fields
    # intentionally are not considered.
    location = str(job.get("location") or "").strip()
    if location:
        tokens = {part.strip().casefold() for part in re.split(r"[,/|()]", location) if part.strip()}
        matches = {code for name, code in COUNTRY_NAMES.items() if name in tokens}
        aliases = {"uae": "AE", "u.a.e.": "AE", "uk": "GB", "u.k.": "GB",
                   "usa": "US", "u.s.a.": "US"}
        lowered_location = location.casefold()
        matches.update(code for alias, code in aliases.items()
                       if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered_location))
        if len(matches) == 1:
            return next(iter(matches))
    return None


def scoped_work_authorization(profile: Mapping[str, Any], job: Mapping[str, Any]) -> dict:
    """Project tri-state answers for this job's actual work arrangement."""
    snapshot = build_applicant_snapshot(profile)
    country = selected_work_country(job)
    authorization = dict(snapshot.authorization_by_country)
    sponsorship = dict(snapshot.sponsorship_by_country)
    if country is None:
        return {"country": None, "legally_authorized": None, "needs_sponsorship": None,
                "source": "unanswered: work country is unclear",
                "policy": work_policy(profile)}
    if country in authorization or country in sponsorship:
        auth = authorization.get(country, TriState.UNKNOWN)
        sponsor = sponsorship.get(country, TriState.UNKNOWN)
        source = f"work_authorization.by_country.{country}"
    elif country == snapshot.country:
        auth = snapshot.legally_authorized_to_work
        sponsor = snapshot.require_sponsorship
        source = "legacy domestic work_authorization"
    else:
        auth = sponsor = TriState.UNKNOWN
        source = f"unanswered for {country}"
    def value(answer: TriState) -> bool | None:
        return True if answer == TriState.YES else False if answer == TriState.NO else None

    return {"country": country, "legally_authorized": value(auth),
            "needs_sponsorship": value(sponsor), "source": source,
            "policy": work_policy(profile)}


def work_policy(profile: Mapping[str, Any]) -> str:
    work = profile.get("work_authorization") or {}
    if not isinstance(work, Mapping):
        return ""
    return str(work.get("foreign_authorization_policy") or "")


def application_profile_projection(profile: Mapping[str, Any], job: Mapping[str, Any]) -> dict:
    """Detached application context without passwords or cross-country answers."""
    from copy import deepcopy

    projected = deepcopy(dict(profile))
    from applypilot.compensation import preferences
    projected['compensation'] = preferences(profile.get('compensation') or {})
    personal = projected.get("personal")
    if isinstance(personal, dict):
        personal.pop("password", None)
    projected["work_authorization"] = scoped_work_authorization(profile, job)
    by_country = (profile.get("work_authorization") or {}).get("by_country")
    if isinstance(by_country, Mapping):
        projected["work_authorization"]["by_country"] = deepcopy(dict(by_country))
    return projected

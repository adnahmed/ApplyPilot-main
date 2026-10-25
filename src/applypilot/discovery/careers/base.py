"""Public careers task plumbing. No credentials, private APIs or implicit recovery."""
from __future__ import annotations

import ipaddress
import json
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from applypilot.discovery.models import Coverage, DiscoveryPage, FailureKind, FetchFailure
from applypilot.discovery.normalization import country_code, scalar_text
from applypilot.discovery import transport


def public_url(value):
    """Validate syntax without DNS/network IO; redirects are never followed."""
    if not isinstance(value, str) or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("A public HTTPS URL is required")
    p = urlsplit(value)
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or p.fragment or "\\" in value):
        raise ValueError("A public HTTPS URL without credentials/fragment is required")
    host = p.hostname.lower()
    if "." not in host or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local career endpoints are not allowed")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Literal IP career endpoints are not allowed")
    if any(re.search(r"token|secret|password|api.?key|auth", key, re.I) for key in parse_qs(p.query)):
        raise ValueError("Credentials do not belong in a public career URL")
    return value


def tenant_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value) or value in {".", ".."}:
        raise ValueError("A valid public board tenant is required")
    return value


def request(context, url, *, json_response=True):
    """One reservation per actual HTTP attempt, including denied/failed requests."""
    public_url(url)
    transport.reserve_budget(context, "http", 1)
    if json_response:
        fetch = getattr(context, "fetch_json", transport.fetch_json_with_transport)
        payload, result = fetch(url, headers={"Accept": "application/json"}, timeout=20.0)
    else:
        fetch = getattr(context, "fetch_html", transport.fetch_with_transport)
        result = fetch(url, headers={"Accept": "text/html,application/xhtml+xml"}, timeout=20.0)
        payload = result.html if isinstance(result, transport.TransportResult) else None
    if isinstance(result, transport.TransportError):
        raise transport.TaskDeferred(result.error, result.kind, http_status=result.http_status,
                                     retry_after=result.retry_after)
    if result is not None:
        if result.url != url:
            raise PermissionError("Public careers requests must not redirect")
        if not 200 <= result.status_code < 300 or transport.access_blocked(result.html, result.url):
            error = transport._response_error(result)
            raise transport.TaskDeferred(error.error, error.kind, http_status=error.http_status,
                                         retry_after=error.retry_after)
    return payload


def task_settings(task, context, parser):
    params = transport.task_params(task, ("ats_registry", parser))
    allowed = {"parser", "provider", "ats_provider", "url", "career_url", "endpoint",
               "api_region", "tenant", "board", "token", "name", "company", "limit"}
    if set(params) - allowed:
        raise transport.TaskDeferred("Unsupported public career task parameters: " + ", ".join(sorted(set(params) - allowed)))
    if context is None:
        raise transport.TaskDeferred("A discovery context is required")
    if context.runtime.get("authorized_sources", {}).get("ats_registry") is not True:
        raise PermissionError("Internal careers require authorized_sources.ats_registry=true")
    for key in ("parser", "provider", "ats_provider"):
        if params.get(key) and params[key] != parser:
            raise ValueError("Career parser selectors disagree")
    tenant = tenant_name(task.target.scope)
    if any(params.get(key) and params[key] != tenant for key in ("tenant", "board", "token")):
        raise ValueError("Task scope and public board identifier disagree")
    urls = [params[key] for key in ("url", "career_url", "endpoint") if params.get(key)]
    if not urls:
        raise transport.TaskDeferred("An approved public career URL/endpoint is required")
    for url in urls:
        public_url(url)
    url = params.get("endpoint") or urls[0]
    region = params.get("api_region") or "global"
    if region != "global":
        raise transport.TaskDeferred("This internal public parser only supports api_region=global")
    native = context.runtime.get("native_targets", {}).get("ats_registry")
    if parser == "generic":
        if not native or any(value not in native for value in urls):
            raise PermissionError("Generic HTML requires each exact URL in native_targets.ats_registry")
    elif native is not None and not any(x in native for x in (tenant, f"{parser}:{tenant}", url)):
        raise PermissionError("Career tenant is outside native_targets.ats_registry")
    return params, tenant, url


def offset_cursor(task, cursor):
    if cursor is None:
        return {"request_key": task.target.request_key(), "offset": 0}
    state = json.loads(cursor)
    if (not isinstance(state, dict) or state.get("request_key") != task.target.request_key()
            or type(state.get("offset")) is not int or state["offset"] < 0):
        raise ValueError("Invalid or foreign career pagination cursor")
    return state


def records_page(task, context, rows, parse, *, next_cursor=None, authoritative=True, metadata=None):
    records, failures = [], []
    for index, raw in enumerate(rows):
        try:
            if not isinstance(raw, dict):
                raise ValueError("Posting must be an object")
            record = parse(raw)
            if record is None:
                continue
            if not scalar_text(record.get("title")) or not record.get("url"):
                raise ValueError("Posting requires a title and listing URL")
            public_url(record["url"])
            if record.get("application_url"):
                public_url(record["application_url"])
            records.append(record)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            failures.append(FetchFailure(FailureKind.SCHEMA, str(error), provider_field=f"jobs[{index}]"))
    partial = bool(failures)
    return DiscoveryPage(task.task_id or "", records=tuple(records), next_cursor=next_cursor,
                         exhausted=next_cursor is None and not partial, complete=not partial,
                         partial=partial, coverage=Coverage.PARTIAL if partial else Coverage.COMPLETE,
                         failures=tuple(failures), request_metadata=transport.page_metadata(
                             task, context, authoritative=authoritative, fetched=len(rows), **(metadata or {})))


def text(value):
    value = scalar_text(value)
    return BeautifulSoup(value, "html.parser").get_text("\n", strip=True) if value else None


def record(raw, *, provider, tenant, company, url, title=None, locations=None,
           description=None, posting_id=None, application_url=None, work_mode=None,
           employment_type=None, salary=None):
    """Same detached dict contract as direct boards; office != eligibility."""
    locations = locations if isinstance(locations, list) else [locations] if locations else []
    names, countries = [], []
    for location in locations:
        if isinstance(location, str):
            names.append(location)
        elif isinstance(location, dict):
            address = location.get("address") or location
            if not isinstance(address, dict):
                continue
            name = location.get("name") or ", ".join(filter(None, (
                scalar_text(address.get("addressLocality") or address.get("city")),
                scalar_text(address.get("addressRegion") or address.get("region")),
                scalar_text(address.get("addressCountry") or address.get("country")))))
            if name:
                names.append(name)
            country = address.get("addressCountry") or address.get("countryCode") or address.get("country")
            if isinstance(country, dict):
                country = country.get("identifier") or country.get("name")
            code = country_code(country)
            if code:
                countries.append(code)
    if isinstance(employment_type, dict):
        employment_type = employment_type.get("label") or employment_type.get("name")
    result = {
        "raw_record": deepcopy(raw), "title": scalar_text(title or raw.get("title") or raw.get("name")),
        "company": company, "employer_name": company, "employer_slug": tenant,
        "hiring_organization": company, "url": url, "application_url": application_url or url,
        "source_posting_id": scalar_text(posting_id),
        "source_internal_id": raw.get("internal_job_id"),
        "source_requisition_id": raw.get("requisition_id") or raw.get("refNumber"),
        "description": text(description), "full_description": text(description),
        "office_locations": list(dict.fromkeys(names)), "office_countries": list(dict.fromkeys(countries)),
        "office_location_data": deepcopy(locations), "work_mode": scalar_text(work_mode),
        "employment_type": scalar_text(employment_type), "compensation": deepcopy(salary),
        "departments": deepcopy(raw.get("department")),
        "posted_at": raw.get("datePosted") or raw.get("releasedDate") or raw.get("published_on") or raw.get("created_at"),
        "updated_at": raw.get("updated_at"),
        "identity": {"provider": provider, "tenant": tenant, "api_region": "global",
                     "posting_id": scalar_text(posting_id), "listing_url": url},
    }
    if isinstance(salary, dict):
        value = salary.get("value") if isinstance(salary.get("value"), dict) else salary
        result.update(salary_min=value.get("minValue", value.get("min", value.get("minimum"))),
                      salary_max=value.get("maxValue", value.get("max", value.get("maximum"))),
                      salary_currency=salary.get("currency"),
                      salary_period=value.get("unitText") or salary.get("period"))
    elif isinstance(salary, str):
        result["salary"] = salary
    return result


def jsonld_record(raw, *, provider, tenant, company, base_url):
    organization = raw.get("hiringOrganization")
    if isinstance(organization, dict):
        company = organization.get("name") or company
    identifier = raw.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value")
    url = urljoin(base_url, raw.get("url") or "") if raw.get("url") else None
    result = record(raw, provider=provider, tenant=tenant, company=company, url=url,
                    posting_id=identifier, locations=raw.get("jobLocation"),
                    description=raw.get("description"), employment_type=raw.get("employmentType"),
                    work_mode="remote" if raw.get("jobLocationType") == "TELECOMMUTE" else None,
                    salary=raw.get("baseSalary"))
    # JobPosting applicantLocationRequirements is eligibility, not jobLocation.
    eligible = raw.get("applicantLocationRequirements")
    if eligible:
        entries = eligible if isinstance(eligible, list) else [eligible]
        codes = [country_code(x.get("name")) if isinstance(x, dict) and x.get("@type") == "Country"
                 else None for x in entries]
        result["applicant_location_requirements"] = deepcopy(eligible)
        if all(codes):
            result.update(eligible_scope="restricted", residency_countries=list(dict.fromkeys(codes)))
    return result


@dataclass
class CareerJob:
    """Compatibility value object; to_dict retains the complete evidence contract."""
    title: str
    url: str
    location: str = ""
    department: str = ""
    description: str = ""
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str = ""
    employment_type: str = ""
    posted_at: str = ""
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        values = asdict(self)
        values.update(values.pop("evidence"))
        return values


class CareerParser:
    provider = "generic"

    def __init__(self, career_url, company_name="", *, context=None, tenant=None):
        self.career_url, self.company_name = career_url, company_name
        self.context, self.tenant = context, tenant

    def fetch_jobs(self):
        from applypilot.discovery.ats_adapter import fetch_company_jobs
        rows = fetch_company_jobs(None, self.tenant or "", self.provider, self.career_url,
                                  context=self.context, company_name=self.company_name)
        return [CareerJob(title=row["title"], url=row["url"], evidence=row) for row in rows]

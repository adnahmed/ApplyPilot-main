"""Conservative native request contracts, not career preferences or access grants.

All planned adapters must export execute_task(task, context, cursor=None) and
return one DiscoveryPage. They must not publish, reread mutable configuration,
run a full crawl, enrich with extra requests, or escalate transport implicitly.
Version labels describe available documentation, not verified v2 adapter wiring.
"""

from dataclasses import dataclass

from .countries import COUNTRY_CODES


@dataclass(frozen=True)
class Capability:
    module: str
    shape: str
    native_targets: tuple[str, ...]
    filters: tuple[str, ...] = ()
    output_only: tuple[str, ...] = ("eligibility", "pay_basis", "engagement")
    invalid_combinations: tuple[str, ...] = ()
    access: str = "public"
    resources: tuple[str, ...] = ("http",)
    min_refresh_seconds: int = 3600
    page_limit: int | None = None
    depth_limit: int | None = None
    contract_version: str = "observed website; v2 adapter unverified"
    approximate_filters: tuple[str, ...] = ()


ALL_COUNTRIES = tuple(sorted(COUNTRY_CODES))
CAPABILITIES = {
    "company_targets": Capability("company_targets", "company", ("target",), resources=("http", "model"),
                                  min_refresh_seconds=60, contract_version="owner-directed target v1"),
    "hiring_cafe": Capability("hiring_cafe", "index", ALL_COUNTRIES + ("global",),
        ("query", "country", "work_modes"), contract_version="public SSR search pages 2026-09-27"),
    "freehire": Capability("freehire", "index", ALL_COUNTRIES + ("global",),
        ("q", "countries", "regions", "cities", "work_mode", "employment_type",
         "salary_min", "salary_currency", "salary_period", "limit", "offset"),
        invalid_combinations=("countries/regions/cities are OR, not intersection",),
        page_limit=100, depth_limit=10000, contract_version="public agent/jobs/search 2026-09-05"),
    "jobspy": Capability("jobspy", "index", (), contract_version="python-jobspy 1.1.82; per-board lowering required"),
    "greenhouse": Capability("greenhouse", "board", ("tenant",), ("content",), contract_version="public Job Board API"),
    "lever": Capability("lever", "board", ("global", "eu"), ("mode",), contract_version="public Postings API"),
    "ashby": Capability("ashby", "board", ("tenant",), ("includeCompensation",), contract_version="public posting API"),
    "ats_registry": Capability("ats_adapter", "registry", ("tenant",), access="explicit"),
    "workday": Capability("workday", "employer", ("tenant",), ("searchText", "limit", "offset"), access="explicit", page_limit=20),
    "amazon": Capability("amazon", "index", ALL_COUNTRIES, ("query", "country"), access="explicit"),
    "costco": Capability("costco", "employer", ("US",), ("query", "country"), access="explicit"),
    "builtin": Capability("builtin", "index", ("US",), ("query", "country"), access="explicit"),
    "remotive": Capability("remotive", "feed", ("feed",), min_refresh_seconds=21600,
        contract_version="public feed; 24h delay; <=4 fetches/day"),
    "remoteok": Capability("remoteok", "feed", ("feed",), contract_version="public feed; attribution/link-back required"),
    "hackernews": Capability("hackernews", "signal", ("thread",), ("query",),
        resources=("http", "model"), contract_version="official item API; extraction separate"),
    "reed": Capability("reed", "index", ("GB",), ("keywords", "locationName", "resultsToTake", "resultsToSkip"),
        access="credentials", page_limit=100, contract_version="Jobseeker API"),
    "seek": Capability("seek", "index", ("AU", "NZ"), ("query", "country"), access="explicit"),
    "irishjobs": Capability("irishjobs", "index", ("IE",), ("query", "country"), access="explicit"),
    "ziprecruiter": Capability("ziprecruiter", "index", ("US", "CA"), ("query", "country"), access="explicit"),
    "linkedin_posts": Capability("linkedin_posts", "signal", ("platform",), ("query",),
        access="explicit", resources=("http", "model"), contract_version="authorized exports/local readers; bounded window"),
    "twitter": Capability("twitter", "signal", ("platform",), ("query",),
        access="explicit", resources=("http", "model"), contract_version="free authorized exports/local readers; no paid API"),
    "web_search": Capability("web_search", "signal", ("web",), ("query",),
        access="explicit", resources=("http", "model"), contract_version="DDGS or self-hosted SearXNG; bounded result window"),
    "smartextract": Capability("smartextract", "fallback", (), access="explicit", resources=("browser",)),
    "browseros_linkedin": Capability("linkedin_browser", "fallback", (), access="explicit", resources=("browser",)),
}

# Separate country sets: a supported Indeed index does not imply Glassdoor support.
INDEED_COUNTRIES = {
    "AR": "argentina", "AU": "australia", "AT": "austria", "BH": "bahrain",
    "BE": "belgium", "BR": "brazil", "CA": "canada", "CL": "chile", "CN": "china",
    "CO": "colombia", "CR": "costa rica", "CZ": "czech republic", "DK": "denmark",
    "EC": "ecuador", "EG": "egypt", "FI": "finland", "FR": "france", "DE": "germany",
    "GR": "greece", "HK": "hong kong", "HU": "hungary", "IN": "india", "ID": "indonesia",
    "IE": "ireland", "IL": "israel", "IT": "italy", "JP": "japan", "KW": "kuwait",
    "LU": "luxembourg", "MY": "malaysia", "MX": "mexico", "MA": "morocco",
    "NL": "netherlands", "NZ": "new zealand", "NG": "nigeria", "NO": "norway",
    "OM": "oman", "PK": "pakistan", "PA": "panama", "PE": "peru", "PH": "philippines",
    "PL": "poland", "PT": "portugal", "QA": "qatar", "RO": "romania", "SA": "saudi arabia",
    "SG": "singapore", "ZA": "south africa", "KR": "south korea", "ES": "spain",
    "SE": "sweden", "CH": "switzerland", "TW": "taiwan", "TH": "thailand",
    "TR": "turkey", "UA": "ukraine", "AE": "united arab emirates", "GB": "uk",
    "US": "usa", "UY": "uruguay", "VE": "venezuela", "VN": "vietnam",
}
GLASSDOOR_COUNTRIES = ("US", "CA", "GB", "IE", "AU", "DE", "FR", "NL", "IN", "SG", "NZ", "BR", "MX", "ES", "IT", "AT", "BE", "CH", "HK", "AR")
JOBSPY_BOARDS = {
    "indeed": Capability("jobspy", "index", tuple(INDEED_COUNTRIES),
        ("search_term", "location", "country_indeed", "distance", "hours_old", "results_wanted"),
        invalid_combinations=("hours_old cannot combine with is_remote/job_type or easy_apply",),
        contract_version="python-jobspy 1.1.82", approximate_filters=("distance",)),
    "glassdoor": Capability("jobspy", "index", GLASSDOOR_COUNTRIES,
        ("search_term", "location", "country_indeed", "results_wanted"), contract_version="python-jobspy 1.1.82"),
    "linkedin": Capability("jobspy", "index", ALL_COUNTRIES,
        ("search_term", "location", "hours_old", "results_wanted"), access="explicit", contract_version="python-jobspy 1.1.82; descriptions require extra budget"),
    "google": Capability("jobspy", "index", ALL_COUNTRIES + ("global",),
        ("google_search_term", "results_wanted"), contract_version="python-jobspy 1.1.82"),
    "bayt": Capability("jobspy", "index", ("query_only",), ("search_term",), contract_version="python-jobspy 1.1.82"),
    "zip_recruiter": Capability("jobspy", "index", ("US", "CA"), ("search_term", "location", "results_wanted"), contract_version="python-jobspy 1.1.82"),
    "naukri": Capability("jobspy", "probe", ("IN",), ("search_term", "results_wanted"), contract_version="python-jobspy 1.1.82"),
    "bdjobs": Capability("jobspy", "probe", ("BD",), ("search_term", "results_wanted"), contract_version="python-jobspy 1.1.82"),
}


def capability_for(target) -> Capability:
    if target.provider == "jobspy":
        return JOBSPY_BOARDS[dict(target.request_params)["site_name"]]
    return CAPABILITIES[target.provider]


def source_key(target) -> str:
    """Canonical access/health/cost identity for one native source contract."""
    if target.provider == "jobspy":
        site = dict(target.request_params).get("site_name")
        # Planner-level deferred targets may represent JobSpy as a whole when
        # no native board can serve a market. They still need a stable health
        # key and must not crash persistence before deferral is recorded.
        return f"jobspy:{site}" if site else "jobspy"
    return target.provider


def access_reason(target, runtime) -> str | None:
    cap = capability_for(target)
    key = source_key(target)
    grant = runtime["authorized_sources"].get(key)
    if grant is False:
        return "access_denied_by_runtime"
    if cap.access != "public" and grant is not True:
        return "explicit_authorization_required"
    if "paid" in cap.resources and (runtime["paid_budget"] <= 0 or runtime["paid_request_cost"] <= 0):
        return "explicit_positive_paid_budget_and_per_request_reservation_required"
    for resource in cap.resources:
        if resource == "paid":
            continue
        if runtime[f"{resource}_budget"] == 0:
            return f"{resource}_resource_disabled"
    return None


def execution_reason(target) -> str | None:
    """Static readiness only; importing a provider is never part of a plan.

    Every non-fallback capability has a bounded one-page execute_task contract.
    Fallbacks are selected only by the scheduler for one exact failed task.
    """
    if capability_for(target).shape == "fallback":
        return "exact_failed_task_required"
    return None

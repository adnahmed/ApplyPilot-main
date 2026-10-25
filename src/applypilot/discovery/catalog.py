"""Provider names and CLI aliases; no execution responsibilities."""
from applypilot.config import load_synonyms_config, load_search_config

DISCOVERY_SOURCES: dict[str, str] = {
    "jobspy":       "JobSpy aggregator (LinkedIn, Indeed, ZipRecruiter)",
    "linkedin":     "LinkedIn only (via JobSpy)",
    "browseros_linkedin": "LinkedIn via BrowserOS neo (fallback for JobSpy failures)",
    "indeed":       "Indeed only (via JobSpy)",
    "glassdoor":    "Glassdoor only (via JobSpy)",
    "zip_recruiter": "ZipRecruiter only (via JobSpy)",
    "google":       "Google Jobs only (via JobSpy)",
    "naukri":       "Naukri only (via JobSpy)",
    "bayt":         "Bayt only (via JobSpy)",
    "bdjobs":       "BDJobs only (via JobSpy)",
    "workday":      "Workday corporate career sites",
    "greenhouse":   "Greenhouse ATS public board API",
    "lever":        "Lever ATS public postings API",
    "ashby":        "Ashby ATS public posting API",
    "freehire":     "FreeHire aggregated ATS/company job discovery",
    "hiring_cafe":  "HiringCafe public job search with original employer application links",
    "amazon":       "amazon.jobs public search API",
    "costco":       "careers.costco.com public API (Issaquah HQ)",
    "builtin":      "builtin.com (config-driven cities + categories)",
    "smartextract": "Smart extract (AI-powered scraping, incl. Dice via sites.yaml)",
    "hackernews":   "Hacker News 'Who is Hiring?' thread",
    "ziprecruiter": "ZipRecruiter via FlareSolverr (Cloudflare bypass)",
    "reed":         "Reed.co.uk (UK job board, REST API)",
    "seek":         "Seek AU/NZ (Australia/New Zealand job board)",
    "irishjobs":    "IrishJobs.ie (Ireland job board, Playwright-protected)",
    "linkedin_posts": "LinkedIn Posts (authorized exports/local readers)",
    "remotive":     "Remotive.com (remote tech jobs, JSON API)",
    "remoteok":     "RemoteOK.com (remote tech jobs, JSON API)",
    "twitter":      "Twitter/X (authorized exports/free local readers)",
    "web_search": "Free web search (DDGS/self-hosted SearXNG; reviewed hiring signals)",
    "ats_registry": "ATS registry (direct company feeds via known ATS providers)",
}

_SOURCE_ALIASES: dict[str, str] = dict(
    load_synonyms_config().get("source_aliases") or {}
)

def configured_source_names(search_cfg: dict | None = None) -> list[str] | None:
    search_cfg = search_cfg if search_cfg is not None else load_search_config()
    src = search_cfg.get("sources") or {}
    if not any(k in src for k in ("enabled", "sites")) and not any(k in search_cfg for k in ("sites", "boards")):
        return None
    from applypilot.discovery.intent import compile_source_selection
    return list(compile_source_selection(search_cfg)[0])

def resolve_source_names(names: list[str]) -> list[str]:
    """Resolve source aliases and validate names. Returns canonical names."""
    resolved = []
    for name in names:
        canonical = _SOURCE_ALIASES.get(name, name)
        if canonical not in DISCOVERY_SOURCES:
            valid = sorted(set(list(DISCOVERY_SOURCES.keys()) + list(_SOURCE_ALIASES.keys())))
            raise ValueError(
                f"Unknown discovery source: '{name}'. "
                f"Valid sources: {', '.join(valid)}"
            )
        if canonical not in resolved:
            resolved.append(canonical)
    return resolved


def request_source(provider, *, context=None, workers=1):
    """Deprecated batch spelling: submit a native request, do not run a scheduler.

    An arbitrary in-memory context cannot silently become the saved workspace
    policy. Call execute_task with an explicit task for finite adapter use.
    """
    if context is not None:
        raise ValueError('Custom-context batch execution is retired. Use the finite execute_task adapter, '
                         'or save the workspace policy and submit a Temporal discovery request.')
    if workers != 1:
        raise ValueError('Configure Activity concurrency on the Temporal worker deployment')
    import asyncio
    from applypilot.orchestration.client import run_work
    return asyncio.run(run_work('discover', {'sources': [provider]}))

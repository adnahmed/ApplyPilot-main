"""Detail page enrichment: scrapes full descriptions and apply URLs.

For each job URL in the database, navigates to the detail page and extracts:
  - full_description: the complete job posting text
  - application_url: the "Apply" button/link URL

Three-tier extraction cascade (cheapest first):
  Tier 1: JSON-LD JobPosting structured data (0 tokens)
  Tier 2: Deterministic CSS pattern matching (0 tokens)
  Tier 3: LLM-assisted extraction (single call, or opt-in bounded RLM)
"""

from applypilot import operations as ops

import json
from html import escape
import logging
import re
from applypilot import postgres
import time
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from applypilot.backlog import MAX_ENRICH_RETRIES
from applypilot.database import commit_with_retry, init_db, transition_state
from applypilot.llm import LLMTask, get_client
from applypilot.reasoning.prompt_templates import instruction_message

log = logging.getLogger(__name__)

def _get_ua() -> str:
    """Build a realistic UA from the actual installed Chrome version."""
    from applypilot.apply.chrome import _get_real_user_agent
    return _get_real_user_agent()


# Version detection is lazy: importing the enrichment module is not a reason
# to inspect or launch a browser. The version helper caches actual reads.

# Stealth JS patches — hides automation signals from bot detectors
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

Object.defineProperty(navigator, 'plugins', {
  get: () => [
    {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
    {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
    {name: 'Native Client', filename: 'internal-nacl-plugin'},
  ],
});

Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});

window.chrome = window.chrome || {};
window.chrome.runtime = window.chrome.runtime || {};

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) =>
  parameters.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : originalQuery(parameters);
"""

# Sites that block scraping -- skip detail extraction entirely
SKIP_DETAIL_SITES = {"glassdoor", "google", "Workopolis"}

# Module-level proxy config (set from CLI or caller)
_PROXY_CONFIG: dict | None = None


def set_proxy(proxy_str: str | None):
    """Set proxy config from an external caller."""
    global _PROXY_CONFIG
    if proxy_str:
        from applypilot.discovery.jobspy import parse_proxy
        _PROXY_CONFIG = parse_proxy(proxy_str)


# -- URL resolution ----------------------------------------------------------

def _load_base_urls() -> dict[str, str | None]:
    """Load site base URLs from config/sites.yaml."""
    from applypilot.config import load_base_urls
    return load_base_urls()


def resolve_url(raw_url: str, site: str) -> str | None:
    """Resolve a stored URL to an absolute URL."""
    if not raw_url:
        return None

    if raw_url.startswith("http://") or raw_url.startswith("https://"):
        return raw_url

    if site == "WelcomeToTheJungle":
        return None

    if site == "Randstad Canada" and "/" not in raw_url:
        return f"https://www.randstad.ca/jobs/search/{raw_url}"

    if site == "4DayWeek" and raw_url in ("/", "/jobs"):
        return None

    base = _load_base_urls().get(site)
    if not base:
        return None

    if ";jsessionid=" in raw_url:
        raw_url = raw_url.split(";jsessionid=")[0]

    return urljoin(base, raw_url)


# -- Detail page intelligence ------------------------------------------------

def collect_detail_intelligence(page) -> dict:
    """Collect signals from a detail page. Lighter than discovery -- no API interception."""
    intel: dict = {"json_ld": [], "page_title": "", "final_url": ""}

    intel["page_title"] = page.title()
    intel["final_url"] = page.url

    for el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(el.text_content() or '')
            intel["json_ld"].append(data)
        except Exception:
            pass

    return intel


# -- Tier 1: JSON-LD extraction -----------------------------------------------

def extract_from_json_ld(intel: dict) -> dict | None:
    """Extract description and apply URL from JSON-LD JobPosting.
    Returns {"full_description": str, "application_url": str|None} or None."""

    def find_job_posting(data):
        if isinstance(data, dict):
            if data.get("@type") == "JobPosting":
                return data
            if "@graph" in data and isinstance(data["@graph"], list):
                for item in data["@graph"]:
                    result = find_job_posting(item)
                    if result:
                        return result
        elif isinstance(data, list):
            for item in data:
                result = find_job_posting(item)
                if result:
                    return result
        return None

    for ld in intel.get("json_ld", []):
        posting = find_job_posting(ld)
        if not posting:
            continue

        desc = posting.get("description", "")
        if not desc:
            continue

        desc_clean = clean_description(desc)
        if len(desc_clean) < 50:
            continue

        apply_url = None
        if posting.get("directApply"):
            apply_url = posting.get("url")
        if not apply_url:
            contact = posting.get("applicationContact")
            if isinstance(contact, dict):
                apply_url = contact.get("url")
        if not apply_url:
            apply_url = posting.get("url")

        return {
            "full_description": desc_clean,
            "application_url": apply_url,
        }

    return None


# -- Tier 2: Deterministic pattern matching ----------------------------------

APPLY_SELECTORS = [
    'a[href*="apply"]',
    'a[data-testid*="apply"]',
    'a[class*="apply"]',
    'a[aria-label*="pply"]',
    'button[data-testid*="apply"]',
    'a#apply_button',
    '.postings-btn-wrapper a',
    'a.ashby-job-posting-apply-button',
    '#grnhse_app a[href*="apply"]',
    'a[data-qa="btn-apply"]',
    'a[class*="btn-apply"]',
    'a[class*="apply-btn"]',
    'a[class*="apply-button"]',
]

DESCRIPTION_SELECTORS = [
    'div.show-more-less-html__markup',  # LinkedIn's public job description, not the surrounding core rail.
    '#job-description',
    '#job_description',
    '#jobDescriptionText',
    '.job-description',
    '.job_description',
    '[class*="job-description"]',
    '[class*="jobDescription"]',
    '[data-testid*="description"]',
    '[data-testid="job-description"]',
    '.posting-page .posting-categories + div',
    '#content .posting-page',
    '#app_body .content',
    '#grnhse_app .content',
    '.ashby-job-posting-description',
    '[class*="posting-description"]',
    '[class*="job-detail"]',
    '[class*="jobDetail"]',
    '[class*="job-content"]',
    '[class*="job-body"]',
    '[role="main"] article',
    'main article',
    'article[class*="job"]',
    '.job-posting-content',
]


def extract_apply_url_deterministic(page) -> str | None:
    """Try known CSS patterns for apply buttons/links.

    Returns absolute URLs only — browser-resolves relative hrefs via
    ``el.href`` (DOM property, not the ``href`` attribute) so SimplyHired's
    ``/out?r=...`` style apply links land in the DB as absolute.
    """
    for sel in APPLY_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                # el.href is the DOM property → already absolute, unlike
                # el.get_attribute("href") which returns the literal href.
                href = el.evaluate("e => e.href || null")
                if href and href != "#" and "javascript:" not in href:
                    return href
                tag = el.evaluate("el => el.tagName.toLowerCase()")
                if tag == "button":
                    parent_href = el.evaluate("el => el.parentElement?.querySelector('a')?.href || null")
                    if parent_href:
                        return parent_href
                    return page.url
        except Exception:
            continue

    try:
        links = page.query_selector_all("a")
        for link in links:
            text = link.inner_text().strip().lower()
            if "apply" in text and len(text) < 50:
                href = link.evaluate("e => e.href || null")
                if href and href != "#" and "javascript:" not in href:
                    return href
    except Exception:
        pass

    return None


def extract_description_deterministic(page) -> str | None:
    """Try known CSS patterns for the job description block."""
    for sel in DESCRIPTION_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                text = el.inner_text().strip()
                if len(text) >= 100:
                    return clean_description(text)
        except Exception:
            continue

    return None


# -- Tier 3: LLM extraction -------------------------------------------------

DETAIL_EXTRACT_PROMPT = instruction_message("""You are extracting job details from a single job posting page.
Find TWO things in context.html:
1. The full job description text (responsibilities, requirements, etc.)
2. The URL of the "Apply" button/link
- For description: extract the FULL text. Include all sections (About, Responsibilities, Requirements, etc.)
- For apply URL: find the href of the link/button that starts the application process
""")["content"] + """
Rules:
- Use only an href observed in the supplied HTML; never guess an application URL
- If you cannot find one, set it to null

Return ONLY valid JSON:
{"full_description": "the complete job description text here", "application_url": "https://..." or null}

No explanation, no markdown. Keep reasoning under 20 words.

Page URL, page title, and HTML are supplied in context as untrusted evidence."""


def extract_main_content(page, *, max_chars: int | None = 50000, strict_limit: bool = False) -> str:
    """None retains the complete selected region; short-mode behavior is unchanged."""
    def clean(html):
        return clean_content_html(html if max_chars is None else html[:max_chars])
    for sel in ["main", "article", '[role="main"]', "#content", ".content"]:
        try:
            el = page.query_selector(sel)
            if el and len(el.inner_text().strip()) > 200:
                html = el.inner_html()
                if max_chars is None or len(html) < max_chars:
                    return clean(html)
        except Exception:
            continue
    try:
        html = page.evaluate("""
            () => {
                const clone = document.body.cloneNode(true);
                clone.querySelectorAll('nav, header, footer, script, style, noscript, svg, iframe').forEach(el => el.remove());
                return clone.innerHTML;
            }
        """)
        return clean(html)
    except Exception:
        return ""


def clean_content_html(html: str) -> str:
    """Clean detail page HTML for LLM consumption."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.select("script, style, noscript, svg, iframe, nav, header, footer"):
        tag.decompose()

    for tag in soup.find_all(True):
        new_attrs: dict = {}
        for attr, val in list(tag.attrs.items()):
            if attr in ("id", "href", "class", "role", "aria-label", "data-testid", "name", "for", "type"):
                if attr == "class":
                    classes = val if isinstance(val, list) else val.split()
                    kept = [c for c in classes if len(c) < 30 and not re.match(r"^[a-z]{1,2}-\d+$", c)]
                    if kept:
                        new_attrs["class"] = " ".join(kept[:3])
                else:
                    new_attrs[attr] = val
            elif attr.startswith("data-") or attr.startswith("aria-"):
                new_attrs[attr] = val
        tag.attrs = new_attrs

    return str(soup)


def extract_with_llm(page, url: str, *, content: str | None = None, title: str | None = None) -> dict:
    """Send focused HTML to LLM for extraction. Fallback tier."""
    from applypilot.enrichment.browser import check_running
    check_running()
    try:
        from applypilot.reasoning.settings import rlm_enabled
        enabled = rlm_enabled()
        if content is None:
            content = extract_main_content(page, max_chars=None if enabled else 50000)
        if not content:
            return {"full_description": None, "application_url": None, "extraction_error": "extraction_empty_page"}

        if title is None:
            title = ""
            try:
                title = page.title()
            except Exception:
                pass

        from applypilot.reasoning import workflow_json
        from applypilot.reasoning.rlm_runtime import should_use_rlm
        from applypilot.reasoning.schemas import DETAIL_SCHEMA
        context = {"page_url": url, "page_title": title, "html": content}
        recursive = should_use_rlm(context)
        t0 = time.time()
        result = workflow_json("detail_extract",
            DETAIL_EXTRACT_PROMPT,
            context, schema=DETAIL_SCHEMA, client=get_client(LLMTask.LONG_CONTEXT), max_tokens=4096)
        elapsed = time.time() - t0
        log.info("LLM extraction engine=%s chars=%d elapsed=%.1fs",
                 "rlm" if recursive else "plain", len(content), elapsed)

        if not isinstance(result, dict):
            raise ValueError("model_schema_error: job details must be a JSON object")
        if any(result.get(key) is not None and not isinstance(result[key], str)
               for key in ("full_description", "application_url")):
            raise ValueError("model_schema_error: description and application URL must be strings or null")
        desc = result.get("full_description")
        apply_url = result.get("application_url")

        if desc:
            desc = clean_description(desc)

        # URL grounding is field-local: rejecting a guessed or malformed link
        # must not erase a description that was successfully extracted.
        extraction_error = None
        if apply_url and apply_url.strip():
            def resolve_href(href):
                if not isinstance(href, str) or not href.strip():
                    return None
                try:
                    resolved = urljoin(url, href.strip())
                    parsed = urlparse(resolved)
                    return resolved if parsed.scheme in {"http", "https"} and parsed.hostname else None
                except (AttributeError, TypeError, ValueError):
                    return None

            observed = {resolve_href(tag["href"]) for tag in BeautifulSoup(content, "html.parser").find_all(href=True)}
            resolved = resolve_href(apply_url)
            if resolved is None or resolved not in observed:
                apply_url = None
                extraction_error = "application_url_unobserved"
            else:
                apply_url = resolved
        else:
            apply_url = None

        from applypilot.operations.worker_journal import current as worker_checkpoint
        worker_checkpoint("enrichment.model_fields",description_chars=len(desc or ""),has_application_url=bool(apply_url),valid_json=True,
                          reason=extraction_error or ("description_and_route" if desc and apply_url else "description_without_route" if desc else
                          "route_without_description" if apply_url else "no_job_fields"))
        fields = {"full_description": desc, "application_url": apply_url}
        if extraction_error:
            fields["extraction_error"] = extraction_error
        return fields
    except Exception as e:
        if getattr(e, 'error_kind', None) == 'cancelled':
            from applypilot.execution import ExecutionStopped
            raise ExecutionStopped('Enrichment inference stopped by its owner') from e
        ops.capture(e, component_name="enrich", operation="extract_model_fields", stage="enrich")
        log.error("LLM extraction failed: %s", type(e).__name__)
        hint = str(e).lower()
        code = ("model_invalid_json" if "model_invalid_json" in hint else
                "model_schema_error" if "model_schema_error" in hint else
                "model_output_incomplete" if "incomplete_output" in hint or "model_empty_response" in hint else "extraction_provider_failure")
        from applypilot.operations.worker_journal import current as worker_checkpoint
        worker_checkpoint("enrichment.model_fields",reason=code,exception_type=type(e).__name__)
        return {"full_description": None, "application_url": None, "extraction_error": code}


# -- Description cleaning ---------------------------------------------------

def clean_description(text: str) -> str:
    """Convert HTML description to clean readable text."""
    if not text:
        return ""

    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "li", "tr"]):
            tag.insert_before("\n")
            tag.insert_after("\n")
        for li in soup.find_all("li"):
            li.insert_before("- ")
        text = soup.get_text()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            lines.append(line)

    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# -- Orchestration -----------------------------------------------------------

SITE_DELAYS = {
    "RemoteOK": 3.0,
    "WelcomeToTheJungle": 2.0,
    "Job Bank Canada": 1.5,
    "CareerJet Canada": 3.0,
    "Hacker News Jobs": 1.0,
    "BuiltIn Remote": 2.0,
}

RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
PERMANENT_FAILURES = {404, 410, 451}

# Error strings that indicate the job posting is still live but we hit a network/LLM issue.
_RETRIABLE_PATTERNS = (
    "timeout", "LLM error", "Client error",
    "HTTP 408", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "ERR_CONNECTION", "ERR_NAME_NOT_RESOLVED", "ERR_INTERNET_DISCONNECTED",
    "net::ERR",
)

# Error strings that mean the posting is definitively gone.
_EXPIRED_PATTERNS = ("HTTP 404", "HTTP 410", "HTTP 451")

# Errors that can never succeed regardless of retries.
_PERMANENT_PATTERNS = (
    "unresolved listing URL",
    "manual://",  # Synthetic HN contact-only URLs — not real web pages
)


def classify_detail_error(error: str) -> str:
    """Classify evidence, without scheduling another attempt."""
    if any(pattern in error for pattern in _EXPIRED_PATTERNS):
        return "expired"
    if any(pattern in error for pattern in _PERMANENT_PATTERNS):
        return "permanent"
    return "transient"


def _mark_enrich_result(conn, url: str, *, status: str,
                        full_description: str | None, application_url: str | None,
                        error: str | None = None, tier: int | None = None,
                        now: str | None = None, context=None) -> None:
    """Persist a single extraction. Attempt counts are evidence, not eligibility."""
    if status not in {"ok", "partial", "error"}:
        raise ValueError("Invalid enrichment observation")
    now = now or datetime.now(timezone.utc).isoformat()
    if not conn.in_transaction:
        conn.execute("BEGIN")
    row = conn.execute("SELECT * FROM jobs WHERE url=? FOR UPDATE", (url,)).fetchone()
    if row is None:
        raise LookupError("Job not found")
    protected = bool(row["applied_at"] or row["apply_status"] not in (None, "failed"))
    if status in {"ok", "partial"}:
        from applypilot.discovery.url_normalize import canonicalize_application_url
        target = urljoin(url, application_url) if application_url else row["application_url"]
        target = canonicalize_application_url(target) if target else None
        conn.execute("UPDATE jobs SET full_description=COALESCE(?,full_description), "
                     "application_url=COALESCE(?,application_url), detail_scraped_at=?, "
                     "detail_error=?, detail_error_category=?, "
                     "enrich_attempts=COALESCE(enrich_attempts,0)+1 WHERE url=?",
                     (full_description or None, target, now, error,
                      "partial" if status == "partial" else None, url))
        if context is not None:
            from applypilot.discovery.repository import reassess_job
            reassess_job(context, conn, url, source_verified=bool(str(full_description or "").strip()))
        if not protected:
            transition_state(conn, url, "enriched", reason="description fetched",
                             metadata={"chars": len(full_description or ""), "tier": tier})
            from applypilot.apply.routes import infer_and_persist_route
            infer_and_persist_route(conn, job_url=url, application_url=target,
                                    source=row["site"], evidence="Observed during enrichment")
    else:
        category = classify_detail_error(error or "unknown")
        conn.execute("UPDATE jobs SET detail_error=?, detail_error_category=?, "
                     "enrich_attempts=COALESCE(enrich_attempts,0)+1 WHERE url=?",
                     (error or "unknown", category, url))
        if category == "expired":
            conn.execute("UPDATE jobs SET discovery_source_status='closed', "
                         "discovery_assessed_content_hash=NULL, discovery_prepare_allowed=0, "
                         "discovery_apply_allowed=0 WHERE url=?", (url,))
            if context is not None:
                from applypilot.discovery.repository import reassess_job
                reassess_job(context, conn, url, source_status="closed")
        if category in {"expired", "permanent"} and not protected:
            transition_state(conn, url, "enrich_failed", reason=category,
                             metadata={"error": error})
        # retriable: no transition — job stays in 'discovered' for retry loop


def _learn_greenhouse_slug_from_iframe(page, parent_url: str) -> None:
    """If the rendered page embeds a Greenhouse iframe, register the slug.

    Called once per enriched page so aggregator-discovered URLs (LinkedIn,
    Indeed, BuiltIn) on unknown employer hosts gradually teach the
    canonicalizer the right slug for that host. After registration, the
    next encounter rewrites without an HTML fetch.
    """
    if not parent_url or not parent_url.startswith(("http://", "https://")):
        return
    parent_host = urlparse(parent_url).netloc.lower()
    if parent_host.startswith("www."):
        parent_host = parent_host[4:]
    if not parent_host or parent_host.endswith("greenhouse.io"):
        return
    try:
        from applypilot.discovery.url_normalize import (
            lookup_slug, parse_greenhouse_slug_from_iframe_src,
            register_runtime_slug,
        )
        if lookup_slug(parent_host):
            return  # already known
        srcs = page.evaluate(
            "() => Array.from(document.querySelectorAll('iframe'))"
            ".map(f => f.src).filter(s => s && s.includes('greenhouse.io'))"
        )
        for src in (srcs or []):
            slug = parse_greenhouse_slug_from_iframe_src(src)
            if slug:
                register_runtime_slug(parent_host, slug)
                return
    except Exception:
        # Iframe sniffing is best-effort; don't fail enrichment over it.
        log.debug("greenhouse iframe slug sniff failed for %s",
                  parent_url[:80], exc_info=True)


def _scrape_browser_detail(page, url: str) -> dict:
    """Full cascade for one detail page."""
    result: dict = {
        "full_description": None,
        "application_url": None,
        "status": "error",
        "tier_used": None,
        "error": None,
    }
    t0 = time.time()

    try:
        with ops.worker_phase("navigating"):
            resp = page.goto(url, timeout=30000, wait_until="domcontentloaded")
        from applypilot.browser.cloudflare import is_browser_challenge
        if (not resp or resp.status in {200, 403, 503}) and is_browser_challenge(page.content()):
            result.update(error="Browser challenge observed", challenge_url=page.url,
                          response_url=page.url, elapsed=time.time() - t0)
            return result
        if resp and resp.status in (PERMANENT_FAILURES | RETRYABLE_STATUSES | {401, 403}):
            result["error"] = f"HTTP {resp.status}"
            result["response_url"] = page.url
            result["elapsed"] = time.time() - t0
            return result
        # A site-specific useful-content check beats network-idle on pages
        # with analytics, streaming connections and long-lived ad requests.
        try:
            page.wait_for_function("""() => Boolean(
                document.querySelector('script[type="application/ld+json"]') ||
                (document.querySelector('main, article, [role="main"]')?.innerText || '').trim().length > 160
            )""", timeout=5000)
        except Exception:
            pass  # Existing extractors determine whether the page has usable data.
    except Exception as e:
        ops.capture(e, component_name="enrich", operation="page_navigation", stage="enrich")
        err_str = str(e)
        if "timeout" in err_str.lower():
            result["error"] = "timeout"
        else:
            result["error"] = err_str[:200]
        result["elapsed"] = time.time() - t0
        return result

    # Best-effort: learn the greenhouse slug for this employer host so
    # canonicalize_application_url can rewrite ?gh_jid=N URLs the next
    # time discovery encounters this host.
    _learn_greenhouse_slug_from_iframe(page, url)

    intel = collect_detail_intelligence(page)
    result['structured_data'] = intel.get('json_ld', [])
    result['source_url'] = page.url

    # Tier 1: JSON-LD
    json_ld_result = extract_from_json_ld(intel)
    if json_ld_result and json_ld_result.get("full_description"):
        result.update(json_ld_result)
        result["tier_used"] = 1
        if not result.get("application_url"):
            apply = extract_apply_url_deterministic(page)
            if apply:
                result["application_url"] = apply
        result["status"] = "ok" if result.get("application_url") else "partial"
        if result["status"] == "partial":
            result["reason_code"] = "missing_application_url"
            result["message"] = "Job description extracted; no application link observed"
        result["elapsed"] = time.time() - t0
        return result

    # Tier 2: Deterministic CSS
    desc = extract_description_deterministic(page)
    apply = extract_apply_url_deterministic(page)

    if desc:
        result["full_description"] = desc
        result["application_url"] = apply
        result["tier_used"] = 2
        result["status"] = "ok" if apply else "partial"
        if result["status"] == "partial":
            result["reason_code"] = "missing_application_url"
            result["message"] = "Job description extracted; no application link observed"
        result["elapsed"] = time.time() - t0
        return result

    tier2_apply = apply

    # Tier 3: LLM
    llm_result = extract_with_llm(page, page.url)
    result["full_description"] = llm_result.get("full_description")
    result["application_url"] = llm_result.get("application_url") or tier2_apply
    result["tier_used"] = 3

    if result.get("full_description"):
        result["status"] = "ok" if result.get("application_url") else "partial"
        if result["status"] == "partial":
            result["reason_code"] = llm_result.get("extraction_error") or "missing_application_url"
            result["message"] = ("Job description extracted; unobserved model application link discarded"
                                 if result["reason_code"] == "application_url_unobserved" else
                                 "Job description extracted; no application link observed")
    elif result.get("application_url"):
        result["status"] = "partial"
        result["reason_code"] = "missing_description"
        result["message"] = "Application link observed; no job description extracted"
    else:
        result["status"] = "error"
        result["error"] = llm_result.get("extraction_error") or "extraction_empty: no data extracted"

    result["elapsed"] = time.time() - t0
    return result


@ops.observe('enrich', 'job', job_arg='url', stage='enrich')
def scrape_detail_page(page, url: str, *, application_url: str | None = None,
                       navigation_gate=None, delay: float = 2.0) -> dict:
    """Recover public reads with HTTP, owned BrowserOS and observed apply targets."""
    from applypilot.execution import ExecutionStopped
    from applypilot.integrations.crawl import CrawlService
    from applypilot.integrations.net import public_url
    from applypilot.integrations.types import SourceError
    from applypilot.enrichment.browser import check_running

    started = time.time()
    best = {"status": "error", "full_description": None, "application_url": application_url,
            "error": "unresolved listing URL"}
    targets = list(dict.fromkeys(value for value in (url, application_url) if value))
    visited = set()
    blocked_hosts = set()
    for target in targets:
        check_running()
        try:
            target = public_url(target)
        except SourceError as exc:
            if exc.kind == "network":
                best["error"] = "extraction_provider_failure: " + str(exc)
            continue
        host = urlparse(target).hostname
        if target in visited or host in blocked_hosts:
            continue
        visited.add(target)
        if navigation_gate is not None:
            navigation_gate.wait(target, delay)
        check_running()
        try:
            with (_detail_page() if page is None else nullcontext(page)) as active_page:
                candidate = _scrape_browser_detail(active_page, target)
        except ExecutionStopped:
            raise
        except Exception as exc:
            ops.capture(exc, component_name="enrich", operation="browser_fallback", severity="warning")
            candidate = {"status": "error", "error": "extraction_provider_failure: " + type(exc).__name__}
        best.update({key: value for key, value in candidate.items() if value is not None})
        if candidate.get("application_url") and candidate["application_url"] not in targets:
            targets.append(candidate["application_url"])
        if best.get("full_description") and best.get("application_url"):
            break
        # Stop reading a restricted source, but still try an observed employer
        # application target. A closed listing likewise says nothing about it.
        if candidate.get("error") in {"HTTP 401", "HTTP 403", "HTTP 429", "HTTP 451"}:
            blocked_hosts.update((host, urlparse(candidate.get("response_url") or target).hostname))
            continue
        if candidate.get("error") in {"HTTP 404", "HTTP 410"}:
            continue
        readers = ["flaresolverr"] if candidate.get("challenge_url") else ["http", "browseros"]
        for reader in readers:
            check_running()
            try:
                document = CrawlService(reader, allow_browseros=reader == "browseros").read(target, fresh=True)
                fields = extract_from_json_ld({"json_ld": list(document.structured_data)})
                tier = 1
                if not fields:
                    content = "<main><pre>" + escape(document.markdown) + "</pre>" + "".join(
                        '<a href="' + escape(link, quote=True) + '">' + escape(link) + "</a>"
                        for link in document.links) + "</main>"
                    fields = extract_with_llm(None, document.url, content=content, title=document.title)
                    tier = 3
                if fields.get("application_url"):
                    best["application_url"] = urljoin(document.url, fields["application_url"])
                    if best["application_url"] not in targets:
                        targets.append(best["application_url"])
                if fields.get("full_description"):
                    best.update(full_description=fields["full_description"], tier_used=tier,
                                source_url=document.url, structured_data=list(document.structured_data), error=None)
                if best.get("full_description"):
                    break
            except SourceError as exc:
                ops.capture(exc, component_name="enrich", operation=reader + "_fallback", severity="warning")
                if getattr(exc, "challenge_url", None):
                    if reader == "flaresolverr":
                        raise
                    if "flaresolverr" not in readers:
                        readers.insert(readers.index(reader) + 1, "flaresolverr")
                    continue
                if exc.kind in {"auth", "access_denied", "rate_limit", "policy"}:
                    blocked_hosts.add(host)
                    break
        if best.get("full_description"):
            break
    best["application_url"] = best.get("application_url") or application_url
    if best.get("full_description"):
        best["status"] = "ok" if best.get("application_url") else "partial"
        best["error"] = None
        if best["status"] == "ok":
            best.pop("reason_code", None)
            best.pop("message", None)
        else:
            best["reason_code"] = "missing_application_url"
            best["message"] = "Job description extracted; no application link observed"
    best["elapsed"] = time.time() - started
    return best


@contextmanager
def _detail_page():
    """Keep browser ownership and bounded cleanup within this worker's lease."""
    from applypilot.execution import browser_operation
    from applypilot.enrichment.browser import owned_page

    with browser_operation():
        options = {"headless": True, "chromium_sandbox": True}
        if _PROXY_CONFIG:
            options["proxy"] = _PROXY_CONFIG["playwright"]
        with owned_page(options=options, user_agent=_get_ua(), init_script=_STEALTH_INIT_SCRIPT) as page:
            yield page






# -- Public entry point ------------------------------------------------------


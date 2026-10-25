"""Audited source disposition; upstream implementations are not vendored."""
SOURCES = (
    ("peopledatalabs/peopledatalabs-python", "excluded", "Hosted people/company API; replaced by evidence research"),
    ("unclecode/crawl4ai", "retired", "SQLite-backed internal caches; replaced with stateless Playwright and the PostgreSQL source store"),
    ("microsoft/playwright-python", "isolated SDK", "Stateless public-page rendering; host PostgreSQL robots/pacing/content cache"),
    ("exa-labs/exa-py", "excluded", "Hosted search API; replaced by DDGS/SearXNG"),
    ("d60/twikit", "isolated SDK", "Authorized session: X search, profile, timeline"),
    ("joeyism/linkedin_scraper", "isolated SDK", "v3 Playwright: profile/company/jobs/company posts"),
    ("olgasafonova/linkedin-rs", "external CLI", "Read-only CLI bridge; license review required before redistributing"),
    ("moaljumaa/linki", "native alternative", "No source copied: existing referral sequencing/approval/outbox"),
    ("unipile/unipile-python", "excluded", "Hosted messaging platform SDK; no paid data/messaging fallback"),
    ("eracle/OpenOutreach", "native alternative", "Company/contact research, advisory fit and reviewed drafts; no email credits"),
    ("firecrawl/fire-enrich", "native alternative", "Evidence-backed company/funding/tech/custom-field research; no Firecrawl API"),
    ("stickerdaniel/linkedin-mcp-server", "local MCP", "Fixed read allowlist, raw export and identity-checked discovery"),
    ("nirholas/XActions", "local MCP", "Forced local mode; X profile/search/timeline/network reads"),
    ("ihuzaifashoukat/x-use", "local MCP", "Explicit account, X search/timeline/post; no writes or queue drain"),
    ("deedy5/ddgs", "isolated SDK", "Explicit DuckDuckGo search/news behind common cache"),
    ("searxng/searxng", "self-hosted service", "JSON metasearch; operator-owned instance, no paid engines configured"),
)

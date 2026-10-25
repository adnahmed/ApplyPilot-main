# Jobseek career-board integration

ApplyPilot imports the original `company_slug`, `board_slug`, `board_url`,
`monitor_type`, `monitor_config`, `scraper_type`, and `scraper_config` from
[Jobseek's board catalog](https://github.com/colophon-group/jobseek/blob/main/apps/crawler/data/boards.csv).
The structured catalog record is joined to verified company evidence using its
website or advertised board URL. Model output cannot rewrite crawler settings.
Every distinct board is retained in `company_target_boards` and participates in
the existing company-target discovery schedule. Configuration changes invalidate
the old checkpoint and target revision.

## Runtime installation

From the ApplyPilot checkout:

```powershell
python scripts/jobseek/install.py
```

This installs the pinned MIT-licensed upstream crawler commit
`e65fb972e4fbb349d10ed66dc9418b1d271f52ca`, its dependencies, and Chromium under
`APPLYPILOT_DIR/tools/jobseek`. Python 3.13 is isolated from ApplyPilot's Python
3.12 environment. `uv` and `git` must be installed. The upstream license and
installation provenance are retained alongside the environment.
The installer records the actual monitor and scraper registry counts. The
Company signals view reads this manifest to show installation status and
available monitor/scraper counts; opening the page does not launch Python or a browser.

The integration calls upstream's pure
[`monitor_one`](https://github.com/colophon-group/jobseek/blob/e65fb972e4fbb349d10ed66dc9418b1d271f52ca/apps/crawler/src/core/monitor.py)
and
[`scrape_one`](https://github.com/colophon-group/jobseek/blob/e65fb972e4fbb349d10ed66dc9418b1d271f52ca/apps/crawler/src/core/scrape.py)
functions, including its monitor/scraper registries and automatic scraper
selection. No separate Jobseek Postgres, Redis, Typesense, or S3 services are
needed. Native ATS APIs, XML feeds, recursive sitemaps, configured DOM selectors,
rendered pages, JSON-LD, and PDF extraction use the maintained upstream
implementations. OCR additionally needs the Tesseract executable and requested
language packs if the original board configuration enables it.

## Scheduling and evidence

Board execution reserves a bounded request allowance before starting the child
process. Each response has a 20 MB transfer limit, a run has a four-minute
deadline, and extracted details are processed in batches. The publication hook
saves a per-board lexical URL checkpoint only when the matching configuration
still exists. Partial inventories are revisited after five minutes. Failed
boards retain their diagnostics while successful sibling boards publish jobs
through ApplyPilot's normal matching, enrichment, and admission pipeline.

Descriptions, employer locations, salary, dates, source identity, and original
application URLs are carried into ordinary discovery records. Partial results
never establish authoritative closure of missing vacancies. The catalog itself
remains company evidence, not proof of a currently advertised job.

## Runtime boundaries and recovery

The child uses an empty working directory and a minimal environment so it cannot
load ApplyPilot's `.env` or model credentials. Native provider deny lists and
tenant restrictions remain enforced. HTTP destinations, redirects, and rendered
page requests must resolve to public addresses. Browser processes are temporary;
the existing execution cancellation mechanism owns their lifetime.

Published configurations are data. Executable browser actions, injected script
settings, and disabled TLS verification are rejected with a visible diagnostic.
Upstream configurations requiring a proxy cannot silently change to direct
egress; without an independently provisioned compatible proxy they report their
configuration error. Unknown monitor or scraper types require a reviewed
upstream revision update. Missing runtimes explicitly name the installer.

This is a runtime integration with the pinned crawler, not a claim that every
remote employer is currently reachable. Provider blocks, configuration drift,
PDFs without extractable text, and unavailable OCR dependencies remain visible
in the target's careers observations and retry schedule.

# Company discovery and watchlists

Open **Targets & sources → Company signals** to manage company discovery, inspect evidence, follow a company, or add a directory/feed. Existing job-source preferences stay under **Search preferences**.

Company discovery runs as a bounded support task alongside job discovery. It records companies and their supporting signals separately from vacancies. A funding announcement, accelerator membership, or portfolio entry cannot create a job or authorize a message.

The shipped source registry includes public accelerator and investor directories, UAE/Saudi/Pakistan ecosystem sources, funding news, Product Hunt, and GitHub Trending. Jobseek's published company and board CSVs and Startup Portfolios' current-year dataset provide additional bootstrap candidates. Those catalogs are secondary evidence; important company details are checked against the company's own website before automatic watching.

## Evidence and selection

Each signal retains its source URL, retrieval time, published event date when available, supporting excerpt, and score reasons. Unknown funding dates remain unknown. An SEC fundraising filing or a portfolio membership is not treated as a completed new funding round.

Company fit uses the current target roles and markets, observed engineering/product activity, remote evidence, career pages, and actual matching vacancies. Inactivity and incompatible employment restrictions reduce fit. Dated funding/cohort events receive a recency bonus only within 30 days. The job pipeline still applies its own eligibility, employer suitability, and risk checks.

A score of 70 or more can create a watched company after primary-site verification, unless there is inactivity or incompatible employment evidence. Lower-scoring companies remain available for inspection and manual watching. Watching an existing company preserves its saved actions and paused/archived state.

The **Watch using search preferences** action follows normal job preparation, application, and posting-related networking policy. It does not create standalone company outreach. **Find jobs only** remains an explicit restriction on automatic applications for that target.

## Crawling and scheduling

Jobseek catalog imports retain every board's monitor and scraper configuration, including multiple boards per company. The pinned isolated runtime executes Jobseek's actual monitors and scrapers, including rendered pages, sitemaps and PDF extraction. See [Jobseek integration](jobseek.md). Native Greenhouse, Lever and Ashby links retain their configured access scopes. Known career targets can poll without repeating company research. Partial coverage does not prove that unobserved jobs closed.

Source discovery can propose more directories from search results and observed investor links. Candidate sources are evaluated before use. Public catalog batches use native offsets, so an irrelevant first slice cannot hide later companies. Continuations run on later bounded passes; completed sources normally refresh daily. Failed reads retain explicit diagnostics and retry deadlines.

Source yield uses persisted company signals, matching target jobs, confirmed applications, and trusted interview/offer outcomes. Allocation changes company and call budgets while retaining exploration. Passive companies do not receive recurring research unless explicitly watched. The UI exposes pause/resume/check controls, limits and discovery toggles.

Structured feeds include RSS/Atom, SEC Form D and Dealroom public APIs. Licensed Dealroom and Crunchbase adapters accept account keys; MAGNiTT licensed feeds use an account-provided endpoint contract. Credentials are never assumed. See [company feeds](company-feeds.md). HTTP blocks remain explicit access failures.

## Predictive company research

The native [job-swarm predictive workflow](job-swarm.md) grows a company evidence corpus, analyzes fit and trajectory, forecasts likely roles, independently audits the evidence, and prepares founder memos and proof-of-work briefs. Its review queue lives on the same **Company signals** page, with per-company research controls, source links, missing-history explanations, and manual outcome tracking.

Forecasts remain separate from confirmed vacancies. Research drafts do not send messages, and proposed artifacts remain proposals until you build them. The existing target and application policies continue to govern observed jobs.

## HiringCafe

The `hiring_cafe` job source reads current public server-rendered search data from `hiringcafe.com`, including native pagination and original application URLs. The older `hiring.cafe/api/search-jobs` POST scraper is no longer the integration contract. When direct access is challenged, the source can use the existing authorized BrowserOS recovery path. Aggregator inclusion does not exempt a job from employer or vacancy-risk checks.

## Results and model configuration

Results and diagnostics provide optional run selection. Confirmed submissions are separated from historical agent-reported submissions that lack captured proof. Learning exposes recorded rounds and before/after evaluation scores; RLM has its own settings and trace view.

Gateway routing consumes refreshed inventory capabilities and provider-owned protocol metadata. Unsupported clients and transient route failures use the existing runtime recovery policy; plugin code does not carry an independent list of banned model names. Learning/RLM remain enabled by default, with evaluation and promotion gates preserved.

Reference implementations: [Jobseek](https://github.com/colophon-group/jobseek), [Startup Portfolios](https://github.com/yigitmeteozcan/startups), [HiringCafe scraper history](https://github.com/umur957/hiring-cafe-job-scraper), and [job-swarm predictive company research](https://github.com/LiamKozma/job-swarm). Job-swarm's future-role prediction is distinct from fraudulent-vacancy detection.

# Predictive company research

Open **Targets & sources → Company signals** to review predictive company research alongside the discovery evidence. The workflow follows companies before a matching vacancy appears, connects their published work to your experience, and prepares a founder memo and a proof-of-work proposal for review.

## Upstream and native integration

This is a native adaptation of [LiamKozma/job-swarm](https://github.com/LiamKozma/job-swarm). The upstream workflow combines a growing company corpus, semantic trajectory analysis, independent model review, diversified memo selection, proof-of-work briefs, and recorded outreach outcomes. ApplyPilot runs these phases through its existing company-discovery scheduler and workspace database.

Model calls use the existing LiteLLM gateway, DSPy learning, and RLM context handling. Provider selection, model recovery, and retry policy stay with those shared services. Learning evaluation and promotion checks remain active.

Local analysis uses `sentence-transformers`, `numpy`, `hmmlearn`, and `scikit-learn`. The CPU embedder loads lazily when predictive research needs it. No Slurm jobs, HPC cluster, dedicated GPU server, or separate model gateway are required.

## Research and evidence

Discovery supplies company candidates from observed funding, accelerator, portfolio, product, and other signals. Predictive research grows a dated corpus from observed primary company blog, product, and research pages and bounded public-source searches. Existing job discovery supplies vacancy and posting history. This integration does not claim a dedicated adapter for every source in the upstream repository.

Documents preserve source URLs, publication dates when available, observation times, and the material used for analysis. Publication dates and retrieval dates have different meanings: repeatedly fetching a page does not give a company a new authored event. Missing dates and insufficient history remain visible.

Your saved résumé, project descriptions included in it, and saved profile facts provide the expertise context. Each eligible research pass uses the current material and replaces stale analysis when the profile changes.

## Analysis

Document embeddings measure alignment with both the pooled profile and individual expertise facets. Trajectories use company-authored material grouped by publication day; five dated days are needed before fitting a two-regime Gaussian hidden Markov model. Euler–Maruyama simulation estimates semantic escalation over the next quarter. These values describe changes in published language, not the probability of a vacancy or a verified business problem.

Hiring trends use complete observations from the same career board and search preferences. Posting-arrival analysis excludes the first inventory, which cannot establish when existing jobs were published. Persistent listings and possible reposts remain observations with unknown causes. Role forecasts come from clusters of real observed vacancies and stay out of the jobs table.

Draft selection penalizes redundant company matches and reserves bounded exploration when at least two drafts per pass are enabled and eligible candidates are available. Recorded replies and rejections can train a logistic ranking model after at least 40 labeled employers. Promotion requires improvement over both the existing heuristic and the baseline on a later, employer-disjoint holdout; insufficient or inconclusive outcomes leave the heuristic active.

## Review queue

Each company report exposes:

- Profile alignment, company trajectory, escalation estimates, and missing-history explanations.
- Likely future roles, explicitly presented as forecasts alongside observed hiring evidence.
- An independent review grounded in source excerpts, including risks and unresolved questions.
- A founder-directed memo with checked supporting quotations.
- A proof-of-work brief scoped to at most two days, with a deliverable, steps, and a verification plan.
- Source links and dates so the reasoning can be traced back to the underlying material.

Draft selection balances fit with diversity and exploration. A forecast does not create a vacancy. Existing job discovery, job eligibility checks, document preparation, and application controls handle confirmed openings.

The founder memo is a reviewable draft. The artifact brief proposes work to build; it does not claim that the work has already been completed. Predictive research does not send messages.

## Scheduling and outcomes

Company discovery settings include **Research future hiring needs**, **Companies per predictive research pass** (1–5, default 2), and **Founder drafts per predictive research pass** (1–3, default 2). Predictive research starts enabled and shares the existing discovery schedule. Disabling company discovery also stops its scheduled research work.

Use **Research again**, **Pause research**, and **Resume research** for an individual company. Failures retain an error and next retry time. Pausing a report preserves its evidence and drafts.

Record outcomes manually as **Reviewing**, **Contacted**, **Followed up**, **Replied**, **Rejected**, or **Closed**, with optional notes. These updates record what happened; they do not send or schedule a message. Outcome data feeds subsequent selection once sufficient history is available.

Contacted, followed-up, replied, rejected, and closed companies stop receiving automatic research updates. Set an outcome back to **Reviewing** when you want to reconsider the company; resume research separately if you also paused it.

See [Company discovery and watchlists](company-discovery.md) for discovery sources, watching companies, and the normal job pipeline.

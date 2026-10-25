# Career workflows

ApplyPilot adapts the [career-ops feature set](https://github.com/career-ops-hq/career-ops#features)
into its existing candidate evidence, company research, application outcomes, and inference pipeline.
These workflows produce saved reports and drafts. They do not send messages, submit applications,
change the application policy, or promote generated achievements into the candidate's evidence ledger.

Use **Career workbench** in the web interface. Search saved opportunities by role, company or URL,
in one typing-and-selection box. Results open immediately, with a 150 ms trailing debounce.
Select a role to reuse its company, description and application evidence, or choose the first
option, **Custom opportunity**, to enter the required company and description. Older opportunities
remain searchable.
Pasted descriptions stay report inputs; they do not create verified vacancies or application jobs.

Every opportunity's detail view links to its career reports and interview preparation. The scoped
workbench shows that opportunity's reports and practice sessions. The **Preparation library** includes
previously generated interview briefs and every career workflow, with Pending, Preparing, Completed,
Needs input, Retry needed and Cancelled states. Select a report to read it in the workbench.

Trusted Gmail invitation outcomes and verified direct networking conversations captured by the
existing Gmail/LinkedIn/X connections queue the interview brief, evaluation, interview plan and
first practice question. Each invitation and workflow has one durable report identity, so repeated
syncs do not duplicate reports. Debrief, negotiation, offer reading and patterns entries wait for
the user's actual notes or terms. A missing saved description also waits for input. Social detection
reuses the existing outcome classifier and does not turn a networking message into a confirmed
application outcome. Unrelated or ambiguous conversations are not matched by guessed company names.

The existing brief stage now consumes this same report queue and retry schedule. Its generator,
claim checks and CLI remain available; historical files stay readable. New report-specific brief
artifacts prevent one preparation attempt from overwriting another report's document. Workers process
the queue while automation runs, and **Prepare now** can run an individual queued report.

Reports automatically use saved profile facts and current actual pay. Profile pay periods are mapped
to the report's units (for example, monthly to month) without currency or amount conversion. Open
**Compensation amounts and overrides** to supply report-specific figures; overriding current pay
requires its amount, currency and period together and does not change the profile. Legacy salary
expectations are not substituted for current pay or used as a universal desired salary.

## Workflows

| Mode | Result |
| --- | --- |
| `interview_brief` | The existing invitation-aware interview brief, using saved candidate evidence and captured correspondence, now stored and browsed with all career workflows. |
| `evaluate` | A–H evaluation: role summary, requirement match, level strategy, compensation, personalization, interview preparation, posting legitimacy, and application-answer drafts. Includes source-backed company research, work-authorization signals and reusable STAR story drafts. |
| `interview_plan` | Time-blocked preparation within a supplied 15–720 minute budget, company questions, grounded stories and unresolved information. |
| `interview_practice` | One question, then the candidate's answer, feedback quoting that answer, and the next question. Sessions and answers persist across restarts. |
| `interview_debrief` | A review of the candidate's actual notes and questions, gaps, employer warning signals, next steps and explicitly tentative next-round expectations. |
| `negotiate` | Evidence-backed negotiation drafts, non-cash topics and package questions. Current, desired, advertised and offered figures retain their supplied currencies and periods. |
| `offer_review` | An ordered walk through quoted offer clauses, comparisons with supplied information, missing documents and questions for an employer or lawyer. |
| `patterns` | ATS/channel cohorts from existing observed submissions, explicit rejection evidence and an upskilling plan only when a rejection excerpt actually identifies the proposed skill. |

The A–H report follows the upstream [evaluation workflow](https://github.com/career-ops-hq/career-ops/blob/main/modes/oferta.md).
Every requirement carries a verbatim JD excerpt, an importance band and one provenance label:
`jd_wording`, `jd_structure`, or `estimate`. Estimated importance cannot be `critical` or `high`.
The 1–5 global score is a holistic judgment, not arithmetic on the importance bands.
Application-answer drafts are omitted below 4.5 or when no application questions are available.

Work-authorization checks reuse ApplyPilot's existing country-scoped profile projection.
An explicit sponsorship prohibition plus a known sponsorship need is a report blocker.
Unknown work authorization asks for clarification rather than borrowing citizenship or residence.
This report does not alter the discovery eligibility engine or application permissions.

## Evidence and limitations

Candidate sources come from the original resume, approved profile facts, eligible impact records,
and curated project evidence already used by document generation. Company facts come from the
existing company research document store. Documents preserve collection and publication dates.
Run company discovery/research for fresh coverage; these career reports do not pretend a missing
company document is evidence or silently replace it with model recollection.

Claims carry source IDs and exact quotes, checked after generation. Candidate matches and STAR
components must cite candidate sources. Generated stories remain drafts: reflection is a prompt
for the candidate, and neither practice answers nor debrief recollections become approved facts.
Exact-quote validation establishes provenance; interpretations remain model judgments for review.

An [interview session](https://github.com/career-ops-hq/career-ops/blob/main/modes/interview/README.md)
retains its original job context. Answer with its `session_id` and `session_revision`; each answer is tied to the current
session revision, so an old retry cannot grade an answer against a newer question. Sessions support
up to 30 answers. Start a new session for further practice.

Compensation amounts require an explicit three-letter currency and hour/day/week/month/year period.
Gaps are calculated with decimal arithmetic only when both currency and period match. No exchange
rate, working-hours assumption or annualization is invented. Missing figures remain missing.

The [offer reading workflow](https://github.com/career-ops-hq/career-ops/blob/main/modes/offer-prep.md)
requires pasted document text. Each clause quote must occur in the document in order. It produces
plain-language interpretations and questions, without legal conclusions or a signing recommendation.
It does not perform jurisdiction-specific legal research or decide whether a clause is enforceable.

Pattern analysis reuses the existing feedback observation horizon and trust rules. Mature observed
submissions form rate denominators; young, ambiguous, or unobserved records remain excluded.
Silence is never manufactured into rejection. Rates include sample sizes and Wilson intervals.
ATS names come from recorded submission target domains, with unrecognized domains labelled Unknown.
The report does not infer that an ATS caused an outcome. Unspecified rejection reasons do not justify
an invented skills diagnosis.

## Persistence and recovery

Reports, sessions and story drafts live in `career_ops_reports`, `career_ops_sessions` and
`career_ops_stories`. Reports record inputs, source revisions, generated output, attempt count,
state, errors and the next retry time. Inputs have per-field limits and a combined 300 KB limit.
Model calls occur outside database transactions through `workflow_json('company_research', ...)`;
DSPy/GEPA learning, long-context RLM routing and LiteLLM recovery stay active.

An exhausted provider call or failed output invariant leaves an error report scheduled for another
round after exponential backoff, capped at one day. The background career workflow cycle retries
only previously requested reports. Interrupted workers are recovered after two hours. Correctable
input errors and explicit cancellation do not automatically retry; submit corrected input or retry
a cancelled report explicitly. There is no persistent learning or model lockout.

## Python interface

The CLI accepts the same data contract:

```console
applypilot career-ops run evaluate --job-url https://example.com/jobs/123
applypilot career-ops run interview_practice --input-json practice.json
applypilot career-ops reports
applypilot career-ops retry --id REPORT_ID
```

An input JSON file contains the fields listed below. For a subsequent practice turn, include
`session_id`, `session_revision` (the session's `revision` from `reports`) and `answer`;
the session retains the original role context.

```python
from applypilot.career_ops import service, store

store.ensure_schema(conn)
report = service.run(conn, "evaluate", {
    "job_description": pasted_description,
    "company_name": "Example Company",
    "instructions": "Focus on seniority and missing interview evidence.",
})
reports = store.overview(conn)
```

`service.run(conn, mode, data, *, cancel_event=None)` accepts:

- `job_url` for a saved job, or `job_description` and optional `company_name`.
- `instructions`, `notes`, `offer_text`, `interview_date`, and `minutes` as appropriate.
- `session_id`, `session_revision` and `answer` to continue practice.
- `current_amount`, `current_currency`, `current_period` and the equivalent `desired_`,
  `advertised_`, and `offer_` fields for compensation comparisons.
- `retry_report_id` to retry an errored or cancelled report using its saved input.

The returned report includes `id`, `mode`, `job_url`, `state`, `result`, `error`,
`session_id`, `attempts`, `next_retry_at`, `created_at`, and `updated_at`.
`store.overview(conn, job_url=None)` returns `reports`, `sessions`, `stories`, and the mode labels.

This is a native implementation informed by upstream workflows, rather than a second application
or an invocation of the upstream CLI. Existing ApplyPilot document and application workflows remain
their own capabilities.

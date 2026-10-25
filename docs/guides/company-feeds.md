# Structured company and funding feeds

Company signals consume RSS/Atom, SEC Form D filings, Dealroom public APIs,
Dealroom Premium v1 and Crunchbase v4. Records use the existing company identity
verification, scoring and watch policy. They do not create vacancies.

The source panel shows access errors and confirmed application, interview and
offer yield. Missing credentials, changed schemas and repeated cursors fail
explicitly. An empty valid feed is distinguished from a failed read.

## Public feeds

The registry includes TechCrunch startup RSS, PR Newswire venture-capital RSS,
Guardian technology RSS, Dealroom market maps/live signals, and SEC current
Form D filings. Add RSS/Atom URLs through **Targets & sources → Company signals**.

Cursors resume bounded batches. Article links are read before company evidence
is accepted. SEC issuer and funding-signal names can be resolved through observed
search/homepage evidence; a name match is not funding corroboration. Form D is a
fundraising filing, not proof a round closed. Publication, filing and event dates
remain distinct. Month-only dates cannot earn a dated-event bonus.

Dealroom public maps are sampled: returned counts, advertised totals and coverage
notices remain in source evidence. Its live signals can omit company websites
and precise dates. The public marketmaps and live-signals endpoints returned data
during implementation; this does not establish complete licensed coverage or
verify every source in the registry.

## Account connections

Save these in **Settings → Connections** or the workspace `.env`:

| Setting | Purpose |
|---|---|
| `APPLYPILOT_SEC_USER_AGENT` | Application name and contact email for SEC requests |
| `DEALROOM_API_KEY` | Licensed Dealroom Premium v1 key |
| `CRUNCHBASE_API_KEY` | Licensed Crunchbase v4 key |
| `MAGNITT_API_KEY` | Licensed MAGNiTT key with an account-provided contract |

No credentials are shipped. Licensed APIs have not been called without an
account. Saving a key does not add a licensed source automatically. Add the
appropriate endpoint as a funding-database source when you want to use it:

- Dealroom: `https://api.dealroom.co/api/v1/companies`
- Crunchbase: `https://api.crunchbase.com/v4/data/searches/organizations`

Dealroom v1 uses Basic authentication with the key as username and empty password;
limit/offset are query parameters. Crunchbase uses `X-cb-user-key`, recent-funding
queries and `after_id` pagination. Keys never enter source URLs or evidence.

References: [Dealroom public APIs](https://dealroom.co/for-agents/),
[Dealroom Premium](https://docs.dealroom.co/docs/premium-api/schemas/V1_UserFilters),
[Crunchbase search](https://data.crunchbase.com/docs/using-search-apis), and
[career-ops funding discovery](https://github.com/career-ops-hq/career-ops/blob/main/company-funded.mjs).

## MAGNiTT licensed contract

Public MAGNiTT pages use the normal page reader. Private endpoint and response
details must come from the licensed account. Put a contract list in
`~/.applypilot/company-feed-contracts.json` (or your `APPLYPILOT_DIR`) and add its
exact endpoint as a company source. This is a shape example: replace endpoint,
header, paths and parameter names with the account's documented values.

```json
[
  {
    "name": "Licensed MAGNiTT feed",
    "url": "https://api.magnitt.com/ACCOUNT_PROVIDED_PATH",
    "method": "GET",
    "credential_env": "MAGNITT_API_KEY",
    "auth_header": "ACCOUNT-PROVIDED-HEADER",
    "auth_prefix": "",
    "records_path": "data.companies",
    "next_path": "pagination.next_cursor",
    "pagination": "cursor",
    "parameters_in": "query",
    "limit_param": "limit",
    "cursor_param": "cursor"
  }
]
```

Only HTTPS MAGNiTT origins are accepted. Contracts are configuration, not code.
GET/query and POST/query-or-body requests support cursor or offset pagination.
Responses must honor the record limit. Invalid MAGNiTT configuration does not
disable unrelated public feeds.

## Tiers and source allocation

Below 30 is ignored; 30–49 is passive; 50–69 receives research; 70+ can be watched
after primary verification. A bounded initial identity check can clarify a newly
seen company; unchanged passive records do not consume recurring research calls.
An explicit owner watch overrides scoring. Automatic watches follow the latest
verified score and employment restrictions.

Source allocation credits relevant companies, jobs, confirmed submissions, and
trusted interview/offer events, discounted by observed resource cost. Voided,
untrusted and ambiguous outcomes cannot become positive yield. Exploration keeps
overdue sources in rotation. Company and call allocations vary within the shared
40 HTTP/20 model request limits; up to six sources can be admitted in one pass.

See [Jobseek](jobseek.md), [predictive research](job-swarm.md) and
[Career workbench](career-ops.md) for downstream workflows.

# Feature coverage and remaining migration work

This is an integration audit of the new execution path, **not an all-feature
production test result**. A finite adapter means there is a callable connection
to existing behavior. It does not mean the old implementation's internal locks,
budgets, data locality or all recovery semantics have disappeared.

| Feature family | Native entry / status in this patch | Remaining work or invariant |
|---|---|---|
| Runtime policy | Existing resolver now has validated Temporal section; policy snapshot in Workflow input | Legacy policy sections remain for old paths and finite adapters |
| Job dispatch | DispatchWorkflow + readiness-aware keyset planner + stable job child IDs | No application task reservation set; existing business eligibility remains |
| Enrichment | `enrich` Activity -> `run_stage_job` | Browser/network helper behavior still needs real capability tests |
| Company research | `company_research` Activity -> `run_company_job` in job chain | Old company retry timestamps remain; no independent company-only backlog scanner in native planner |
| Scoring | `score` Activity; native selection omits old score retry-date/attempt caps | Model calls and existing downstream provider admission remain |
| Tailored resume | `tailor` Activity, stored output checked | Artifact validation/approval and business predicates remain |
| Cover letter | `cover` Activity, stored output checked | Existing model validation/recovery logic remains |
| PDF / DOCX | `pdf` Activity invokes existing converters, declared format | Shared filesystem and legacy absolute paths; not object-store rebasing |
| Impact/application brief | `brief` Activity invokes existing brief stage | Brief outcome does not yet have the same explicit expected-field postcondition as other job phases |
| Application attempts | Dedicated ApplicationWorkflow, one irreversible Activity attempt then evidence observation | Existing atomic attempt/authorization checks remain; existing attempts are not deleted or automatically reset |
| Submission reconciliation | Existing persisted receipt/captured-submission reconciliation | No guarantee of provider-visible absence; unknown outcomes can remain unresolved |
| BrowserOS / extension / session tooling | Existing application/enrichment adapters remain available | No durable browser-process-memory replay; isolated endpoints/profile directories required |
| Direct job/discovery traversal | Native task queues, no Dask supervisor or runtime registry dispatch | Physical adapters can still contain local resource state |
| Configured discovery providers | Existing capability registry selects a finite `execute_task` adapter | No claim each provider was integration tested; source internals may cap work or require grants |
| ATS boards/registry | Native planner unions configured and observed board targets | Provider/schema behavior and credentials unchanged |
| Source-page publication | Short page-specific transaction + replayable publication receipt | Real PostgreSQL concurrency test unrun; existing business publication locks remain |
| Signal-cache acknowledgement | Committed receipt replays acknowledgement independently | Source cache remains existing subsystem, not Temporal persistence |
| Partial provider coverage | Explicit `coverage_partial` result | No fake exhaustion; automatic authorization/fallback selection not newly implemented |
| Absence / source retirement semantics | Not fully ported | Complete source-snapshot disappearance/retirement accounting still needs explicit native implementation |
| Paid discovery transport | No fake budget grant | Verified spend integration needed; this path refuses to fabricate authorization |
| Admission/evidence refresh | Finite `maintain_evidence` plus durable traversal cursor | Existing business checks and recovery seeding remain |
| Referral preparation | Finite `prepare_referrals` service Activity | Existing eligibility/routing/outreach intent rules remain |
| Application/email tracking | Finite `run_tracking` service Activity | Mail/account integrations and old internal state unchanged |
| Network preparation | Finite preparation pass | Existing unattended permissions and per-pass limits remain |
| Network research and connection refresh | Finite explicit research pass, no dispatcher supervisor | Existing case retry dates/lease fields and refresh state remain |
| Network delivery | Single-attempt service Activity invokes current delivery routine | Native Schedule is not exactly-once delivery; existing receipt/approval logic remains; no new independent delivery-reconciliation Workflow |
| Company discovery | Finite `run_cycle` service Activity | Existing model/http reservation budgets and retry delays inside that routine remain |
| Career operations | Finite `run_cycle` service Activity | Existing mode-specific retry/subtask behavior remains; individual CLI tools stay available |
| DSPy / GEPA / MIPRO optimization | Learning service Activity -> existing finite learning round | No new distributed cohort-by-cohort optimizer Workflow/checkpoint adapter; existing locks, cooldowns, evaluation budgets remain |
| RLM | Existing domain RLM execution called inside Activities; native scope avoids legacy inference-slot acquisition | Internal RLM/model limits and session state not rewritten or made replay-durable automatically |
| Policy evaluation/promotion/rollback | Existing learning/policy functions preserved | Workflow replay compatibility and immutable policy artifact rollout still need full lifecycle tests |
| Backup/sync/restore tools | Finite automatic backup service Activity; existing CLI tools retained | Backup locks, restore exclusion and provider rate limits remain; restore is not automatically driven by the new scheduler |
| Impact ledger / defense / draft tools | Existing CLI/domain tools preserved | Not every interactive tool has a new dedicated native Workflow command |
| Onboarding/authentication | Existing wizard/credential setup preserved | Temporal does not create user credentials, bypass access control, or answer unknown applicant facts |
| LiteLLM / egress / Redis allocation | Previous patch and independent infrastructure preserved | This patch does not migrate egress controllers or gateway admission to Temporal |
| Existing application dashboard | New read-only Temporal live/cached panel | Existing Start/Stop, legacy status projections and most mutation APIs remain legacy |
| Diagnostics ZIP | Adds latest native status/install snapshots, excludes raw immutable inputs/receipts | Capture must be requested; not every remote history/stack/server metric is exported |
| Kubernetes/remote compute | Native capability-worker launch; workflow-only container and Deployment example | No full browser/model Activity images, storage migration, HA server chart or autoscaler supplied |
| Community/SDK plugins | Native SDK plugin injection boundary | No third-party plugin silently installed; no verified universal DSPy/GEPA/RLM plugin claimed |
| Legacy Dask entry points | Preserved and explicitly separated | No complete dependency/code deletion; do not run old and native orchestration concurrently |

## Completion criteria for a complete replacement

A complete replacement still requires actual startup/replay/crash tests, every
finite adapter's old scheduling decisions removed or replaced by supported
Temporal primitives, distributed artifact/session handling, optimizer checkpoint
and policy rollout semantics, delivery reconciliation, source coverage parity,
and native dashboard command integration. Then remove the old supervisor,
DaskProducer paths, dependency pins, admission/reservation state that no longer
serves an external-side-effect invariant, and obsolete UI/diagnostic projections.

This patch does not label those remaining items “complete” merely because a
service Activity can call their entry point. Nor does it manufacture success or
force an action when authorization, applicant facts, external evidence or a
required worker capability is absent.

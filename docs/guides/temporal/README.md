# Temporal continuation: staging entry point

**Migration is incomplete.** Current implementation and deployment instructions
are maintained in the repository-root `TEMPORAL_ARCHITECTURE.md` and `DEPLOYMENT.md`.
`FEATURE_MAP.md`, `FEATURE_REGISTRY.json`, `ARCHITECTURE_AUDIT.json` and
`VALIDATION.json` record active legacy paths and precise evidence.

This continuation starts from the uploaded `ApplyPilot-main_38.zip`, not the
original baseline. The earlier `_37.zip` identified by previous metadata was not
available, so an original-baseline complete patch is not claimed.

New job/discovery types are v2. Retain compatible v1 workers for old histories;
do not use the new command sequences as an unreviewed in-place replacement.
Documents and signal extraction have explicit Activities and new model/document
queues. Generic job, application, learning and service paths still invoke legacy
code. See the root architecture document before running any capability.

The local coordinator remains `../docker-compose.temporal.yml`; it contains
persistent Temporal PostgreSQL, explicit schema setup, server, UI, namespace
initialization and a workflow worker. Search workers are separate. Docker and
Kubernetes examples were not executed in the delivery environment.

The Dockerfile-specific ignore files allow only their required source/configuration
inputs. Never add local tunnel keys, browser profiles, private environment files
or applicant evidence to an image context.

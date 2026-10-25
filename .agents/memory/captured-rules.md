---
status: temporary
last_verified: 2026-10-04
---

# Captured workflow rules

## Provider errors must not stop gateway refresh

**User said:** “if theres an error than theres an error … it shouldnt pause/stop a refresh”

**Scope:** ApplyPilot gateway/provider maintenance.

**Rule:** Record provider/API-key failures through the existing Prometheus,
Grafana, and ApplyPilot dashboard surfaces. Retain that provider's previous
inventory and continue applying successful provider updates. Provider errors
must not become a global refresh pause or an automatic provider disable.

**Configuration preference:** Simple custom OpenAI-compatible providers should
be declared in JSON with an API base, optional credential reference, optional
model discovery, and optional limits. Use an updater for richer provider-specific
discovery.

**Implementation and operator guide:** `docs/GATEWAY_REFRESH.md`.

**Status:** Captured from an explicit request to record the workflow. No shared
agent-instruction files were changed.

"""One complete display catalog; ordering is presentation, never scheduling."""
from __future__ import annotations

# id, label, journal stage/component/operation, unit, eligible-queue key, owner
STAGES = (
    ("discover", "Discovery", "discovery", "discovery", "fetch_page", "pages", None, "engine"),
    ("enrich", "Enrichment", "enrich", "enrich", "job", "jobs", "enrich", "engine"),
    ("admission", "Evidence admission", "admission", "admission", "refresh", "passes", None, "engine"),
    ("score", "Scoring", "score", "score", "job", "jobs", "score", "engine"),
    ("tailor", "Résumé tailoring", "tailor", "tailor", "job", "jobs", "tailor", "engine"),
    ("cover", "Cover letters", "cover", "cover", "job", "jobs", "cover", "engine"),
    ("pdf", "Document generation", "pdf", "pdf", "document", "documents", "pdf", "engine"),
    ("brief", "Interview briefs", "brief", "brief", "job", "jobs", "brief", "engine"),
    ("applications", "Applications", "applications", "applications", "attempt_result", "attempts", "apply", "autopilot"),
    ("company_research", "Company research", "company_research", "company_research", "job", "jobs", None, "engine"),
    ("referrals", "Referral planning", "referrals", "referrals", "prepare", "passes", None, "engine"),
    ("tracking", "Mailbox tracking", "tracking", "feedback", "sync_mailbox", "passes", None, "autopilot"),
    ("outreach", "Outreach delivery", "outreach", "outreach", "deliver", "passes", None, "autopilot"),
    ("unattended", "Networking maintenance", "unattended", "unattended", "cycle", "cycles", None, "autopilot"),
    ("network_research", "Contact research", "network_research", "network_research", "case", "cases", None, "autopilot"),
    ("learning", "Learning", "learning", "learning", "round", "rounds", None, "autopilot"),
    ("recovery", "Recovery", "recovery", "recovery", "recover", "jobs", None, "engine"),
    ("flow_control", "Buffer monitoring", "flow_control", "flow_control", "refresh", "passes", None, "engine"),
    ("backup", "Backups", "backup", "backup", "sync", "passes", None, "autopilot"),
    ("engine", "Engine coordinator", "engine", "engine", "run", "services", None, "engine"),
    ("autopilot", "Autopilot supervisor", "autopilot", "autopilot", "run", "services", None, "autopilot"),
)


def catalog():
    return [{"id": key, "label": label, "stage": stage, "component": component,
             "operation": operation, "unit": unit, "queue_key": queue, "owner": owner}
            for key, label, stage, component, operation, unit, queue, owner in STAGES]

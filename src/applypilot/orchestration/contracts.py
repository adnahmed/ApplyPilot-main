"""Wire contracts and identities. Pure functions; safe inside Workflow replay."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any

ROLES = ("workflow", "preparation", "discovery", "documents", "applications",
         "learning", "network", "maintenance", "research", "models", "email",
         "browser", "social", "reporting", "egress")
JOB_PHASES = ("enrich", "company_research", "score", "tailor", "cover", "pdf", "brief")
SERVICE_PHASES = ("admission", "referrals", "tracking", "network_prepare",
                  "network_research", "network_delivery", "company_discovery",
                  "career_ops", "learning", "backup", "backup_full", "email_warmup", "social")
CORE_KINDS = frozenset((*SERVICE_PHASES, "jobs", "apply", "discover", "source_refresh",
                        "backup_check", "backup_points"))
PHASE_ROLES = {
    **dict.fromkeys(("enrich", "company_research", "score", "tailor", "cover", "brief"), "preparation"),
    "pdf": "documents", "discovery_page": "discovery",
    "application_prepare": "applications", "application_attempt": "applications",
    "application_evidence": "applications", "application_review": "maintenance",
    "application_account_enqueue": "maintenance", "application_account_history": "maintenance",
    "document_inspect": "preparation",
    "learning": "learning", "network_prepare": "network",
    "network_research": "network", "network_delivery": "network",
    **dict.fromkeys(("plan", "admission", "referrals", "tracking", "company_discovery",
                    "career_ops", "backup", "backup_full", "backup_check", "backup_points"), "maintenance"),
}
PHASE_ROLES.update({
    **dict.fromkeys(('mailbox_begin', 'mailbox_page', 'mailbox_fetch_message'), 'email'),
    'mailbox_tracking_document': 'documents',
    'mailbox_classify_message': 'models', 'mailbox_outcome_model': 'learning',
    **dict.fromkeys(('mailbox_pending_page', 'mailbox_publish_message', 'mailbox_observations'), 'maintenance'),
})
PHASE_ROLES.update(dict.fromkeys(("learning_collect", "learning_build_dataset",
    "learning_evaluate_current", "learning_optimize", "learning_evaluate_candidate", "learning_publish"), "learning"))
# Retry only ordinary transient Activity failures. Expected domain dispositions
# are results, not exceptions. Unknown external effects have a separate workflow.
NON_RETRYABLE = ()


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def workspace_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("temporal.workspace must be a stable 1-64 character identifier")
    return value


def identity(workspace: str, kind: str, subject: str = "") -> str:
    workspace_id(workspace)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,47}", kind):
        raise ValueError("Invalid workflow kind")
    return f"ap:{workspace}:{kind}:{digest(subject)[:40]}"


def task_queue(prefix: str, workspace: str, role: str) -> str:
    if role not in ROLES:
        raise ValueError("Unknown worker role: " + role)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", prefix):
        raise ValueError("Invalid task queue prefix")
    return f"{prefix}.{workspace_id(workspace)}.{role}"


@dataclass
class Command:
    """Opaque database reference plus non-secret execution policy snapshot."""
    reference: str
    policy: dict[str, Any]
    kind: str = "jobs"
    cursor: str = ""
    page: int = 0
    totals: dict[str, int] = field(default_factory=dict)
    token: str = ""
    # Workflow clock at the start of a scan; retained across Continue-As-New.
    selection_time: float | None = None
    coverage_clean: bool = True


@dataclass
class ActivityRequest:
    reference: str
    phase: str
    cursor: str | None = None
    sequence: int = 0
    # Filled by the deterministic Workflow, not read from a mutable worker YAML.
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """Compact facts; full reports and applicant documents stay in the database."""
    state: str
    detail: dict[str, Any] = field(default_factory=dict)
    next_cursor: str | None = None


@dataclass
class WorkPage:
    items: list[dict[str, str]] = field(default_factory=list)
    next_cursor: str | None = None


@dataclass
class EntityCommand:
    """One job or company/posting entity; payload remains in the immutable store."""
    reference: str
    policy: dict[str, Any]
    entity_id: str
    kind: str = "jobs"


@dataclass
class ApplicationCommand:
    reference: str
    policy: dict[str, Any]
    application_id: str
    operation_id: str = ""
    attempt_id: str = ""
    previous_attempt_id: str = ""
    sequence: int = 0
    state: str = "observing"
    review_reference: str = ""
    dispatch_started: bool = False
    account_queued: bool = False
    account_workflow_id: str = ""


@dataclass
class AccountSubmission:
    command: ApplicationCommand
    requester: str


@dataclass
class ApplicationAccountCommand:
    policy: dict[str, Any]
    pending: list[AccountSubmission] = field(default_factory=list)
    submissions: list[float] = field(default_factory=list)
    handled: dict[str, float] = field(default_factory=dict)
    initialized: bool = False
    cancelled: list[str] = field(default_factory=list)
    provider_available_at: float = 0


def job_identity(workspace: str, job_url: str) -> str:
    workspace_id(workspace)
    if not isinstance(job_url, str) or not job_url.strip():
        raise ValueError("An existing posting identity is required")
    return digest({"workspace": workspace, "posting": job_url})


def validate_request_options(options: dict, *, recurring: bool = False, exact: bool = False) -> dict:
    """Validate finite business selection, not worker/concurrency controls."""
    result = dict(options)
    limit = result.get("limit")
    if limit is not None:
        if type(limit) is not int or limit < 0:
            raise ValueError("limit must be a nonnegative integer or null")
        if recurring:
            raise ValueError("limit is a finite request option; omit it for recurring Schedules")
        if exact and limit != 1:
            raise ValueError("An exact job request accepts only limit=1 (or no limit)")
    for name in ("min_score", "max_score"):
        value = result.get(name)
        if value is not None and (type(value) is not int or not 0 <= value <= 10):
            raise ValueError(name + " must be an integer from 0 through 10 or null")
    if (result.get("min_score") is not None and result.get("max_score") is not None
            and result["min_score"] > result["max_score"]):
        raise ValueError("min_score cannot exceed max_score")
    for name in ("target_ids", "source_request_keys"):
        values = result.get(name)
        if values is not None:
            if (not isinstance(values, (list, tuple)) or not values
                    or any(not isinstance(value, str) or not value.strip() for value in values)):
                raise ValueError(name + " must be a nonempty list of exact identities")
            result[name] = sorted(set(values))
    return result

"""Resolve deployment settings once, outside Workflow replay."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .contracts import JOB_PHASES, ROLES, SERVICE_PHASES, task_queue, workspace_id

Positive = Annotated[int, Field(strict=True, gt=0)]
Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class ActivityPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_to_close_seconds: Seconds
    heartbeat_seconds: Seconds = 60
    heartbeat_interval_seconds: Seconds = 10

    @model_validator(mode="after")
    def intervals(self):
        if self.heartbeat_interval_seconds * 2 >= self.heartbeat_seconds:
            raise ValueError("Allow at least two missed heartbeats before detecting worker loss")
        if self.heartbeat_seconds >= self.start_to_close_seconds:
            raise ValueError("Heartbeat detection must be shorter than an execution attempt")
        return self


class WorkerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    activities: Positive
    shutdown_seconds: Seconds = 60
    max_activities_per_second: Seconds | None = None
    max_task_queue_activities_per_second: Seconds | None = None


class LinkedInApplicationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    account_id: str = "primary"
    spacing_seconds: Seconds = 600
    hourly_limit: Positive = 4
    daily_limit: Positive = 20
    restriction_retry_seconds: Seconds = 86400


class SchedulePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    every_seconds: Seconds
    enabled: bool = True


class TemporalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # A namespace is an authorization/isolation boundary. A task queue is not.
    workspace: str = "default"
    namespace: str = "applypilot"
    queue_prefix: str = "applypilot"
    job_phases: tuple[str, ...] = JOB_PHASES
    page_size: Annotated[int, Field(strict=True, ge=1, le=256)] = 32
    pages_per_run: Annotated[int, Field(strict=True, ge=1, le=32)] = 8
    discovery_pages_per_run: Annotated[int, Field(strict=True, ge=1, le=128)] = 32
    evidence_observations_per_run: Positive = 32
    workflow_tasks_per_worker: Positive = 64
    observation_limit: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100
    history_retention_days: Positive = 30
    schedule_catchup_seconds: Seconds = 86400
    evidence_poll_seconds: Seconds = 60
    # A business evidence freshness horizon, not a SQL due/retry clock.
    source_refresh_age_seconds: Seconds = 86400
    activity_retry_initial_seconds: Seconds = 1
    activity_retry_maximum_seconds: Seconds = 30
    linkedin_applications: LinkedInApplicationPolicy = Field(default_factory=LinkedInApplicationPolicy)
    workers: dict[str, WorkerPolicy]
    activities: dict[str, ActivityPolicy]
    schedules: dict[str, SchedulePolicy]

    @model_validator(mode="after")
    def complete(self):
        workspace_id(self.workspace)
        if not self.namespace or len(self.namespace) > 128:
            raise ValueError("A Temporal namespace is required")
        for role in ROLES:
            task_queue(self.queue_prefix, self.workspace, role)
        if set(self.workers) != set(ROLES):
            raise ValueError("Declare each Temporal worker role exactly once")
        if set(self.activities) != set(ROLES) - {"workflow"}:
            raise ValueError("Declare the execution-attempt contract for every Activity role")
        if not self.job_phases or len(set(self.job_phases)) != len(self.job_phases):
            raise ValueError("job_phases must be nonempty and unique")
        if any(phase not in JOB_PHASES for phase in self.job_phases):
            raise ValueError("Unknown job phase")
        if tuple(phase for phase in JOB_PHASES if phase in self.job_phases) != self.job_phases:
            raise ValueError("Preserve the domain dependency order when selecting job phases")
        if set(self.schedules) != {*SERVICE_PHASES, "jobs", "discover", "source_refresh"}:
            raise ValueError("Declare all autonomous schedules, including disabled ones")
        if self.activity_retry_initial_seconds > self.activity_retry_maximum_seconds:
            raise ValueError("Native Activity retry maximum must be >= initial delay")
        return self

    def queue(self, role):
        return task_queue(self.queue_prefix, self.workspace, role)


def load(root=None) -> TemporalPolicy:
    from applypilot.runtime_policy import load_policy
    policy = load_policy(root).temporal
    # Established backup preferences are resolved once into native Schedule
    # specifications. The backup adapter never interprets these as due clocks.
    from applypilot.config import APP_DIR
    directory = Path(root or APP_DIR)
    if (directory / "github_sync" / "config.json").is_file():
        from applypilot.backup.common import config as backup_config
        cfg = backup_config(directory)
        schedules = policy.model_dump(mode="json")["schedules"]
        schedules["backup"]["every_seconds"] = cfg["static_minutes"] * 60
        schedules["backup_full"]["every_seconds"] = cfg["snapshot_hours"] * 3600
        policy = TemporalPolicy.model_validate({**policy.model_dump(), "schedules": schedules})
    # Identity changes are explicit worker/client deployment configuration.
    changes = {field: os.environ[env] for field, env in (
        ("workspace", "APPLYPILOT_TEMPORAL_WORKSPACE"),
        ("namespace", "APPLYPILOT_TEMPORAL_NAMESPACE"),
    ) if os.getenv(env)}
    return TemporalPolicy.model_validate({**policy.model_dump(), **changes})


def certificate(env: str) -> bytes | None:
    value = os.getenv(env, "").strip()
    return Path(value).read_bytes() if value else None

"""One declarative contract for execution, scheduling, buffers and support capture.

Temporal owns native task execution; legacy legacy worker configuration remains for rollback. This module only
resolves data. It never claims work, raises a capacity deferral or writes state.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from applypilot.orchestration.settings import TemporalPolicy
import yaml

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
Seconds = Annotated[float, Field(gt=0, allow_inf_nan=False)]
BUFFER_NAMES = frozenset({"score", "qualified", "cover", "documents", "apply"})
SERVICE_NAMES = frozenset({"admission", "recovery", "referrals", "company_discovery", "career_ops"})
# Unconditional PostgreSQL session semantics. Connection acquisition is separate.
SQL_SESSION_SETTINGS = {
    "statement_timeout": "0", "lock_timeout": "0", "idle_in_transaction_session_timeout": "0",
}


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CapacityPolicy(PolicyModel):
    workers: PositiveInt
    browsers: PositiveInt | None = None
    inference: PositiveInt | None = None
    pending: PositiveInt | None = None
    recovery: PositiveInt | None = None
    dispatch_batch: PositiveInt
    database_connections: PositiveInt | None = None


class SchedulePolicy(PolicyModel):
    poll_seconds: Seconds
    refill_seconds: Seconds
    scan_seconds: Seconds
    inventory_seconds: Seconds
    publish_seconds: Seconds
    status_rpc_seconds: Seconds
    status_max_age_seconds: Seconds
    retry_seconds: Seconds
    not_ready_seconds: Seconds
    company_retry_seconds: Seconds
    services: dict[str, Seconds]

    @model_validator(mode="after")
    def known_services(self):
        if set(self.services) != SERVICE_NAMES:
            raise ValueError("schedule.services must declare every supported service")
        return self


class Watermarks(PolicyModel):
    low: Annotated[int, Field(strict=True, ge=0)]
    high: Annotated[int, Field(strict=True, gt=0, le=100000)]

    @model_validator(mode="after")
    def ordered(self):
        if self.low >= self.high:
            raise ValueError("buffer low must be smaller than high")
        return self


class OutputPolicy(PolicyModel):
    buffer: str
    when_selected: str | None = None
    unless_selected: str | None = None

    def selected(self, stages):
        return ((self.when_selected is None or self.when_selected in stages)
                and (self.unless_selected is None or self.unless_selected not in stages))


class StagePolicy(PolicyModel):
    input: str | None = None
    outputs: tuple[OutputPolicy, ...] = ()


class DatabasePolicy(PolicyModel):
    connect_seconds: PositiveInt
    pool_wait_seconds: Seconds
    pool_idle_seconds: Seconds
    pool_lifetime_seconds: Seconds


class DiagnosticPolicy(PolicyModel):
    file_bytes: PositiveInt
    table_bytes: PositiveInt
    total_bytes: PositiveInt
    table_rows: PositiveInt
    files: PositiveInt
    scanned_files: PositiveInt

    stack_frames: PositiveInt
    threads: PositiveInt
    history_files: PositiveInt
    selection_files: PositiveInt


class BackoffPolicy(PolicyModel):
    initial_seconds: Seconds
    maximum_seconds: Seconds
    multiplier: Annotated[float, Field(ge=1, allow_inf_nan=False)]

    @model_validator(mode="after")
    def ordered(self):
        if self.maximum_seconds < self.initial_seconds:
            raise ValueError("retry maximum must not be less than its initial delay")
        return self

    def delay(self, failures):
        # Cap the exponent before powering; large historical counters stay finite.
        import math
        if self.multiplier == 1:
            return self.initial_seconds
        exponent = min(max(0, failures - 1),
                       math.log(self.maximum_seconds / self.initial_seconds, self.multiplier))
        return min(self.maximum_seconds, self.initial_seconds * self.multiplier ** exponent)


class RecoveryPolicy(PolicyModel):
    normal: BackoffPolicy
    verification: BackoffPolicy


class TelemetryPolicy(PolicyModel):
    queue_records: PositiveInt
    queue_bytes: PositiveInt
    open_traces: PositiveInt


class RuntimePolicy(PolicyModel):
    capacity: CapacityPolicy
    schedule: SchedulePolicy
    buffer_mode: Literal["automatic", "watermarks"]
    buffers: dict[str, Watermarks]
    stages: dict[str, StagePolicy]
    database: DatabasePolicy
    diagnostics: DiagnosticPolicy
    recovery: RecoveryPolicy
    telemetry: TelemetryPolicy
    temporal: TemporalPolicy

    @model_validator(mode="after")
    def references(self):
        if set(self.buffers) != BUFFER_NAMES:
            raise ValueError("buffers must declare the preparation output inventories")
        if set(self.stages) != {"enrich", "score", "tailor", "cover", "pdf", "brief"}:
            raise ValueError("stages must declare the supported preparation stages")
        for stage in self.stages.values():
            if stage.input is not None and stage.input not in self.buffers:
                raise ValueError("unknown input buffer")
            for output in stage.outputs:
                if output.buffer not in self.buffers:
                    raise ValueError("unknown output buffer")
                for condition in (output.when_selected, output.unless_selected):
                    if condition is not None and condition not in self.stages:
                        raise ValueError("unknown output selection condition")
        return self

    def destinations(self, ordered):
        return {name: tuple(o.buffer for o in stage.outputs if o.selected(ordered))
                for name, stage in self.stages.items()}


@lru_cache(maxsize=16)
def _document(path, mtime, size):
    if size > 2_000_000:
        raise ValueError("runtime_policy.yaml is too large")
    value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("runtime_policy.yaml must contain a mapping")
    return value


def _read(path):
    if path.is_symlink():
        raise ValueError("runtime_policy.yaml must not be a symlink")
    info = path.stat()
    return _document(str(path), info.st_mtime_ns, info.st_size)


def _merge(base, updates):
    result = dict(base)
    for key, value in updates.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result


CAPACITY_ENV = {
    "APPLYPILOT_RESOURCE_WORKERS": "workers", "APPLYPILOT_BROWSER_WORKERS": "browsers",
    "APPLYPILOT_RLM_WORKERS": "inference", "APPLYPILOT_PREPARATION_MAX_PENDING": "pending",
    "APPLYPILOT_RECOVERY_WORKERS": "recovery", "APPLYPILOT_DB_POOL_SIZE": "database_connections",
}


def load_policy(root=None, *, workers=None, browsers=None):
    if root is None:
        from applypilot.config import APP_DIR
        root = APP_DIR
    data = _read(Path(__file__).with_name("config") / "runtime_policy.yaml")
    override = Path(root) / "runtime_policy.yaml"
    if override.exists() or override.is_symlink():
        data = _merge(data, _read(override))
    capacity = dict(data["capacity"])
    for env, field in CAPACITY_ENV.items():
        raw = os.getenv(env, "").strip()
        if raw and not (field == "database_connections" and raw == "auto"):
            capacity[field] = int(raw)
    # A concrete CLI/API request wins over inherited environment defaults.
    if workers is not None:
        capacity["workers"] = workers
    if browsers is not None:
        capacity["browsers"] = browsers
    validated = CapacityPolicy.model_validate(capacity)
    values = validated.model_dump()
    for field in ("browsers", "inference", "recovery"):
        if values[field] is None:
            values[field] = values["workers"]
    if values["pending"] is None:
        values["pending"] = max(4, values["workers"] * 2)
    if values["database_connections"] is None:
        # SQL connections are short-lived borrowers, not one per browser/thread.
        values["database_connections"] = min(values["workers"], max(4, (os.cpu_count() or 1) * 2))
    return RuntimePolicy.model_validate({**data, "capacity": values})

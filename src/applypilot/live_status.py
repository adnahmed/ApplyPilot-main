"""Producer-owned PostgreSQL status; readers never inspect runtime files."""
from __future__ import annotations

import json
import math
import os
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from applypilot import postgres

Component = Literal["runtime", "execution", "preparation", "applications", "discovery"]
_initialized: set[tuple[int, str, str]] = set()
_schema_lock = threading.Lock()


class _Payload(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    runtime_id: str = Field(min_length=1)


class RuntimeStatus(_Payload):
    running: bool
    at: str | float
    started_at: str
    workers: list[dict[str, Any]] = Field(default_factory=list)
    worker_activity: list[dict[str, Any]] = Field(default_factory=list)
    services: dict[str, dict[str, Any]] = Field(default_factory=dict)

    @field_validator("started_at")
    @classmethod
    def validate_started_at(cls, value):
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is None or not math.isfinite(stamp.timestamp()):
            raise ValueError("Runtime start requires a finite timestamp with a timezone")
        return value


class StageStatus(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False)
    state: str = "unknown"
    active_tasks: int | None = Field(default=None, ge=0)
    queued_tasks: int | None = Field(default=None, ge=0)
    reason: str | None = None


class ExecutionStatus(_Payload):
    scheduler: str
    stages: dict[str, StageStatus]
    sampled_at: float
    active_tasks: int = Field(ge=0)
    queued_tasks: int = Field(ge=0)


class PreparationStatus(_Payload):
    running: bool
    at: float
    stages: dict[str, StageStatus]
    buffers: dict[str, Any] = Field(default_factory=dict)


class ApplicationStatus(_Payload):
    reported_at: float
    selection_available: bool | None = None
    selectable_jobs: int | None = Field(default=None, ge=0)
    checked_at: float | None = None
    active_tasks: int | None = Field(default=None, ge=0)
    queued_tasks: int | None = Field(default=None, ge=0)
    effective_workers: int | None = Field(default=None, ge=0)


class DiscoveryStatus(_Payload):
    available: bool
    at: str | float
    held: bool
    stages: dict[str, int] = Field(default_factory=dict)


_PAYLOADS = {"runtime": RuntimeStatus, "execution": ExecutionStatus,
             "preparation": PreparationStatus, "applications": ApplicationStatus,
             "discovery": DiscoveryStatus}


class StatusComponent(BaseModel):
    component: Component
    runtime_id: str = Field(min_length=1)
    updated_at: float = Field(allow_inf_nan=False)
    revision: int = Field(ge=1)
    payload: dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self):
        value = _PAYLOADS[self.component].model_validate(self.payload)
        if value.runtime_id != self.runtime_id:
            raise ValueError("Status payload belongs to another runtime")
        self.payload = value.model_dump(mode="json", exclude_unset=True)
        return self


class LiveStatus(BaseModel):
    available: bool = True
    error_type: str | None = None
    runtime_id: str | None = None
    components: dict[Component, StatusComponent] = Field(default_factory=dict)


def _root(root):
    if root is None:
        from applypilot.config import APP_DIR
        root = APP_DIR
    return Path(root).expanduser().resolve()


def _current_runtime():
    from applypilot.execution import current_task
    return os.getenv("APPLYPILOT_RUNTIME_ID") or current_task().get("key")


def _sample_time(component, payload):
    key = {"execution": "sampled_at", "applications": "reported_at"}.get(component, "at")
    value = payload[key]
    if isinstance(value, str):
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("Status timestamp requires a timezone")
        stamp = instant.timestamp()
    else:
        stamp = float(value)
    if not math.isfinite(stamp):
        raise ValueError("Status timestamp must be finite")
    return stamp


def publish(component: Component, payload: dict, *, root=None, runtime_id=None) -> bool:
    """Commit one producer sample and notify listeners without business writes."""
    key = None
    try:
        root = _root(root)
        identity = runtime_id or payload.get("runtime_id") or _current_runtime()
        from applypilot.runtime_info import process_info
        value = _PAYLOADS[component].model_validate({**payload, "runtime_id": identity,
                                                   "process": process_info(component)})
        body = value.model_dump(mode="json", exclude_unset=True)
        sampled_at = _sample_time(component, body)
        with closing(postgres.connect(root / "main.pgstore", timeout=1)) as conn:
            key = (os.getpid(), postgres.server_uri(), conn.schema)
            initialize = key not in _initialized
            if initialize and not _schema_lock.acquire(timeout=1):
                return False
            try:
                # Raw SQL avoids business-data notifications for heartbeat-only writes.
                with conn:
                    if initialize:
                        conn.raw.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema() || '.live_status'))")
                        conn.raw.execute("""CREATE TABLE IF NOT EXISTS live_status (
                            runtime_id TEXT NOT NULL, component TEXT NOT NULL,
                            updated_at DOUBLE PRECISION NOT NULL, revision BIGINT NOT NULL,
                            payload JSONB NOT NULL, PRIMARY KEY (runtime_id, component))""")
                    changed = conn.raw.execute("""INSERT INTO live_status
                        (runtime_id,component,updated_at,revision,payload) VALUES (%s,%s,%s,1,%s::jsonb)
                        ON CONFLICT (runtime_id,component) DO UPDATE SET
                        updated_at=excluded.updated_at, revision=live_status.revision+1, payload=excluded.payload
                        WHERE excluded.updated_at >= live_status.updated_at""",
                        (identity, component, sampled_at, json.dumps(body, allow_nan=False))).rowcount
                    if changed:
                        conn.raw.execute("SELECT pg_notify('applypilot_live', %s)",
                                         (json.dumps({"root": str(root), "schema": conn.schema}),))
                _initialized.add(key)
            finally:
                if initialize:
                    _schema_lock.release()
        return True
    except (postgres.Error, OSError, ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        if key is not None:
            _initialized.discard(key)
        return False


def _read(root, *, runtime_id=None, component=None, latest=False):
    path = _root(root) / "main.pgstore"
    if not path.is_dir():
        return []
    with closing(postgres.connect(path, readonly=True, isolation_level=None, timeout=1)) as conn:
        if not conn.execute("SELECT to_regclass('live_status')").fetchone()[0]:
            return []
        if latest:
            rows = conn.execute("SELECT component,runtime_id,updated_at,revision,payload FROM live_status "
                                "WHERE component=? ORDER BY updated_at DESC,revision DESC LIMIT 1", (component,))
        else:
            rows = conn.execute("SELECT component,runtime_id,updated_at,revision,payload FROM live_status "
                "WHERE runtime_id=COALESCE(?,(SELECT runtime_id FROM live_status WHERE component='runtime' "
                "AND ap_day(payload->>'started_at') IS NOT NULL "
                "ORDER BY ap_day(payload->>'started_at') DESC,updated_at DESC,revision DESC LIMIT 1))", (runtime_id,))
        result = []
        for row in rows:
            try:
                result.append(StatusComponent.model_validate(dict(row)))
            except ValueError:
                continue
        return result


def read_status(root, runtime_id=None) -> LiveStatus:
    """Read only the selected runtime; absent stores remain uninitialized."""
    try:
        records = _read(root, runtime_id=runtime_id)
    except (postgres.Error, OSError, ValueError, TypeError, RuntimeError, OverflowError) as exc:
        return LiveStatus(available=False, error_type=type(exc).__name__, runtime_id=runtime_id)
    identity = records[0].runtime_id if records else runtime_id
    return LiveStatus(runtime_id=identity, components={record.component: record for record in records})


def read_component(root, component: Component, runtime_id=None, *, latest=False) -> dict | None:
    """Read one runtime, or explicitly restore a producer's prior checkpoint."""
    if latest and runtime_id is not None:
        raise ValueError("A latest checkpoint cannot also select a runtime")
    if latest:
        try:
            records = _read(root, component=component, latest=True)
        except (postgres.Error, OSError, ValueError, TypeError, RuntimeError, OverflowError):
            return None
        record = records[0] if records else None
    else:
        record = read_status(root, runtime_id=runtime_id or _current_runtime()).components.get(component)
    return record.payload if record is not None else None

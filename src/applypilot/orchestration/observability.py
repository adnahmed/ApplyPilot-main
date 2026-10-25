"""Read-only Temporal observations. No input/history bodies or task tokens."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from .contracts import ROLES
from .settings import load


def write_snapshot(root, filename, value):
    from applypilot.config import APP_DIR
    if filename not in {"status.json", "install.json"}:
        raise ValueError("Unknown diagnostic snapshot")
    directory = Path(root or APP_DIR) / "temporal"
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Diagnostic directory must not be a symlink")
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / filename)
    finally:
        Path(temporary).unlink(missing_ok=True)


def cached(root=None):
    from applypilot.config import APP_DIR
    path = Path(root or APP_DIR) / "temporal" / "status.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Refusing redirected diagnostic snapshot")
    if not path.exists():
        return {"state": "not_observed", "workflows": [], "queues": [],
                "note": "Run applypilot temporal status to capture native execution state."}
    value = json.loads(path.read_text(encoding="utf-8"))
    return {**value, "cached": True,
            "note": "Last explicit capture, not a live worker heartbeat. Observe the capture timestamp."}


async def collect(*, root=None, client=None):
    from temporalio.api.common.v1 import WorkflowExecution
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import DescribeTaskQueueRequest, DescribeWorkflowExecutionRequest
    from temporalio.api.enums.v1 import TaskQueueType
    from .connection import connect
    policy = load(root)
    client = client or await connect(root)
    prefix = "ap:" + policy.workspace + ":"
    rows = []
    # Visibility is eventually consistent. Never use this view for task admission.
    query = "WorkflowId STARTS_WITH '" + prefix + "'"
    truncated = False
    async for info in client.list_workflows(query=query, page_size=min(100, policy.observation_limit)):
        if len(rows) == policy.observation_limit:
            truncated = True
            break
        row = {"workflow_id": info.id, "run_id": info.run_id, "type": info.workflow_type,
               "status": info.status.name if info.status else "UNKNOWN",
               "start_time": info.start_time.isoformat() if info.start_time else None,
               "close_time": info.close_time.isoformat() if info.close_time else None}
        try:
            desc = await client.workflow_service.describe_workflow_execution(DescribeWorkflowExecutionRequest(
                namespace=policy.namespace, execution=WorkflowExecution(workflow_id=info.id, run_id=info.run_id)))
            row["pending_activities"] = [{"activity_id": item.activity_id,
                "activity_type": item.activity_type.name, "state": int(item.state),
                "attempt": item.attempt, "last_heartbeat_time": item.last_heartbeat_time.ToJsonString()
                    if item.HasField("last_heartbeat_time") else None} for item in desc.pending_activities]
        except Exception as exc:
            # A missing individual execution is reported, not hidden as healthy.
            row["describe_error_type"] = type(exc).__name__
        rows.append(row)
    queues = []
    for role in ROLES:
        kind = TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW if role == "workflow" else TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY
        entry = {"role": role, "task_queue": policy.queue(role)}
        try:
            desc = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=policy.namespace, task_queue=TaskQueue(name=entry["task_queue"]),
                task_queue_type=kind, report_stats=True))
            entry["recent_pollers"] = [{"identity": poller.identity,
                "last_access_time": poller.last_access_time.ToJsonString()} for poller in desc.pollers]
            if desc.HasField("stats"):
                entry["approximate_backlog"] = desc.stats.approximate_backlog_count
                entry["approximate_backlog_age_seconds"] = desc.stats.approximate_backlog_age.ToTimedelta().total_seconds()
                entry["tasks_add_rate"] = desc.stats.tasks_add_rate
                entry["tasks_dispatch_rate"] = desc.stats.tasks_dispatch_rate
            else:
                entry["stats_unavailable"] = True
            entry["scope"] = "native approximate task-queue statistics; workflow sticky-queue tasks are excluded"
        except Exception as exc:
            entry["error_type"] = type(exc).__name__
        queues.append(entry)
    result = {"state": "observed", "captured_at": datetime.now(timezone.utc).isoformat(),
              "namespace": policy.namespace, "workspace": policy.workspace, "workflows": rows,
              "workflows_truncated": truncated, "queues": queues,
              "policy": policy.model_dump(mode="json")}
    from .client import schedule_status
    result['schedules'] = (await schedule_status(root=root))['schedules']
    write_snapshot(root, "status.json", result)
    return result

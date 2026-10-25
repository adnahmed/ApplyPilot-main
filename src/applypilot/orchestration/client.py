"""Explicit deployment/client commands. No scheduler loop and no worker ownership."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, RegisterNamespaceRequest
from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleAlreadyRunningError,
    ScheduleIntervalSpec, ScheduleOverlapPolicy, SchedulePolicy, ScheduleSpec, ScheduleState, ScheduleUpdate)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.service import RPCError, RPCStatusCode
from . import store
from .connection import connect
from .contracts import Command, EntityCommand, SERVICE_PHASES, identity, digest, job_identity
from .settings import load
from .workflows import DispatchWorkflow, JobWorkflow, ServiceWorkflow


def initialize_data():
    from applypilot import config
    from applypilot.database import init_db, get_connection, close_connection
    import os
    config.load_env()
    if not os.getenv("APPLYPILOT_DATABASE_URL"):
        raise ValueError("Set APPLYPILOT_DATABASE_URL before initializing shared Activity data")
    config.ensure_dirs()
    try:
        init_db()
        from applypilot.discovery.schema_migrations import retire_cycle_gate, retire_target_runs
        retire_cycle_gate(get_connection())
        retire_target_runs(get_connection())
        store.initialize(get_connection())
        from .search_store import initialize as initialize_search
        initialize_search(get_connection())
        from applypilot.documents.store import initialize as initialize_documents
        initialize_documents(get_connection())
        from applypilot.reasoning.learning_store import initialize as initialize_learning
        from applypilot.operations.store import initialize as initialize_operations
        initialize_learning()
        initialize_operations()
        from applypilot.integrations.social_actions import initialize as initialize_professional_actions
        initialize_professional_actions(get_connection())
        get_connection().commit()
        from applypilot.integrations.state import SourceState
        source_state = SourceState()
        source_state.initialize()
        source_state.retire_coordination()
        from applypilot.discovery.signal_cache import SignalCache
        SignalCache(config.APP_DIR / 'signals.pgstore').initialize()
    finally:
        close_connection()


async def initialize_namespace(root=None):
    policy = load(root)
    client = await connect(root)
    try:
        await client.workflow_service.describe_namespace(DescribeNamespaceRequest(namespace=policy.namespace))
        return {"namespace": policy.namespace, "created": False}
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    from google.protobuf.duration_pb2 import Duration
    retention = Duration(seconds=policy.history_retention_days * 86400)
    try:
        await client.workflow_service.register_namespace(RegisterNamespaceRequest(
            namespace=policy.namespace, workflow_execution_retention_period=retention,
            description="ApplyPilot durable orchestration"))
    except RPCError as exc:
        if exc.status != RPCStatusCode.ALREADY_EXISTS:
            raise
    return {"namespace": policy.namespace, "created": True}


def capture(options, *, job_url=None, workspace=None):
    from .contracts import validate_request_options
    options = validate_request_options(options, exact=job_url is not None)
    from applypilot.database import get_connection, close_connection
    try:
        conn = get_connection()
        reference = store.put(conn, store.snapshot_options(options))
        if job_url is not None:
            if conn.execute("SELECT 1 FROM jobs WHERE url=?", (job_url,)).fetchone() is None:
                raise ValueError("Choose an existing job URL from this workspace")
            reference = store.put(conn, {"snapshot": reference, "job_url": job_url,
                "job_id": job_identity(workspace, job_url), "workspace": workspace,
                "kind": "apply" if options.get("apply") else "jobs"})
        conn.commit()  # Required before telling Temporal this input exists.
        return reference
    finally:
        close_connection()


def schedule_id(policy, name):
    if name not in policy.schedules:
        raise ValueError("Unknown declarative Schedule")
    return identity(policy.workspace, "schedule", name)


def workflow_for(kind):
    if kind in {"jobs", "apply", "discover", "source_refresh"}:
        return DispatchWorkflow.run
    if kind == "learning":
        from .learning_workflows import LearningWorkflow
        return LearningWorkflow.run
    if kind == "company_discovery":
        from .company_workflows import CompanyDiscoveryWorkflow
        return CompanyDiscoveryWorkflow.run
    if kind == "career_ops":
        from .integration_workflows import ReportsWorkflow
        return ReportsWorkflow.run
    if kind in {"network_prepare", "network_research", "network_delivery", "referrals"}:
        from .integration_workflows import NetworkingWorkflow
        return NetworkingWorkflow.run
    if kind == "email_warmup":
        from .integration_workflows import EmailWarmupWorkflow
        return EmailWarmupWorkflow.run
    if kind == "social":
        from .integration_workflows import SocialWorkflow
        return SocialWorkflow.run
    return ServiceWorkflow.run


def desired_schedule(policy, name, reference, *, apply=False, start=False):
    kind = "apply" if name == "jobs" and apply else name
    workflow = workflow_for(kind)
    cfg = policy.schedules[name]
    return Schedule(
        action=ScheduleActionStartWorkflow(workflow, Command(reference, policy.model_dump(mode="json"), kind),
            id=identity(policy.workspace, "scheduled", name), task_queue=policy.queue("workflow"),
            static_summary="ApplyPilot: " + name),
        spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(seconds=cfg.every_seconds))]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=False,
                              catchup_window=timedelta(seconds=policy.schedule_catchup_seconds)),
        state=ScheduleState(paused=not start or not cfg.enabled,
                            note="ApplyPilot declarative Schedule: " + name))


async def install(options, *, start=False, root=None):
    from .contracts import validate_request_options
    options = validate_request_options(options, recurring=True)
    policy = load(root)
    if options.get('job_phases') is not None:
        policy = type(policy).model_validate({**policy.model_dump(),
            'job_phases': tuple(options['job_phases'])})
    # This client snapshot is deliberately committed separately from Temporal's
    # API. An interrupted install is idempotently reapplied using the same IDs.
    reference = await asyncio.to_thread(capture, options)
    client = await connect(root)
    results = []
    for name, cfg in policy.schedules.items():
        desired = desired_schedule(policy, name, reference, apply=options.get("apply", False), start=start)
        sid = schedule_id(policy, name)
        try:
            await client.create_schedule(sid, desired)
            results.append({"name": name, "id": sid, "action": "created", "paused": desired.state.paused})
        except ScheduleAlreadyRunningError:
            def update(current, desired=desired, enabled=cfg.enabled):
                # Preserve an operator pause on a mere config reapply. Only an
                # explicit --start resumes enabled Schedules. No hidden state DB.
                paused = not enabled or (not start and current.description.schedule.state.paused)
                return ScheduleUpdate(replace(desired, state=replace(desired.state, paused=paused)))
            await client.get_schedule_handle(sid).update(update)
            description = await client.get_schedule_handle(sid).describe()
            results.append({"name": name, "id": sid, "action": "updated", "paused": description.schedule.state.paused})
    from .target_schedules import sync_target_schedules
    targets = await sync_target_schedules(root=root, start=start, trigger=start)
    result = {"namespace": policy.namespace, "workspace": policy.workspace,
              "input_reference": reference, "applications_requested": bool(options.get("apply")), "schedules": results}
    result["targets"] = targets["targets"]
    from .observability import write_snapshot
    write_snapshot(root, "install.json", result)
    return result


async def change_schedules(action, names=(), *, root=None):
    policy = load(root)
    if action not in {"pause", "resume", "trigger"}:
        raise ValueError("Unsupported Schedule action")
    selected = tuple(names) or tuple(policy.schedules)
    # Validate the whole request before doing any remote writes.
    ids = [(name, schedule_id(policy, name)) for name in selected]
    client = await connect(root)
    result = []
    for name, sid in ids:
        handle = client.get_schedule_handle(sid)
        if action == "pause":
            await handle.pause(note="Explicit ApplyPilot client request")
        elif not policy.schedules[name].enabled:
            result.append({"name": name, "action": "disabled_by_declared_policy"})
            continue
        elif action == "resume":
            await handle.unpause(note="Explicit ApplyPilot client request")
        else:
            await handle.trigger()
        result.append({"name": name, "action": action})
    return {"schedules": result, "existing_workflows_cancelled": False}


async def run_job(job_url, options, *, root=None):
    policy = load(root)
    if options.get('job_phases') is not None:
        policy = type(policy).model_validate({**policy.model_dump(), 'job_phases': tuple(options['job_phases'])})
    reference = await asyncio.to_thread(capture, options, job_url=job_url, workspace=policy.workspace)
    client = await connect(root)
    handle = await client.start_workflow(JobWorkflow.run,
        EntityCommand(reference, policy.model_dump(mode="json"), job_identity(policy.workspace, job_url),
                      "apply" if options.get("apply") else "jobs"),
        id=identity(policy.workspace, "job", job_identity(policy.workspace, job_url)), task_queue=policy.queue("workflow"),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    return {"workflow_id": handle.id, "run_id": handle.result_run_id,
            "accepted": True, "completed": False}


async def update_application_pacing(account_id, *, spacing_seconds, hourly_limit, daily_limit,
                                    restriction_retry_seconds=None, root=None):
    from .settings import LinkedInApplicationPolicy
    from .account_workflows import ApplicationAccountWorkflow
    options = {} if restriction_retry_seconds is None else {'restriction_retry_seconds': restriction_retry_seconds}
    pacing = LinkedInApplicationPolicy(account_id=account_id, spacing_seconds=spacing_seconds,
        hourly_limit=hourly_limit, daily_limit=daily_limit, **options).model_dump(exclude={'account_id'})
    if restriction_retry_seconds is None:
        pacing.pop('restriction_retry_seconds')
    policy = load(root)
    client = await connect(root)
    workflow_id = identity(policy.workspace, 'linkedin-applications', account_id)
    result = await client.get_workflow_handle(workflow_id).execute_update(
        ApplicationAccountWorkflow.set_pacing, pacing)
    return {'workflow_id': workflow_id, 'pacing': result, 'usage_history_preserved': True}


async def update_outreach_pacing(account_id, *, spacing_seconds, daily_limit, root=None):
    from .integration_workflows import AccountWorkflow
    policy = load(root)
    client = await connect(root)
    workflow_id = identity(policy.workspace, 'outreach-account', account_id)
    result = await client.get_workflow_handle(workflow_id).execute_update(
        AccountWorkflow.set_pacing, {'spacing_seconds': spacing_seconds, 'daily_limit': daily_limit})
    return {'workflow_id': workflow_id, 'pacing': result, 'usage_history_preserved': True}


def _capture_application_review(policy, job_url, attempt_id, reviewed_by, provider_evidence, adapter_stop_evidence):
    from applypilot.database import get_connection, close_connection
    import re
    if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{16,128}", attempt_id):
        raise ValueError("An exact persisted attempt ID is required")
    for name, value in (("reviewed_by", reviewed_by), ("provider_evidence", provider_evidence),
                        ("adapter_stop_evidence", adapter_stop_evidence)):
        if not isinstance(value, str) or not value.strip() or len(value) > 8000:
            raise ValueError(name + " must be attributable non-empty evidence (at most 8000 characters)")
    application_id = job_identity(policy.workspace, job_url)
    try:
        conn = get_connection()
        row = conn.execute("SELECT * FROM application_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None or row['job_url'] != job_url or row['application_id'] not in (None, application_id):
            raise ValueError("Review attempt does not belong to this workspace application")
        evidence = {"kind": "application_review", "source": "operator_verified",
                    "decision": "confirmed_not_submitted", "application_id": application_id,
                    "attempt_id": attempt_id, "job_url": job_url, "reviewed_by": reviewed_by,
                    "provider_evidence": provider_evidence, "adapter_stopped": True,
                    "adapter_stop_evidence": adapter_stop_evidence}
        reference = store.put(conn, evidence)
        conn.commit()
        return reference
    finally:
        close_connection()


async def record_application_review(job_url, attempt_id, *, reviewed_by, provider_evidence,
                                    adapter_stop_evidence, root=None):
    """Explicit operator authorization, after checking provider absence AND stopping the old adapter.

    A timeout, empty local log, or a missing receipt is not acceptable evidence.
    This commits an immutable reference and signals the existing durable entity;
    it never starts an adapter or creates a new application attempt directly.
    """
    from .workflows import ApplicationWorkflow
    policy = load(root)
    reference = await asyncio.to_thread(_capture_application_review, policy, job_url, attempt_id,
                                         reviewed_by, provider_evidence, adapter_stop_evidence)
    client = await connect(root)
    workflow_id = identity(policy.workspace, "application", job_identity(policy.workspace, job_url))
    await client.get_workflow_handle(workflow_id).signal(ApplicationWorkflow.review_available, reference)
    return {"workflow_id": workflow_id, "evidence_reference": reference, "signalled": True,
            "new_attempt_started_by_client": False}


async def run_mailbox(options=None, *, root=None):
    """Start the same durable account coordinator used by periodic tracking."""
    from .mailbox_workflows import MailboxSyncWorkflow
    policy = load(root)
    options = dict(options or {})
    reference = await asyncio.to_thread(capture, options)
    client = await connect(root)
    handle = await client.start_workflow(MailboxSyncWorkflow.run,
        Command(reference, policy.model_dump(mode="json"), "tracking"),
        id=identity(policy.workspace, "mailbox-sync", "primary"),
        task_queue=policy.queue("workflow"),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    return {"workflow_id": handle.id, "run_id": handle.result_run_id,
            "accepted": True, "completed": False, "dry_run": bool(options.get("dry_run"))}


async def cancel(workflow_id, *, root=None):
    policy = load(root)
    if not workflow_id.startswith("ap:" + policy.workspace + ":"):
        raise ValueError("Workflow ID is outside the selected workspace")
    client = await connect(root)
    await client.get_workflow_handle(workflow_id).cancel()
    return {"workflow_id": workflow_id, "cancellation_requested": True,
            "note": "Cancellation is cooperative; it cannot undo an external submission."}


async def stop_workspace(*, root=None, wait_seconds=30):
    policy = load(root)
    client = await connect(root)
    prefix = 'ap:' + policy.workspace + ':'
    query = "WorkflowId STARTS_WITH '" + prefix + "' AND ExecutionStatus = 'Running'"
    schedules = [item async for item in await client.list_schedules() if item.id.startswith(prefix)]
    for item in schedules:
        await client.get_schedule_handle(item.id).pause(note='Explicit ApplyPilot stop request')
    requested = set()
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while True:
        executions = [item async for item in client.list_workflows(query=query, page_size=100)]
        if not executions:
            break

        async def request_cancel(item):
            key = (item.id, item.run_id)
            if key in requested:
                return
            try:
                await client.get_workflow_handle(item.id, run_id=item.run_id).cancel()
            except RPCError as exc:
                if exc.status not in {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}:
                    raise
            requested.add(key)

        for offset in range(0, len(executions), 25):
            await asyncio.gather(*(request_cancel(item) for item in executions[offset:offset + 25]))
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(.5)
    remaining = (await client.count_workflows(query=query)).count
    terminated = 0
    if remaining:
        from temporalio.api.common.v1 import WorkflowExecution
        from temporalio.api.enums.v1 import PendingActivityState
        from temporalio.api.workflowservice.v1 import DescribeWorkflowExecutionRequest

        async def stop_idle(item):
            nonlocal terminated
            try:
                description = await client.workflow_service.describe_workflow_execution(
                    DescribeWorkflowExecutionRequest(namespace=policy.namespace,
                        execution=WorkflowExecution(workflow_id=item.id, run_id=item.run_id)))
                if any(activity.state != PendingActivityState.PENDING_ACTIVITY_STATE_SCHEDULED
                       for activity in description.pending_activities):
                    return
                await client.get_workflow_handle(item.id, run_id=item.run_id).terminate(
                    reason='Explicit ApplyPilot stop: no executing activities; cooperative cancellation did not finish')
                terminated += 1
            except RPCError as exc:
                if exc.status not in {RPCStatusCode.NOT_FOUND, RPCStatusCode.FAILED_PRECONDITION}:
                    raise

        executions = [item async for item in client.list_workflows(query=query, page_size=100)]
        for offset in range(0, len(executions), 25):
            await asyncio.gather(*(stop_idle(item) for item in executions[offset:offset + 25]))
        remaining = (await client.count_workflows(query=query)).count
    return {'cancellation_requested': len(requested), 'remaining_workflows': remaining,
            'existing_workflows_cancelled': remaining == 0 and terminated == 0,
            'existing_workflows_stopped': remaining == 0, 'stopped': remaining == 0,
            'idle_workflows_terminated': terminated,
            'paused_schedules': len(schedules)}


def capture_search(query, observation_id, providers, *, page=1, category="general", page_size=50):
    from applypilot.database import get_connection, close_connection
    from .search_contracts import SearchRequest
    from . import search_store
    request = SearchRequest(query, page, category, page_size).validate()
    try:
        conn = get_connection()
        reference = search_store.capture(conn, request, observation_id, providers)
        conn.commit()
        return reference
    finally:
        close_connection()


async def run_search(query, *, providers=("searxng", "ddgs"), page=1,
                     category="general", observation_id=None, root=None):
    import uuid
    from .search_contracts import PROVIDERS, SearchCommand
    from .search_workflows import SearchWorkflow
    from temporalio.exceptions import WorkflowAlreadyStartedError
    if not providers or len(set(providers)) != len(providers) or set(providers) - set(PROVIDERS):
        raise ValueError("Select unique supported search providers")
    policy = load(root)
    # Reusing an observation_id resumes the same observation. An explicit new
    # invocation without it is fresh research, not a private cooldown decision.
    observation = observation_id or digest(uuid.uuid4().hex)
    reference = await asyncio.to_thread(capture_search, query, observation, providers, page=page, category=category)
    cfg = policy.activities["research"]
    command = SearchCommand(reference, observation, policy.workspace,
        policy.queue("workflow"), policy.queue("research"), tuple(providers),
        cfg.start_to_close_seconds).validate()
    client = await connect(root)
    workflow_id = identity(policy.workspace, "search", observation)
    try:
        handle = await client.start_workflow(SearchWorkflow.run, command,
            id=workflow_id, task_queue=policy.queue("workflow"),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
    except WorkflowAlreadyStartedError:
        # The database has already verified identical query/provider semantics.
        handle = client.get_workflow_handle(workflow_id)
    return {"workflow_id": handle.id, "observation_id": observation,
            "request_id": reference, "accepted": True, "completion_checked": False}


def initialize_search_data():
    """Explicit search-only deployment DDL; never starts business schedules."""
    import os
    from applypilot.config import load_env, DB_PATH
    from applypilot.database import get_connection, close_connection
    from applypilot.postgres import initialize_store
    from . import search_store
    load_env()
    if not os.environ.get('APPLYPILOT_DATABASE_URL'):
        raise ValueError('Search deployment requires an explicit shared PostgreSQL URI')
    initialize_store(DB_PATH)
    try:
        conn = get_connection()
        store.initialize(conn)
        search_store.initialize(conn)
    finally:
        close_connection()
    return {'initialized': ['temporal_inputs', 'temporal_publication_receipts',
                            'search_observations', 'search_evidence'],
            'application_schedules_changed': False}


async def run_signal(signal_id, *, root=None):
    """Resume one captured signal without invoking the discovery scheduler."""
    from applypilot.signals.contracts import SignalCommand, SignalInput
    from .signal_workflows import SignalWorkflow
    from temporalio.exceptions import WorkflowAlreadyStartedError
    SignalInput(signal_id).validate()
    policy = load(root)
    model = policy.activities['models']
    publish = policy.activities['discovery']
    command = SignalCommand(signal_id, policy.queue('models'), policy.queue('discovery'),
        model.start_to_close_seconds, publish.start_to_close_seconds,
        min(model.heartbeat_seconds, publish.heartbeat_seconds),
        min(model.heartbeat_interval_seconds, publish.heartbeat_interval_seconds)).validate()
    client = await connect(root)
    workflow_id = identity(policy.workspace, 'signal', signal_id)
    try:
        handle = await client.start_workflow(SignalWorkflow.run, command,
            id=workflow_id, task_queue=policy.queue('workflow'),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY)
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id)
    return {'workflow_id': handle.id, 'signal_id': signal_id, 'accepted': True, 'completed': False}


async def run_work(kind, options=None, *, root=None):
    """Start a durable core business operation and return after server acceptance."""
    from .contracts import CORE_KINDS
    if kind not in CORE_KINDS:
        raise ValueError('Unknown core operation: ' + str(kind))
    from .contracts import validate_request_options
    options = validate_request_options(options or {})
    if options.get('limit') is not None and kind not in {'jobs', 'apply', 'discover', 'source_refresh'}:
        raise ValueError('limit is supported only by finite native dispatch requests')
    if any(options.get(key) is not None for key in ('target_ids', 'source_request_keys')) and kind not in {'discover', 'source_refresh'}:
        raise ValueError('Source/target selection requires discovery or source_refresh')
    policy = load(root)
    selected = options.get('job_phases')
    if selected is not None:
        policy = policy.model_copy(update={'job_phases': tuple(selected)})
        policy = type(policy).model_validate(policy.model_dump())
    capture_operation = capture
    if kind.startswith("backup"):
        from .backup_domain import capture as capture_operation
    reference = await asyncio.to_thread(capture_operation, options)
    client = await connect(root)
    implementation = workflow_for(kind)
    handle = await client.start_workflow(implementation,
        Command(reference, policy.model_dump(mode='json'), kind),
        id=identity(policy.workspace, 'command', kind + ':' + reference), task_queue=policy.queue('workflow'),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    return {'workflow_id': handle.id, 'run_id': handle.result_run_id,
            'accepted': True, 'completed': False, 'execution_owner': 'Temporal'}


async def schedule_status(*, root=None, client=None):
    """Describe declared Schedules directly. Missing deployment is not running work."""
    policy = load(root)
    client = client or await connect(root)
    rows = []
    for name in policy.schedules:
        sid = schedule_id(policy, name)
        try:
            description = await client.get_schedule_handle(sid).describe()
            rows.append({'name': name, 'id': sid, 'installed': True,
                         'paused': description.schedule.state.paused})
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            rows.append({'name': name, 'id': sid, 'installed': False, 'paused': True})
    prefix = 'ap:' + policy.workspace + ':'
    count = await client.count_workflows(query="WorkflowId STARTS_WITH '" + prefix + "' AND ExecutionStatus = 'Running'")
    return {'namespace': policy.namespace, 'workspace': policy.workspace, 'schedules': rows,
            'open_workflows': count.count}


async def install_backup_schedules(*, enabled: bool, root=None):
    """Native Schedule upserts after explicit backup configuration/consent."""
    from .backup_domain import capture as capture_backup
    policy = load(root)
    reference = await asyncio.to_thread(capture_backup, {"automatic": True})
    client = await connect(root)
    results = []
    for name in ("backup", "backup_full"):
        desired = desired_schedule(policy, name, reference, start=enabled)
        sid = schedule_id(policy, name)
        try:
            await client.create_schedule(sid, desired)
        except ScheduleAlreadyRunningError:
            await client.get_schedule_handle(sid).update(
                lambda current, desired=desired: ScheduleUpdate(desired))
        results.append({"name": name, "id": sid, "paused": desired.state.paused})
    return {"schedules": results, "existing_workflows_cancelled": False}


async def work_status(workflow_id, *, run_id=None, root=None):
    """Exact execution read. No local worker registry or inferred idle status."""
    policy = load(root)
    if not isinstance(workflow_id, str) or not workflow_id.startswith("ap:" + policy.workspace + ":"):
        raise ValueError("Workflow ID is outside this workspace")
    client = await connect(root)
    handle = client.get_workflow_handle(workflow_id, run_id=run_id)
    description = await handle.describe()
    return {"workflow_id": description.id, "run_id": description.run_id,
            "status": description.status.name if description.status else "UNKNOWN",
            "type": description.workflow_type,
            "start_time": description.start_time.isoformat() if description.start_time else None,
            "close_time": description.close_time.isoformat() if description.close_time else None}

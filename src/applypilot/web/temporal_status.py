"""Read-only worker heartbeats and queue observations for the dashboard."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import logging
import threading
import time

from applypilot.orchestration.connection import connect
from applypilot.orchestration.settings import load


LABELS = {
    'preparation': 'Job preparation', 'discovery': 'Job discovery',
    'research': 'Company & web research', 'models': 'AI analysis',
    'documents': 'Application documents', 'applications': 'Applications',
    'learning': 'Learning from outcomes', 'network': 'Networking',
    'email': 'Inbox & replies', 'social': 'Social activity',
    'reporting': 'Reports & interview preparation', 'maintenance': 'Planning & upkeep',
    'browser': 'Browser work', 'egress': 'Connection checks',
}
_lock = threading.Lock()
_cache = {}
log = logging.getLogger(__name__)


async def collect(root, *, client=None):
    from datetime import timedelta

    from temporalio.api.enums.v1 import TaskQueueType, WorkerStatus
    from temporalio.api.taskqueue.v1 import TaskQueue
    from temporalio.api.workflowservice.v1 import (
        DescribeTaskQueueRequest, DescribeWorkerRequest, ListWorkersRequest,
    )

    policy = load(root)
    client = client or await connect(root)
    from applypilot.orchestration.client import schedule_status
    from applypilot.control import _view

    roles = {policy.queue(role): role for role in LABELS}
    heartbeats = {role: [] for role in LABELS}
    detail_errors = set()
    worker_error = False
    truncated = False
    describing = asyncio.Semaphore(8)
    rpc_timeout = timedelta(seconds=3)

    def record(heartbeat):
        role = roles.get(heartbeat.task_queue)
        if role and heartbeat.status == WorkerStatus.WORKER_STATUS_RUNNING:
            heartbeats[role].append(heartbeat)

    async def describe_worker(worker):
        role = roles.get(worker.task_queue)
        if not role or worker.status != WorkerStatus.WORKER_STATUS_RUNNING:
            return
        try:
            async with describing:
                response = await client.workflow_service.describe_worker(DescribeWorkerRequest(
                    namespace=policy.namespace, worker_instance_key=worker.worker_instance_key),
                    timeout=rpc_timeout)
            if not response.HasField('worker_info') or not response.worker_info.HasField('worker_heartbeat'):
                detail_errors.add(role)
                return
            record(response.worker_info.worker_heartbeat)
        except Exception as exc:
            detail_errors.add(role)
            log.warning('Worker details unavailable for %s (%s)', role, type(exc).__name__)

    async def read_workers():
        nonlocal worker_error, truncated
        token = b''
        try:
            for _ in range(10):
                response = await client.workflow_service.list_workers(ListWorkersRequest(
                    namespace=policy.namespace, page_size=100, next_page_token=token),
                    timeout=rpc_timeout)
                if response.workers:
                    await asyncio.gather(*(describe_worker(worker) for worker in response.workers))
                else:
                    for info in response.workers_info:
                        record(info.worker_heartbeat)
                token = response.next_page_token
                if not token:
                    return
            truncated = True
        except Exception as exc:
            worker_error = True
            log.warning('Worker listing unavailable (%s)', type(exc).__name__)

    async def read_queue(role):
        try:
            response = await client.workflow_service.describe_task_queue(DescribeTaskQueueRequest(
                namespace=policy.namespace, task_queue=TaskQueue(name=policy.queue(role)),
                task_queue_type=TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY, report_stats=True),
                timeout=rpc_timeout)
            return response
        except Exception as exc:
            log.warning('Queue observation unavailable for %s (%s)', role, type(exc).__name__)
            return None

    control, _, *backlogs = await asyncio.gather(schedule_status(root=root, client=client),
        read_workers(), *(read_queue(role) for role in LABELS))
    now = datetime.now(timezone.utc)
    queues = []

    def is_current(heartbeat):
        return (heartbeat.HasField('heartbeat_time') and
                -5 <= now.timestamp() - heartbeat.heartbeat_time.ToDatetime(timezone.utc).timestamp() <= 20)

    for (role, label), response in zip(LABELS.items(), backlogs):
        current = [heartbeat for heartbeat in heartbeats[role] if is_current(heartbeat)]
        complete = not worker_error and not truncated and role not in detail_errors
        known = (bool(current) and all(h.HasField('activity_task_slots_info') for h in current)
                 and complete)
        slots = [heartbeat.activity_task_slots_info for heartbeat in current]
        active = sum(slot.current_used_slots for slot in slots) if known else None
        free = (sum(slot.current_available_slots for slot in slots)
                if known and all(slot.current_available_slots >= 0 for slot in slots) else None)
        at = max((h.heartbeat_time.ToDatetime(timezone.utc) for h in current
                  if h.HasField('heartbeat_time')), default=None)
        stats = response.stats if response is not None and response.HasField('stats') else None
        queued = stats.approximate_backlog_count if stats is not None else None
        polls = [poller.last_access_time.ToDatetime(timezone.utc) for poller in response.pollers
                 if poller.HasField('last_access_time')] if response is not None else []
        last_poll = max(polls, default=None)
        recent_pollers = sum(-5 <= (now - polled_at).total_seconds() <= 150 for polled_at in polls)
        if active:
            state, reason = 'running', 'Tasks are in progress; some may be waiting for a service response.'
        elif not complete:
            state, reason = 'unverified', 'Worker status could not be fully read. Retrying automatically.'
        elif active is None:
            if recent_pollers:
                state, reason = 'unverified', 'A worker connected recently; waiting for its current activity report.'
            else:
                state, reason = 'offline', 'No worker is reporting for this activity. Start the local app to process queued work.'
        elif queued:
            if stats.rate_limiting_active:
                state, reason = 'queued', 'Work is waiting for the configured processing rate.'
            else:
                state, reason = 'queued', 'Work is queued for the next available worker.'
        elif queued is None:
            state, reason = 'unverified', 'Worker is connected; the waiting task count is temporarily unavailable.'
        else:
            state, reason = 'idle', 'Ready for the next task.'
        queues.append({'role': role, 'label': label, 'state': state, 'reason': reason,
                       'active': active, 'queued': queued, 'available_slots': free,
                       'worker_count': len(current) if complete else None,
                       'recent_pollers': recent_pollers if response is not None else None,
                       'last_poll_at': last_poll.isoformat() if last_poll else None,
                       'queued_age_seconds': (stats.approximate_backlog_age.ToTimedelta().total_seconds()
                                              if stats is not None and stats.HasField('approximate_backlog_age') else None),
                       'tasks_add_rate': stats.tasks_add_rate if stats is not None else None,
                       'tasks_dispatch_rate': stats.tasks_dispatch_rate if stats is not None else None,
                       'rate_limiting_active': stats.rate_limiting_active if stats is not None else None,
                       'processed_tasks': sum(slot.total_processed_tasks for slot in slots) if known else None,
                       'failed_tasks': sum(slot.total_failed_tasks for slot in slots) if known else None,
                       'interval_processed_tasks': sum(slot.last_interval_processed_tasks for slot in slots) if known else None,
                       'interval_failed_tasks': sum(slot.last_interval_failure_tasks for slot in slots) if known else None,
                       'reported_at': at.isoformat() if at else None})
    counts_known = all(row['active'] is not None for row in queues)
    queues_known = all(row['queued'] is not None for row in queues)
    warnings = []
    if worker_error or detail_errors:
        warnings.append('Some worker details could not be read. Reconnecting automatically.')
    if truncated:
        warnings.append('The worker list is incomplete; current totals are unavailable.')
    if not counts_known:
        warnings.append('Some activities have no current worker report.')
    if not queues_known:
        warnings.append('Some queue totals could not be read. Their counts are shown as unknown.')
    return {'available': True, 'captured_at': now.isoformat(), 'queues': queues, 'control': _view(control),
            'in_progress': sum(row['active'] for row in queues) if counts_known else None,
            'queued': sum(row['queued'] for row in queues) if queues_known else None,
            'warnings': warnings}


def _unavailable():
    return {'available': False, 'captured_at': None, 'queues': [],
            'in_progress': None, 'queued': None,
            'control': {'state': 'unavailable', 'message': 'Waiting for current automation status…'},
            'warnings': ['Current work status is unavailable. Updates will resume automatically.']}


def _store(root, value):
    with _lock:
        _cache[str(Path(root).resolve())] = (time.monotonic(), value)
    return deepcopy(value)


def unavailable(root):
    return _store(root, _unavailable())


async def refresh(root, *, client=None):
    """Refresh the shared observation; only the dashboard producer calls this."""
    try:
        async with asyncio.timeout(10):
            value = await collect(root, client=client)
    except Exception as exc:
        log.warning("Current worker observation failed (%s)", type(exc).__name__)
        return unavailable(root)
    return _store(root, value)


def snapshot(root):
    """HTTP readers share the producer's sample and never issue Temporal RPCs."""
    with _lock:
        cached = _cache.get(str(Path(root).resolve()))
        if cached and time.monotonic() - cached[0] < 20:
            return deepcopy(cached[1])
    return _unavailable()


def workers(root, runtime):
    from .worker_history import history

    observed = runtime['temporal']
    return {'execution_owner': 'Temporal', 'automation': observed, 'rows': [],
            'runtime_current': observed['available'], 'at': observed['captured_at'],
            'history': history(root), 'counts': {}, 'warnings': [],
            'pipeline': {'native': True, **observed}}

"""Capability worker bootstrap. SDK polling/matching owns distributed capacity."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack, ExitStack
from datetime import timedelta
from importlib import import_module
import logging
import os
import signal

from temporalio.worker import Worker, PollerBehaviorAutoscaling
from temporalio.runtime import Runtime, TelemetryConfig
from .activities import Activities
from .connection import connect
from .contracts import ROLES
from .settings import load
from .workflows import WORKFLOWS
from .search_workflows import SEARCH_WORKFLOWS
from .document_workflows import DOCUMENT_WORKFLOWS
from .signal_workflows import SIGNAL_WORKFLOWS
from .mailbox_workflows import MAILBOX_WORKFLOWS
from .account_workflows import ACCOUNT_WORKFLOWS
from .learning_workflows import LEARNING_WORKFLOWS
from .company_workflows import COMPANY_WORKFLOWS
from .integration_workflows import INTEGRATION_WORKFLOWS
from .utility_workflows import UTILITY_WORKFLOWS
from .target_workflows import TARGET_WORKFLOWS


def workflow_types():
    return [*WORKFLOWS, *SEARCH_WORKFLOWS, *DOCUMENT_WORKFLOWS, *SIGNAL_WORKFLOWS, *MAILBOX_WORKFLOWS, *ACCOUNT_WORKFLOWS, *LEARNING_WORKFLOWS, *COMPANY_WORKFLOWS, *INTEGRATION_WORKFLOWS, *UTILITY_WORKFLOWS, *TARGET_WORKFLOWS]


def activities_for(role):
    if role == "documents":
        from .document_activities import DOCUMENT_ACTIVITIES
        registered = [*DOCUMENT_ACTIVITIES, Activities(role).operation]
    elif role == "models":
        from .signal_activities import extract_signal
        registered = [extract_signal, Activities(role).operation]
    elif role == "research":
        from .search_activities import SEARCH_ACTIVITIES
        registered = list(SEARCH_ACTIVITIES)
    else:
        bound = Activities(role)
        registered = [bound.operation] + ([bound.plan] if role == "maintenance" else [])
    if role == 'discovery':
        from .signal_activities import publish_signal
        from .source_activities import fetch_source_page
        registered.extend([publish_signal, fetch_source_page])
    from .company_activities import COMPANY_ACTIVITIES_BY_ROLE
    registered.extend(COMPANY_ACTIVITIES_BY_ROLE.get(role, ()))
    from .integration_activities import ACTIVITIES_BY_ROLE
    registered.extend(ACTIVITIES_BY_ROLE.get(role, ()))
    from .utility_activities import UTILITY_ACTIVITIES_BY_ROLE
    registered.extend(UTILITY_ACTIVITIES_BY_ROLE.get(role, ()))
    from .target_activities import TARGET_ACTIVITIES_BY_ROLE
    registered.extend(TARGET_ACTIVITIES_BY_ROLE.get(role, ()))
    return registered


def _deployment():
    name = os.getenv("APPLYPILOT_TEMPORAL_DEPLOYMENT")
    build = os.getenv("APPLYPILOT_TEMPORAL_BUILD")
    if bool(name) != bool(build):
        raise ValueError("Set both Temporal deployment name and build version")
    if not name:
        return None
    from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
    from temporalio.worker import WorkerDeploymentConfig
    return WorkerDeploymentConfig(version=WorkerDeploymentVersion(name, build),
        use_worker_versioning=True, default_versioning_behavior=VersioningBehavior.PINNED)


async def serve(roles, *, stop=None, plugins=(), root=None):
    from applypilot import config
    config.load_env()
    policy = load(root)
    roles = tuple(dict.fromkeys(roles))
    if not roles or any(role not in ROLES for role in roles):
        raise ValueError("Select at least one known worker role")
    if "applications" in roles and len(roles) != 1:
        raise ValueError("Run applications in a dedicated Temporal worker process for session isolation")
    if "applications" in roles and policy.workers["applications"].activities != 1:
        raise ValueError("The current browser adapter supports one applications Activity per process")
    if "workflow" not in roles or len(roles) > 1:
        # No remote worker may silently create its own empty embedded database.
        if not os.getenv("APPLYPILOT_DATABASE_URL"):
            raise ValueError("Activity workers require APPLYPILOT_DATABASE_URL for the shared PostgreSQL store")
    if root is not None and config.APP_DIR.resolve() != root.resolve():
        raise ValueError("APPLYPILOT_DIR must be set before importing the application")
    if any(role != 'workflow' for role in roles):
        numpy_version = import_module('numpy').__version__
        import_module('applypilot.reasoning.dspy_runtime')
        logging.getLogger(__name__).info('Reasoning libraries ready (NumPy %s)', numpy_version)
    # Schema migration is explicit (`temporal initialize`), not worker startup.
    runtime = Runtime(telemetry=TelemetryConfig(), worker_heartbeat_interval=timedelta(seconds=5))
    client = await connect(root, plugins=plugins, runtime=runtime)
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    fallback_signals = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            try:
                fallback_signals[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            except ValueError:
                fallback_signals.pop(sig, None)
    deployment = _deployment()
    try:
        with ExitStack() as pools:
            async with AsyncExitStack() as workers:
                running = []
                try:
                    for role in roles:
                        cfg = policy.workers[role]
                        args = dict(task_queue=policy.queue(role),
                            graceful_shutdown_timeout=timedelta(seconds=cfg.shutdown_seconds),
                            disable_eager_activity_execution=True,
                            deployment_config=deployment,
                            workflow_task_poller_behavior=PollerBehaviorAutoscaling(),
                            activity_task_poller_behavior=PollerBehaviorAutoscaling())
                        if role == "workflow":
                            args["workflows"] = workflow_types()
                            args["max_concurrent_workflow_tasks"] = policy.workflow_tasks_per_worker
                        else:
                            args["activities"] = activities_for(role)
                            args["activity_executor"] = pools.enter_context(ThreadPoolExecutor(
                                max_workers=cfg.activities, thread_name_prefix="temporal-" + role))
                            args["max_concurrent_activities"] = cfg.activities
                            args["max_activities_per_second"] = cfg.max_activities_per_second
                            args["max_task_queue_activities_per_second"] = cfg.max_task_queue_activities_per_second
                        running.append(await workers.enter_async_context(Worker(client, **args)))
                    await stop.wait()
                finally:
                    await asyncio.gather(*(worker.shutdown() for worker in running))
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        for sig, previous in fallback_signals.items():
            signal.signal(sig, previous)

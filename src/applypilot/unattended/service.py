"""Compatibility command: install native Schedules, never an OS owner loop.

Existing user-created systemd/launchd units are not silently edited or removed.
Disable old autopilot units explicitly during deployment; worker processes are
managed independently and stopping one does not cancel durable Workflows.
"""
from __future__ import annotations

import asyncio
from .settings import load


def install(*, start=False):
    if not load()['enabled']:
        raise ValueError('Run unattended setup --enable first')
    from applypilot.orchestration.client import install as install_schedules
    result = asyncio.run(install_schedules({'apply': False}, start=start))
    return {**result, 'execution_owner': 'Temporal', 'os_service_created': False,
            'existing_os_services_modified': False,
            'note': 'Disable any previously installed autopilot OS service explicitly. '
                    'Run native Temporal workers as separately managed processes.'}

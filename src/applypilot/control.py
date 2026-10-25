"""Authenticated business control requests backed directly by Temporal.

Pause prevents future Schedule actions. Stop also cancels existing work in the
selected workspace. Individual cancellation accepts an exact Workflow ID.
Saved options are business configuration, never execution ownership.
"""
from __future__ import annotations
import asyncio
from contextlib import contextmanager
from pathlib import Path
from applypilot.onboarding.storage import read_json

OPTIONS = "autopilot-options.json"


def _view(observation):
    schedules = observation.get('schedules', [])
    installed = any(row.get('installed', True) for row in schedules)
    running = any(row.get('installed', True) and not row.get('paused', True) for row in schedules)
    stopped = not running and (not installed or observation.get('open_workflows') == 0)
    return {**observation, 'state': 'running' if running else 'stopped' if stopped else 'paused',
            'managed': installed, 'active': None, 'runtime_reported': False,
            'desired': 'running' if running else 'stopped' if stopped else 'paused', 'maintenance': False,
            'can_start': not running, 'can_pause': installed, 'can_stop': installed,
            'lifetime': 'Temporal', 'execution_owner': 'Temporal',
            'message': ('Autopilot is enabled. Open Workers to see current work.' if running else
                        'Autopilot stopped.' if stopped and installed else
                        'Schedules paused. Existing workflows still process queued tasks and retries. Stop cancels them.' if installed else
                        'Start autopilot to enable scheduled work.'),
            'workflow_activity_unknown': True}


def status(root: Path, *, heartbeat=None, live=False) -> dict:
    if live:
        from applypilot.orchestration.client import schedule_status
        return _view(asyncio.run(schedule_status(root=Path(root))))
    from applypilot.orchestration.observability import cached
    observation = cached(Path(root))
    # Cached diagnostics never assert that remote workers are idle or active.
    result = _view(observation)
    result.update(state='not_observed' if not observation.get('schedules') else result['state'],
                  cached=True, workflow_activity_unknown=True)
    return result


async def request(root: Path, data: dict) -> dict:
    from applypilot.orchestration import client
    command = data.get('action')
    stopped = None
    if command == 'cancel':
        return await client.cancel(data.get('workflow_id', ''), root=root)
    if command in {'start', 'resume', 'restart'}:
        options = read_json(root / OPTIONS, {})
        options = {key: value for key, value in options.items() if key in {
            'min_score', 'max_age_days', 'sources', 'doc_format', 'track_days',
            'browser_runtime', 'dry_run', 'apply'}}
        options.setdefault('apply', False)
        await client.install(options, start=True, root=root)
    elif command == 'stop':
        wait = data.get('wait_seconds', 30)
        if type(wait) is not int or not 0 <= wait <= 3600:
            raise ValueError('Stop wait must be a whole number within 0..3600 seconds')
        stopped = await client.stop_workspace(root=root, wait_seconds=wait)
    elif command == 'pause':
        await client.change_schedules('pause', root=root)
    else:
        raise ValueError('Choose start, pause, resume, stop, or cancel with a Workflow ID')
    result = _view(await client.schedule_status(root=root))
    result['existing_workflows_cancelled'] = False
    if stopped is not None:
        result.update(stopped, state='stopped' if stopped['stopped'] else 'stopping',
                      message='Autopilot stopped.' if stopped['stopped'] else
                      f"Stopping existing work: {stopped['remaining_workflows']} operations are finishing cancellation.")
    return result


def action(root: Path, data: dict, *, dashboard_owner=None) -> dict:
    return asyncio.run(request(Path(root), data))


def stop(root: Path, *, timeout=300) -> dict:
    return action(root, {'action': 'stop', 'wait_seconds': timeout})


@contextmanager
def maintenance(root: Path, progress=lambda **kw: None, timeout=300):
    """Configuration edit only; immutable Workflow snapshots remain unchanged.

    This is NOT an idle/worker-ownership assertion. Destructive reset/restore
    must be performed offline after operators stop all Activity deployments.
    """
    yield


def require_offline_maintenance(root: Path):
    """Explicit operator attestation plus a database-idle guard, not a lease.

    Local PID files and cached visibility cannot prove remote deployments are
    stopped. Recovery/reset remains an offline utility, never a web job.
    """
    import os
    if os.environ.get("APPLYPILOT_OFFLINE_MAINTENANCE") != "1":
        raise RuntimeError(
            "Offline maintenance required: pause Temporal Schedules, resolve/cancel "
            "affected Workflows, stop every Activity deployment and close dashboards. "
            "Only after doing that set APPLYPILOT_OFFLINE_MAINTENANCE=1 in the recovery shell. "
            "This is operator attestation, not automatic cancellation or a remote-idle proof.")
    from applypilot import postgres
    postgres.require_idle()


if __name__ == '__main__':
    from applypilot.orchestration.cli import app
    app()

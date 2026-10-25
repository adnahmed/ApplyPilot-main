"""Bounded owner-triggered activation of the existing local gateway service."""
from __future__ import annotations

import shutil
import subprocess
import threading
import time
import uuid
from applypilot.operations.runtime import exception_details, sanitize, stderr_details

_lock = threading.Lock()
_tasks = {}


def start(app_dir, data):
    from .provider_health import gateway_root
    from .runtime import read_runtime
    if set(data) != {'confirm'} or data.get('confirm') is not True:
        raise ValueError('Confirm reloading the local LiteLLM gateway')
    if read_runtime(app_dir).get('running'):
        raise ValueError('Stop Autopilot and let its workers finish before reloading the gateway')
    executable = shutil.which('docker')
    compose = gateway_root() / 'compose.yaml'
    if not executable or not compose.is_file():
        raise ValueError('The local Docker gateway configuration is unavailable')
    with _lock:
        if any(task['state'] == 'running' for task in _tasks.values()):
            raise ValueError('A gateway reload is already running')
        for key in list(_tasks):
            if time.time() - _tasks[key]['started_at'] > 3600:
                del _tasks[key]
        identifier = uuid.uuid4().hex
        task = {'id': identifier, 'state': 'running', 'started_at': time.time(),
                'message': 'Loading saved credentials and gateway code…'}
        _tasks[identifier] = task
        try:
            threading.Thread(target=_reload, args=(identifier, executable, compose),
                             name='gateway-reload', daemon=True).start()
        except Exception as exc:
            from .provider_health import diagnostic_secrets
            task.update(state='failed', ok=False,
                        message=sanitize(f'{type(exc).__name__}: {exc}', 4000, secrets=diagnostic_secrets(compose.parent)))
        return dict(task)


def status(identifier):
    with _lock:
        if identifier not in _tasks:
            raise LookupError('Gateway reload record is unavailable; inspect the gateway before retrying')
        return dict(_tasks[identifier])


def _reload(identifier, executable, compose):
    from applypilot.processes import process_guard
    secrets = {}
    try:
        from .provider_health import diagnostic_secrets
        secrets = diagnostic_secrets(compose.parent)
        with process_guard(compose.parent / 'runtime' / 'provider-errors' / 'gateway-reload.lock'):
            result = subprocess.run([executable, 'compose', '-f', str(compose), 'up', '-d',
                                     '--no-deps', '--force-recreate', 'litellm'],
                                    cwd=compose.parent, capture_output=True, timeout=120,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        ok = result.returncode == 0
        details = {'return_code': result.returncode, **stderr_details(result.stderr or b'', secrets=secrets)}
        output = details['stderr']
        if not ok and not output and result.stdout:
            captured = stderr_details(result.stdout, secrets=secrets)
            output = captured['stderr']
            details.update(stdout=output, stdout_bytes=captured['stderr_bytes'], stdout_truncated=captured['stderr_truncated'])
        value = {'state': 'completed' if ok else 'failed', 'ok': ok,
                 'message': ('Gateway recreated with the saved settings. Check a model to observe provider health.' if ok else
                             f'Gateway reload exited with code {result.returncode}: ' + (output or 'No process output was captured.')),
                 'error_details': details if not ok else None}
    except subprocess.TimeoutExpired as exc:
        details = exception_details(exc, secrets=secrets)
        output = stderr_details(exc.stderr or exc.stdout or b'', secrets=secrets)['stderr']
        value = {'state': 'failed', 'ok': False,
                 'message': f'Gateway reload exceeded its {exc.timeout}-second deadline.' + (' ' + output if output else ''),
                 'error_details': details}
    except Exception as exc:
        value = {'state': 'failed', 'ok': False,
                 'message': sanitize(f'{type(exc).__name__}: {exc}', 4000, secrets=secrets),
                 'error_details': exception_details(exc, secrets=secrets)}
    with _lock:
        _tasks[identifier].update(value)

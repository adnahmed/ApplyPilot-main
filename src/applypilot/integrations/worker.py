"""Isolated SDK runner: avoids JobSpy's markdownify pin and browser conflicts."""
from __future__ import annotations

from applypilot import operations as ops

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from .types import SourceError

PACKAGES = {
    "ddgs": "ddgs>=9,<10",
    "playwright": "playwright>=1.40,<2",
    "twikit": "twikit>=2.3,<3",
    "linkedin_scraper": "linkedin-scraper>=3.1.2,<4",
}


def python_for(provider):
    from applypilot.config import APP_DIR
    if provider not in PACKAGES:
        raise SourceError("Unknown SDK worker")
    env = "APPLYPILOT_" + provider.upper() + "_PYTHON"
    executable = os.environ.get(env)
    if executable:
        path = shutil.which(executable) or executable
    else:
        path = APP_DIR / "tools" / provider / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not Path(path).is_file():
        raise SourceError(f"{provider} is not installed. Run scripts/free-sources/install.py {provider}, or set {env}.", "unavailable")
    return str(path)


def minimal_env(passthrough=()):
    names = {"PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG",
             "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "LC_ALL", "DISPLAY", "XDG_RUNTIME_DIR", "LOCALAPPDATA", "APPDATA", "PLAYWRIGHT_BROWSERS_PATH"}
    names.update(passthrough)
    env = {key: os.environ[key] for key in names if key in os.environ}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    return env


def run_json(argv, payload=None, *, timeout=90, env=None, marker=None, interruptible=True):
    """Own the subprocess through completion/cancellation; deadlines are optional."""
    from applypilot.execution import ExecutionStopped, current_task
    from applypilot.operations.runtime import diagnostic_details, stderr_details
    from applypilot.work_context import stage_context

    stop_events = (current_task().get("stop_event"),
                   stage_context().get("stop_event"))
    child_env = env if env is not None else minimal_env()
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = None
            started = time.monotonic()
            try:
                if any(event is not None and event.is_set() for event in stop_events):
                    raise ExecutionStopped("Execution stopped before connector start")
                process = subprocess.Popen(argv, stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
                                           stdout=out, stderr=err, start_new_session=os.name != "nt", env=child_env)
                try:
                    pending_input = json.dumps(payload).encode() if payload is not None else None
                    while True:
                        if process.poll() is not None:
                            process.communicate(pending_input)
                            break
                        if interruptible and any(event is not None and event.is_set() for event in stop_events):
                            raise ExecutionStopped("Execution stopped during connector call")
                        remaining = timeout - (time.monotonic() - started) if timeout is not None else None
                        if remaining is not None and remaining <= 0:
                            raise subprocess.TimeoutExpired(argv, timeout)
                        try:
                            poll = min(remaining, 0.2) if remaining is not None else 0.2
                            process.communicate(pending_input, timeout=poll if interruptible else remaining)
                            break
                        except subprocess.TimeoutExpired:
                            pending_input = None
                except (subprocess.TimeoutExpired, ExecutionStopped):
                    if os.name != "nt":
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        # SDK workers may own browser children. Target only the
                        # tree belonging to this live Popen handle.
                        if process.poll() is None:
                            try:
                                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                            except (OSError, subprocess.TimeoutExpired):
                                pass
                            if process.poll() is None:
                                process.kill()
                    process.communicate()
                    raise
                out.seek(0)
                content = out.read().decode("utf-8", errors="replace")
                if marker:
                    lines = [line[len(marker):] for line in content.splitlines() if line.startswith(marker)]
                    if not lines:
                        raise SourceError("SDK worker exited without a result envelope", "unknown")
                    content = lines[-1]
                if process.returncode and not marker:
                    stderr = stderr_details(err, secrets=child_env)['stderr']
                    reason = next((line.strip() for line in stderr.splitlines() if line.strip()), '')
                    kind = 'configuration' if process.returncode == 2 and reason.startswith('error:') and 'Usage:' in stderr else 'unknown'
                    message = f"Reader process exited with code {process.returncode}"
                    message += ': ' + reason[:1000] if reason else '; no error output was captured'
                    raise SourceError(message, kind)
                data = json.loads(content)
                if marker:
                    if not data.get("ok"):
                        error = SourceError(data.get("error", "SDK worker failed"), data.get("kind", "unknown"), data.get("retry_after"))
                        error.details = diagnostic_details(data.get("details"), secrets=child_env)
                        raise error
                    return data["data"]
                return data
            except Exception as exc:
                exc.details = {**diagnostic_details(getattr(exc, 'details', None), secrets=child_env),
                    **stderr_details(err, secrets=child_env), 'command': Path(argv[0]).name,
                    'return_code': process.returncode if process is not None else None,
                    'elapsed_seconds': round(time.monotonic() - started, 3), 'timeout_seconds': timeout,
                    'timed_out': isinstance(exc, subprocess.TimeoutExpired), 'details_redacted': True}
                raise
    except subprocess.TimeoutExpired as exc:
        raise SourceError("Connector timed out; no automatic retry was made", "timeout") from exc
    except OSError as exc:
        raise SourceError(f"Connector process could not run ({type(exc).__name__})", "unavailable" if isinstance(exc, FileNotFoundError) else "unknown") from exc
    except (UnicodeError, ValueError) as exc:
        raise SourceError(f"Connector output failed ({type(exc).__name__})", "unsupported") from exc


@ops.observe('sdk', 'call')
def call_worker(provider, operation, arguments, *, timeout=None):
    from .container_runtime import selected, call
    from .recovery import LABELS
    from applypilot.operations.runtime import diagnostic_details
    try:
        if selected(provider):
            return call(provider, operation, arguments, timeout=timeout)
        return run_json([python_for(provider), str(Path(__file__).with_name("worker_main.py"))],
                        {"provider": provider, "operation": operation, "arguments": arguments},
                        timeout=timeout, marker="APPLYPILOT_RESULT:")
    except SourceError as exc:
        exc.details = {**diagnostic_details(getattr(exc, 'details', None)),
                       'component': provider, 'subcommand': operation}
        exc.args = (f"{LABELS.get(provider, provider)}: {exc}",)
        raise

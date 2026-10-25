"""Network-free Docker transport for the pinned rlms REPL implementation.

Upstream's Docker environment exposes an HTTP proxy and permits internet access.
Only JSON model requests cross this boundary; generated Python stays in Docker.
"""
from __future__ import annotations

from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from functools import cache
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Literal
import uuid

from pydantic import AliasPath, BaseModel, Field
from rlm.core.types import REPLResult
from rlm.environments.base_env import BaseEnv
from rlm.environments.docker_repl import _build_exec_script


def _context_resident_bytes(value) -> int:
    """Account for Python's decoded Unicode width and JSON container overhead."""
    pending, seen, total = [value], set(), 0
    while pending:
        item = pending.pop()
        identity = id(item)
        if identity in seen:
            continue
        seen.add(identity)
        total += sys.getsizeof(item)
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
    return total


def _bounded_exec_script(code, depth, history):
    """Keep the pinned runner's state semantics without its full-context reprs."""
    script = _build_exec_script(code, 0, depth, custom_tools={"history": history}, compaction=True)
    locals_field = '"locals": {k: repr(v) for k, v in _locals.items() if not k.startswith("_")},'
    serializer_import = 'try:\n    import dill\nexcept ImportError:\n    import pickle as dill'
    if script.count(locals_field) != 1 or script.count(serializer_import) != 1:
        raise RuntimeError("Unsupported rlms execution scaffold; expected bounded-local output adapter")
    # Redirecting stdout is too late: repr(context) and its JSON encoding have
    # already allocated multiple complete copies of both context aliases.
    return script.replace(locals_field, '"locals": {},').replace(
        serializer_import, 'import _applypilot_cloudpickle as dill')


@cache
def _state_serializer_source():
    from cloudpickle import cloudpickle
    return Path(cloudpickle.__file__).read_text(encoding='utf-8')


class _CellResult(BaseModel):
    stdout: str
    stderr: str
    final_answer: str | None
    diagnostic_variables: dict[str, Any] = Field(default_factory=dict)


class _ModelRequest(BaseModel):
    route: Literal["llm_query", "llm_query_batched", "rlm_query", "rlm_query_batched"]
    payload: dict[str, Any]


class _ContainerInspection(BaseModel):
    state: dict[str, Any] = Field(validation_alias="State")
    labels: dict[str, str] = Field(validation_alias=AliasPath("Config", "Labels"))


_startup_sample_lock = threading.Lock()
_startup_sample = {}








@contextmanager
def _container_start(check, memory_mib):
    """Start one sandbox; Docker allocation failures propagate to Temporal."""
    check()
    yield


_CONTAINER_LOGGER = '''import json, sys, time
from pathlib import Path
host_path = Path('/workspace/_host_phase.json')
host_revision = None
host_record = None
next_host_check = 0
last_host_log = 0
with open('/tmp/rlm-diagnostics.log', 'a+', encoding='utf-8') as stream:
    stream.seek(0)
    print(json.dumps({'at': time.time(), 'event': 'container_logger_ready'}), flush=True)
    while True:
        chunk = stream.read(65536)
        if chunk:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        else:
            time.sleep(0.1)
        now = time.monotonic()
        if now >= next_host_check:
            next_host_check = now + 2
            try:
                stat = host_path.stat()
                if not host_path.is_symlink() and stat.st_size <= 16000 and stat.st_mtime_ns != host_revision:
                    with host_path.open(encoding='utf-8') as host_stream:
                        host_record = json.load(host_stream)
                    host_revision = stat.st_mtime_ns
                    print(json.dumps(host_record, ensure_ascii=False), flush=True)
                    last_host_log = now
                if host_record and str(host_record.get('phase', '')).startswith('waiting_') and now - last_host_log >= 15:
                    print(json.dumps({**host_record, 'event': 'host_waiting',
                                      'wait_seconds': round(time.time() - host_record['at'], 1)}), flush=True)
                    last_host_log = now
            except (OSError, ValueError, TypeError, AttributeError, KeyError):
                pass
'''


# The upstream REPL imports requests. This transport implements only its post /
# response.json contract, over the sandbox's private mounted scratch directory.
_TRANSPORT = '''import json as _json, time, os
from pathlib import Path
from urllib.parse import urlsplit

def _diagnostic(event, **fields):
    try:
        record = {'at': time.time(), 'event': event, 'pid': os.getpid(),
                  'cell': os.getenv('APPLYPILOT_RLM_CELL', ''), **fields}
        with open('/tmp/rlm-diagnostics.log', 'a', encoding='utf-8') as stream:
            stream.write(_json.dumps(record, ensure_ascii=False) + '\\n')
    except Exception:
        pass

def post(url, json=None, timeout=None):
    root = Path('/workspace')
    reply = root / '_reply.json'
    reply.unlink(missing_ok=True)
    pending = root / '_pending.json'
    route = urlsplit(url).path.removeprefix('/')
    pending.write_text(_json.dumps({'route': route, 'payload': json}), encoding='utf-8')
    pending.replace(root / '_query.json')
    # The host owns cancellation, budgets and provider recovery. Upstream's
    # requests timeout must not truncate a recursive model call in the guest.
    started = time.monotonic()
    next_log = started + 15
    _diagnostic('model_request_queued', route=route)
    while not reply.exists():
        if time.monotonic() >= next_log:
            _diagnostic('waiting_model_reply', elapsed_seconds=round(time.monotonic() - started, 3))
            next_log = time.monotonic() + 15
        time.sleep(0.05)
    with reply.open(encoding='utf-8') as stream:
        value = _json.load(stream)
    _diagnostic('model_reply_received', elapsed_seconds=round(time.monotonic() - started, 3))
    class Response:
        def json(self):
            return value
    return Response()
'''

_RUN_CELL = '''import faulthandler, json, os, resource, runpy, sys, traceback
from contextlib import redirect_stdout
sys.path.insert(0, '/workspace')
os.environ['APPLYPILOT_RLM_CELL'] = sys.argv[2]
from requests import _diagnostic
# Bound actual computation, excluding provider waits and sleeping. This limit
# belongs to this disposable cell process, not the recursive inference run.
cpu_seconds = int(sys.argv[3])
resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
try:
    fault_output = open('/tmp/rlm-diagnostics.log', 'a')
except OSError:
    fault_output = sys.stderr
faulthandler.enable(file=fault_output)
faulthandler.dump_traceback_later(30, repeat=True, file=fault_output)
_diagnostic('cell_started')
try:
    # The pinned runner exposes its buffers and answer in the returned namespace.
    # The adapter removes full-local reprs before running this scaffold. Discard
    # its remaining redundant serialized stdout record.
    with open(os.devnull, 'w') as discarded, redirect_stdout(discarded):
        namespace = runpy.run_path('/workspace/_exec.py', run_name='__main__')
except BaseException as exc:
    _diagnostic('cell_crashed', error_type=type(exc).__name__, traceback=traceback.format_exc())
    raise
finally:
    faulthandler.cancel_dump_traceback_later()
    faulthandler.disable()
    if fault_output is not sys.stderr:
        fault_output.close()
value = {'stdout': namespace['stdout_buf'].getvalue(),
         'stderr': namespace['stderr_buf'].getvalue(),
         'final_answer': namespace['_final']}
if sys.argv[1:2] == ['json'] and value.get('final_answer') is not None:
    # rlms 0.1.3 applies str() to native dictionaries. Read its in-guest
    # namespace before losing type information; never deserialize guest pickle
    # or evaluate Python representations on the host.
    try:
        native = namespace['_ans']['content']
        value['final_answer'] = native if isinstance(native, str) else json.dumps(native, ensure_ascii=False, allow_nan=False)
    except (KeyError, TypeError, ValueError):
        value['final_answer'] = ''
        value['stderr'] = value.get('stderr', '') + '\\nFinal answer must be JSON-serializable; correct answer["content"].'

# Report compact metadata only. State remains intact inside the container.
variables = {}
for name, item in list(namespace.get('_locals', {}).items())[:100]:
    if isinstance(name, str) and not name.startswith('_'):
        variables[name] = {'type': type(item).__name__}
        if type(item) in (str, list, tuple, dict, set, bytes):
            variables[name]['size'] = len(item)
value['diagnostic_variables'] = variables
text = value.get('stdout', '')
stderr = value.get('stderr', '')
_diagnostic('cell_finished', stdout=text[:128000], stderr=stderr[:32000],
            variables=variables,
            stdout_chars=len(text), stderr_chars=len(stderr),
            stdout_truncated=len(text)>128000, stderr_truncated=len(stderr)>32000,
            has_final_answer=value.get('final_answer') is not None)
value['stdout'] = text[:32000] + ('\\n[Output truncated; inspect a smaller context slice.]' if len(text) > 32000 else '')
value['stderr'] = stderr[:4000]
print(json.dumps(value, ensure_ascii=False))
'''


class ObservationSandbox(BaseEnv):
    """A disposable environment; no browser, host filesystem or network access."""

    def __init__(self, context, *, query, check, application_id, image, parallel_calls, depth,
                 final_validator=None, final_feedback=True, diagnostics=None):
        super().__init__()
        from applypilot.config import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(prefix="rlm-", dir=LOG_DIR, ignore_cleanup_errors=True)
        self.root = Path(self._temporary.name).resolve()
        self.root.chmod(0o777)
        self.query, self.check = query, check
        self.final_validator = final_validator
        self.final_feedback = final_feedback
        self.parallel_calls, self.depth = parallel_calls, depth
        self.container = "applypilot-rlm-" + uuid.uuid4().hex
        self.image, self.application_id = image, application_id
        from .rlm_diagnostics import RLMTrace
        self.diagnostics = diagnostics or RLMTrace("sandbox", application_id)
        self.cell_number = 0
        self.request_number = 0
        self._container_removed = True
        from .settings import integer
        # Configured memory is a floor; input size determines additional room
        # for JSON decoding, guest state serialization and Unicode expansion.
        self._memory_mib = integer("APPLYPILOT_RLM_SANDBOX_MEMORY_MB", 512, 128)
        self._context_bytes = 0
        self.history = []
        self.calls = []
        try:
            self._write("requests.py", _TRANSPORT)
            self.load_context(context)
        except BaseException:
            self.cleanup()
            raise

    def _event(self, name, **fields):
        self.diagnostics.event(name, container=self.container, depth=self.depth, cell=self.cell_number, **fields)

    def host_phase(self, phase, **fields):
        """Make host model waits visible to Docker's guest-only log stream."""
        try:
            self._write("_host_phase.json", json.dumps({
                "at": time.time(), "event": "host_phase", "phase": phase,
                "container": self.container, "cell": self.cell_number,
                "rlm_id": self.diagnostics.rlm_id, **fields,
            }))
        except (OSError, TypeError, ValueError):
            pass

    def _write(self, name, text):
        # Atomic replacement never follows a sandbox-created destination symlink.
        temporary = self.root / ("host-" + uuid.uuid4().hex)
        with temporary.open("x", encoding="utf-8") as output:
            output.write(text)
        temporary.replace(self.root / name)

    def _docker(self, args, *, timeout=30, cleanup=False):
        """One cancellable control request; cleanup retains its own deadline."""
        command = ["docker", *args]
        timeout = min(timeout, self.check() or timeout) if not cleanup else timeout
        deadline = time.monotonic() + timeout
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            try:
                while True:
                    if not cleanup:
                        self.check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(command, timeout)
                    try:
                        output, error = process.communicate(timeout=min(0.2, remaining))
                        return subprocess.CompletedProcess(command, process.returncode, output, error)
                    except subprocess.TimeoutExpired:
                        continue
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def setup(self):
        self.check()
        if shutil.which("docker") is None:
            raise RuntimeError("Recursive context analysis requires Docker")
        args = ["run", "--detach", "--rm", "--pull", "never", "--name", self.container,
                "--log-driver", "local", "--log-opt", "max-size=4m", "--log-opt", "max-file=2",
                "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt",
                "no-new-privileges", "--pids-limit", "64", "--memory", f"{self._memory_mib}m",
                "--memory-swap", f"{self._memory_mib * 2}m", "--cpus", "1",
                "--user", "65534:65534", "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m",
                "--mount", f"type=bind,source={self.root},target=/workspace"]
        if self.application_id:
            args.extend(["--label", "applypilot.application=" + self.application_id])
        args.extend(["--label", "applypilot.rlm=" + self.diagnostics.rlm_id,
                     "--label", "applypilot.trace=" + self.diagnostics.trace_id])
        args.extend([self.image, "python", "-u", "-c", _CONTAINER_LOGGER])
        with self.diagnostics.span("container_start", container=self.container, image=self.image, depth=self.depth,
                                   memory_mib=self._memory_mib, context_bytes=self._context_bytes):
            try:
                # From this point a timed-out CLI may still have created the
                # container; cleanup must inspect the exact owned instance.
                self._container_removed = False
                result = self._docker(args)
            except subprocess.TimeoutExpired:
                # Docker Desktop can start the container before its CLI returns.
                # Confirm this exact owned instance instead of discarding it or
                # launching a second one; load_context still checks REPL readiness.
                state = self._owned_container_state()
                if not state or not state.get("Running"):
                    raise
                self.check()
                self._event("container_start_recovered", reason="docker_client_timeout")
                return
        self._event("container_start_result", return_code=result.returncode, payload=result.stderr)
        if result.returncode:
            raise RuntimeError("Cannot start the isolated RLM container; pull the configured Docker image first")

    def load_context(self, context_payload):
        # Stream the JSON encoder to disk rather than retaining another giant
        # host string. Atomic replacement does not follow guest-created links.
        temporary = self.root / ("host-" + uuid.uuid4().hex)
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(context_payload, output, ensure_ascii=False)
            self._context_bytes = temporary.stat().st_size
            temporary.replace(self.root / "context.json")
        finally:
            temporary.unlink(missing_ok=True)
        resident_bytes = _context_resident_bytes(context_payload)
        quantum = 64 * 1024 * 1024
        # JSON decoding temporarily holds the raw string plus decoded objects;
        # the pinned runner also serializes guest state between cells. Account
        # for those copies without ever shortening the available source corpus.
        peak_bytes = 128 * 1024 * 1024 + resident_bytes * 3 + self._context_bytes * 2
        memory_mib = max(self._memory_mib, ((peak_bytes + quantum - 1) // quantum) * 64)
        if not self._container_removed and memory_mib > self._memory_mib:
            result = self._docker(["update", "--memory", f"{memory_mib}m",
                                   "--memory-swap", f"{memory_mib * 2}m", self.container])
            result.check_returncode()
        self._memory_mib = memory_mib
        self._event("context_capacity", context_bytes=self._context_bytes,
                    resident_bytes=resident_bytes, memory_mib=self._memory_mib)
        if self._container_removed:
            self.host_phase("waiting_container_start", memory_mib=self._memory_mib)
            with _container_start(self.check, self._memory_mib):
                self.setup()
                self._initialize_context()
        else:
            self._initialize_context()

    def _initialize_context(self):
        self.execute_code("import json\nwith open('/workspace/context.json', encoding='utf-8') as f:\n"
                          "    context_0 = context = json.load(f)\nhistory = []")

    def append_compaction_entry(self, entry):
        self.history.append(entry)

    def _request(self):
        path = self.root / "_query.json"
        if not path.exists():
            return
        self.request_number += 1
        self._event("subcall_read_started", request=self.request_number)
        # Read guest-controlled paths in the guest. A replaced symlink must
        # never cause the host to open a file outside the scratch directory.
        reader = ("from pathlib import Path; p=Path('/workspace/_query.json'); "
                  "assert p.is_file() and not p.is_symlink() and p.stat().st_size <= 2000000; "
                  "print(p.read_text(encoding='utf-8')); p.unlink()")
        result = self._docker(["exec", self.container, "python", "-c", reader])
        if result.returncode:
            self._event("subcall_read_failed", request=self.request_number, return_code=result.returncode,
                        payload=result.stdout + result.stderr)
            result.check_returncode()
        self.check()
        request = _ModelRequest.model_validate_json(result.stdout, strict=True)
        route, payload = request.route, request.payload
        prompts = payload.get("prompts") if route.endswith("_batched") else [payload.get("prompt")]
        if (not isinstance(prompts, list) or not 1 <= len(prompts) <= 64
                or any(not isinstance(prompt, (str, dict, list)) for prompt in prompts)):
            raise ValueError("Invalid sandbox model prompts")
        self._event("subcall_dispatch", request=self.request_number, route=route, count=len(prompts), payload=prompts)
        # Model selection remains host-owned. Container-supplied model names,
        # URLs, provider options and paths never reach the gateway.
        with ThreadPoolExecutor(max_workers=self.parallel_calls) as pool:
            pending = [pool.submit(copy_context().run, self.query, prompt, route.startswith("rlm_"))
                       for prompt in prompts]
            results = [future.result() for future in pending]
        self.calls.extend(results)
        response = ({"responses": [item.response for item in results]} if route.endswith("_batched")
                    else {"response": results[0].response})
        self._write("_reply.json", json.dumps(response, ensure_ascii=False))
        self.host_phase("executing_cell")
        self._event("subcall_reply", request=self.request_number, route=route, count=len(results), payload=response)

    def _validate_final_result(self, data: _CellResult):
        final = data.final_answer
        if final is not None and self.final_validator is not None:
            try:
                data.final_answer = self.final_validator(final)
            except ValueError as exc:
                self._event("final_validation_rejected", payload={"error": str(exc), "proposed_answer": final})
                # Learning grades invalid students immediately. Judges and
                # reflection still receive bounded contract feedback.
                if not self.final_feedback:
                    raise
                # Feedback stays inside the existing finite RLM iteration and
                # call/time budget. Do not add another provider retry loop.
                data.final_answer = None
                data.stderr = (data.stderr[:2500] +
                    "\nFinal answer rejected: " + str(exc)[:1000] +
                    "\nCorrect answer['content'] using the original evidence and schema.")
        return data

    def execute_code(self, code):
        from .settings import integer

        started = time.monotonic()
        self.check()
        self.cell_number += 1
        self.host_phase("executing_cell")
        self._event("cell_started", payload=code, remaining_seconds=self.check())
        self.calls = []
        # History is host-owned. Synchronize it with this cell instead of
        # launching a second Docker exec just to assign it after every turn.
        self._write("_applypilot_cloudpickle.py", _state_serializer_source())
        self._write("_exec.py", _bounded_exec_script(code, self.depth, self.history))
        # Reinstall the restricted transport between cells, including after a
        # model imports or overwrites modules in its own scratch workspace.
        self._write("requests.py", _TRANSPORT)
        for name in ("_query.json", "_reply.json"):
            (self.root / name).unlink(missing_ok=True)
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error_output:
            process = subprocess.Popen(["docker", "exec", "--env", "PYTHONDONTWRITEBYTECODE=1",
                                        self.container, "python", "-c", _RUN_CELL,
                                       "json" if self.final_validator is not None else "text", str(self.cell_number),
                                       str(integer("APPLYPILOT_RLM_CELL_CPU_SECONDS", 60, 1))],
                                       stdout=output, stderr=error_output,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            next_log = time.monotonic() + 15
            output_captured = False
            raw = None
            stderr = b""
            try:
                while process.poll() is None:
                    self.check()
                    if output.tell() + error_output.tell() > 4_000_000:
                        raise ValueError("Sandbox output exceeded its bound")
                    self._request()
                    if time.monotonic() >= next_log:
                        self._event("cell_waiting", phase_seconds=round(time.monotonic() - started, 3),
                                    remaining_seconds=self.check(), output_bytes=output.tell() + error_output.tell())
                        next_log = time.monotonic() + 15
                    time.sleep(0.05)
                self.check()
                output.seek(0)
                raw = output.read(4_000_001)
                error_output.seek(0)
                stderr = error_output.read(max(0, 4_000_001 - len(raw)))
                output_captured = True
                self._event("cell_process_exit", return_code=process.returncode,
                            phase_seconds=round(time.monotonic() - started, 3), output_bytes=len(raw) + len(stderr))
                if len(raw) + len(stderr) > 4_000_000:
                    raise ValueError("Sandbox output exceeded its bound")
                if process.returncode:
                    error = RuntimeError(f"RLM sandbox execution failed (exit {process.returncode})")
                    error.details = {"return_code": process.returncode, "reason": "sandbox_process_failed",
                                     "context_bytes": self._context_bytes, "memory_mib": self._memory_mib}
                    raise error
                if stderr:
                    self._event("cell_process_stderr", payload=stderr)
                data = self._validate_final_result(_CellResult.model_validate_json(raw, strict=True))
                self.host_phase("cell_finished", has_final_answer=data.final_answer is not None)
                self._event("cell_result", payload=data.model_dump(), has_final_answer=data.final_answer is not None)
                return REPLResult(stdout=data.stdout, stderr=data.stderr,
                                  locals={}, rlm_calls=self.calls,
                                  execution_time=time.monotonic() - started, final_answer=data.final_answer)
            except BaseException as exc:
                if raw is not None:
                    self._event("cell_failed_output", payload=raw + stderr)
                self.diagnostics.failure("cell_failed", exc, container=self.container, cell=self.cell_number)
                raise
            finally:
                active_error = sys.exception()
                try:
                    try:
                        if process.poll() is None:
                            self._remove_container()
                    finally:
                        try:
                            if process.poll() is None:
                                process.kill()
                        finally:
                            process.wait(timeout=5)
                except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
                    self.diagnostics.failure("cell_cleanup_failed", exc,
                                             container=self.container, cell=self.cell_number)
                    if active_error is None:
                        raise
                if not output_captured:
                    output.seek(0)
                    raw = output.read(4_000_001)
                    error_output.seek(0)
                    self._event("cell_interrupted_output", return_code=process.returncode,
                                payload=raw + error_output.read(max(0, 4_000_001 - len(raw))))

    def _owned_container_state(self, *, cleanup=False):
        result = self._docker(["inspect", "--type", "container", "--format", "{{json .}}", self.container],
                              cleanup=cleanup)
        if result.returncode:
            error = result.stderr.lower()
            if b"no such object" in error or b"no such container" in error:
                return None
            result.check_returncode()
        container = _ContainerInspection.model_validate_json(result.stdout, strict=True)
        if (container.labels.get("applypilot.rlm"), container.labels.get("applypilot.trace")) != (
                self.diagnostics.rlm_id, self.diagnostics.trace_id):
            raise RuntimeError("RLM container ownership labels do not match")
        self._event("container_state", payload=container.state)
        return container.state

    def _remove_container(self):
        if self._container_removed:
            return
        if shutil.which("docker"):
            state = self._owned_container_state(cleanup=True)
            if state is None:
                self._container_removed = True
                return
            # Preserve container diagnostics before --rm destroys Docker's copy.
            try:
                result = self._docker(["logs", "--timestamps", "--tail", "300", self.container],
                                      timeout=5, cleanup=True)
                self._event("container_logs", return_code=result.returncode, payload=result.stdout + result.stderr)
            except (OSError, subprocess.SubprocessError) as exc:
                self.diagnostics.failure("container_logs_unavailable", exc, container=self.container)
            result = self._docker(["rm", "-f", self.container], cleanup=True)
            self._container_removed = result.returncode == 0
            self._event("container_removed", return_code=result.returncode, payload=result.stderr)
            result.check_returncode()

    def cleanup(self):
        active_error = sys.exception()
        try:
            self._remove_container()
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            self.diagnostics.failure("container_cleanup_failed", exc, container=self.container)
            if active_error is None:
                raise
        finally:
            self._temporary.cleanup()

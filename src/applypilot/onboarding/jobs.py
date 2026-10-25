"""Bounded owner-triggered setup jobs. Fixed subprocess operations, no shell endpoint."""
from __future__ import annotations
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
from .storage import now, read_json, write_json, setup_lock
from .environment import child_env
from applypilot.processes import guard_held, process_guard

def _alive(pid):
    from applypilot.processes import process_alive
    return process_alive(pid)


TIMEOUTS={"apply_change":1500,"session_import":180,"session_reuse":180,"github_connect":310,"github_backup_repo":450,"social_connect":1200,"mcporter_scan":35,"mcporter_auth":300,"intake":360,"material_url":650,"suggest":900,"github":600,"verify":180,"gmail_connect":290,"authorize":220,"backup_sync":21600,"backup_check":21600,"backup_restore":21600,"backup_points":1500,"impact_extract":1800,"impact_revise":300}
TIMEOUTS['career_ops'] = 1800

COMPONENT_LABELS = {"llm": "AI gateway", "gmail": "Gmail", "github": "GitHub",
                    "browseros": "BrowserOS", "sources": "job sources", "linkedin": "LinkedIn", "x": "X"}
TASK_LABELS = {"apply_change": "Save settings", "session_import": "Import account session",
               "session_reuse": "Connect saved account", "github_connect": "Connect GitHub",
               "github_backup_repo": "Create backup repository", "social_connect": "Connect social account",
               "mcporter_scan": "Find installed integrations", "mcporter_auth": "Connect integration",
               "intake": "Read profile material", "material_url": "Import material from a link",
               "suggest": "Suggest profile updates", "github": "Collect GitHub evidence",
               "gmail_connect": "Connect Gmail", "authorize": "Enable outreach",
               "backup_sync": "Back up workspace", "backup_check": "Check backup integrity",
               "backup_restore": "Restore workspace", "backup_points": "List recovery points",
               "impact_extract": "Extract work achievements", "impact_revise": "Revise work achievements"}
TASK_LABELS['career_ops'] = 'Prepare career report'


def task_identity(operation, payload=None):
    """Persist only allowlisted descriptive metadata, never action payload values."""
    payload = payload if isinstance(payload, dict) else {}
    component = str(payload.get("component") or "")
    if operation == "verify":
        component = component if component in COMPONENT_LABELS else "connections"
        label = COMPONENT_LABELS.get(component, "saved connections")
        return {"component": component, "display_title": "Check " + label + (" connection" if component not in {"sources", "connections"} else "")}
    component = ("backup" if operation.startswith("backup_") or operation == "github_backup_repo" else
                 "gmail" if operation == "gmail_connect" else "github" if operation.startswith("github") else
                 "outreach" if operation == "authorize" else "material" if operation in {"intake", "material_url", "impact_extract", "impact_revise"} else "settings")
    if operation in {"social_connect", "session_import", "session_reuse"}:
        provider = str(payload.get("provider") or "")
        component = "linkedin" if provider.startswith("linkedin") else "x" if provider == "twikit" else "sources"
    return {"component": component, "display_title": TASK_LABELS.get(operation, "Background task")}


_MUTATIONS = {"social_connect", "session_import", "session_reuse", "github_connect",
              "github_backup_repo", "gmail_connect", "authorize", "mcporter_auth"}
for _name in _MUTATIONS:
    TIMEOUTS[_name] += 1200  # Safe drain allowance before the operation itself.


class _StderrTail:
    """Drain the worker pipe without retaining an unbounded or raw log file."""
    def __init__(self, stream):
        self.stream=stream;self.tail=bytearray();self.total=0;self.lock=threading.Lock()
        self.thread=threading.Thread(target=self._read,daemon=True,name="setup-stderr")
        self.thread.start()

    def _read(self):
        try:
            with self.stream:
                while chunk := self.stream.read1(4096):
                    with self.lock:
                        self.total+=len(chunk)
                        self.tail.extend(chunk)
                        del self.tail[:-32768]
        except OSError:
            pass

    def diagnostics(self, env):
        from applypilot.operations.runtime import diagnostic_details, sanitize
        self.thread.join(timeout=1)
        with self.lock:
            raw,total=bytes(self.tail),self.total
        if total>len(raw):
            raw=raw.partition(b"\n")[2]
        redacted=sanitize(raw,32768,secrets=env)
        clipped=redacted[-6000:]
        return diagnostic_details({"stderr":clipped,"stderr_bytes":total,
                                   "stderr_captured":True,
                                   "stderr_truncated":total>len(raw) or len(redacted)>6000,
                                   "details_redacted":True},secrets=env)


class Jobs:
    def __init__(self,root):
        self.root=root;self.folder=root/"setup"/"tasks";self.lock=threading.Lock();self.process=None;self.thread=None;self.current=None
        self.folder.mkdir(parents=True,exist_ok=True,mode=0o700)
        self._reconcile()

    def _reconcile(self):
        if guard_held(self.root / "setup-tasks.guard") or guard_held(self.root / "setup-worker.guard"):
            return
        from datetime import datetime, UTC
        for path in self.folder.glob("*.state.json"):
            row = read_json(path, {})
            if row.get("state") not in {"queued", "running"}:
                continue
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(row["started_at"])).total_seconds()
            except (KeyError, ValueError, TypeError):
                age = 31
            if age < 30 and not row.get("worker_pid"):
                continue  # Brief launch window, not an indefinite owner-PID lease.
            row.update(state="interrupted", message="This task was interrupted. Saved changes are retained; retry the action to continue.",
                       finished_at=now(), auth_url=None, device_code=None)
            write_json(path, row)

    def status(self):
        self._reconcile()
        paths=sorted(self.folder.glob("*.state.json"),key=lambda p:p.stat().st_mtime,reverse=True)[:12]
        from applypilot.runtime_info import runtime_info
        build = runtime_info()["build"]
        rows = [read_json(p,{}) for p in paths]
        for row in rows:
            row["current_build"] = row.get("runtime_build") == build
        import asyncio
        from applypilot.orchestration.utility_client import utility_status
        try:
            rows.extend(asyncio.run(asyncio.wait_for(utility_status(root=self.root), timeout=5)))
        except Exception as exc:
            rows.append({'id': 'temporal-status', 'operation': 'workflow_status', 'state': 'unavailable',
                         'message': 'Workflow status is unavailable (' + type(exc).__name__ + ').'})
        return rows

    def launch(self,operation,payload):
        from applypilot.orchestration.utility_contracts import ONBOARDING_OPERATIONS
        if operation in ONBOARDING_OPERATIONS:
            import asyncio
            from applypilot.orchestration.utility_client import submit_utility
            return asyncio.run(submit_utility(operation, payload, root=self.root))
        if operation == "career_ops":
            import asyncio
            from applypilot.orchestration.integration_client import submit_report
            return asyncio.run(submit_report(payload['mode'], payload, root=self.root))
        if operation.startswith("backup_"):
            # Old callers must not regain a subprocess-based backup execution path.
            import asyncio
            from applypilot.orchestration.client import run_work
            kinds = {"backup_sync": "backup_full", "backup_check": "backup_check", "backup_points": "backup_points"}
            if operation not in kinds:
                raise ValueError("Recovery is offline: use applypilot backup restore/rollback")
            return asyncio.run(run_work(kinds[operation], {"automatic": False}, root=self.root))
        if operation not in TIMEOUTS: raise ValueError("Unsupported setup operation")
        metadata = task_identity(operation, payload)
        with self.lock, setup_lock(self.root):
            if operation in {"backup_sync", "backup_restore"}:
                from applypilot.backup.common import operation_busy
                if operation_busy(self.root):
                    return {"status": "busy", "state": "deferred", "message": "Another backup or recovery is active; no duplicate operation was started."}
            from .storage import digest
            fingerprint = digest({"operation": operation, "payload": payload})
            active = next((r for r in self.status() if r.get("state") in {"queued", "running"}), None)
            if active:
                if active.get("request_fingerprint") == fingerprint:
                    return {"task_id": active["id"], "state": active["state"], "message": "Already working on this action.", **metadata}
                raise ValueError("Another setup action is in progress. Its status and Cancel button are in Activity & results.")
            if self.thread and self.thread.is_alive(): raise ValueError("One setup task is already running; finish or cancel it before starting another")
            from applypilot.runtime_info import runtime_info
            from applypilot.operations.runtime import correlation
            operation_context = correlation()
            identifier=secrets.token_hex(12);self.current=identifier
            source=self.folder/(identifier+".input.json")
            write_json(source,{"operation":operation,"payload":payload})
            write_json(self.folder/(identifier+".state.json"),{"id":identifier,"operation":operation,**metadata,"request_fingerprint":fingerprint,"owner_pid":os.getpid(),"state":"queued","runtime_build":runtime_info()["build"],"started_at":now(),"message":"Starting: "+metadata["display_title"],"auth_url":None,
                **({"operation_run_id":operation_context.get("run_id"),"operation_span_id":operation_context.get("span_id")}
                   if operation_context.get("run_id") else {})})
            self.thread=threading.Thread(target=self._run,args=(identifier,operation),daemon=True,name="setup-"+operation)
            self.thread.start()
        return {"task_id":identifier,"state":"queued", **metadata}

    def _run(self,identifier,operation):
        with process_guard(self.root / "setup-tasks.guard"):
            self._run_locked(identifier, operation)

    def _run_locked(self,identifier,operation):
        path=self.folder/(identifier+".state.json");source=self.folder/(identifier+".input.json")
        output=self.folder/(identifier+".result.json")
        stderr=None;env={}
        try:
            argv=[sys.executable,"-m","applypilot.onboarding.worker",identifier]
            env=child_env(self.root)
            env["APPLYPILOT_DIR"]=str(self.root.resolve())
            # Preserve package import location for source-tree invocations, without accepting it from HTTP.
            env["PYTHONPATH"]=str(Path(__file__).resolve().parents[2])+os.pathsep+env.get("PYTHONPATH","")
            with self.lock:
                if read_json(path,{}).get("state")=="cancelled": return
                self.process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                    start_new_session=os.name!="nt")
                proc=self.process
                stderr=_StderrTail(proc.stderr)
                row=read_json(path,{});row["worker_pid"]=proc.pid;write_json(path,row)
            try: proc.wait(timeout=TIMEOUTS[operation])
            except subprocess.TimeoutExpired as exc:
                self._kill(proc)
                from .diagnostics import explain
                row=read_json(path,{})
                result={**explain(exc,component=operation),**stderr.diagnostics(env)}
                row.update(state="timed_out",message="Task timed out. Inspect saved drafts/credentials before retrying; no task is automatically replayed.",result=result,finished_at=now(),auth_url=None,device_code=None)
                if operation == 'career_ops':
                    row['message'] = 'Career report timed out. Saved input remains available; interrupted reports retry through the pipeline after their lease expires.'
                write_json(path,row)
            else:
                row=read_json(path,{})
                if row.get("state") not in {"cancelled","timed_out"}:
                    result=read_json(output,{"error":"Setup worker stopped unexpectedly; no success was verified",
                                            "code":"setup_worker_stopped", "return_code":proc.returncode,
                                            "phase":"worker_exit", "details_redacted":True})
                    if proc.returncode and "error" not in result:
                        result.update(error="Setup worker exited unsuccessfully; no success was verified",
                                      code="setup_worker_stopped",return_code=proc.returncode,phase="worker_exit")
                    if proc.returncode or result.get("code")=="setup_worker_stopped":
                        result.update(stderr.diagnostics(env))
                    deferred = result.get("status") in {"busy", "backoff", "not_due", "disabled"}
                    check_failed = operation in {"authorize", "verify"} and (result.get("authorized") is False or result.get("ok") is False)
                    failed = "error" in result or check_failed
                    if failed and "error" not in result:
                        result["error"] = "The requested checks did not pass. Review the failed checks; no success is assumed."
                    row.update(state="failed" if failed else "deferred" if deferred else "completed",result=result,
                               message=result.get("error",result.get("message") or result.get("note") or "Completed successfully."),finished_at=now(),auth_url=None,device_code=None)
                    write_json(path,row)
        except Exception as exc:
            from .diagnostics import explain
            row=read_json(path,{})
            result=explain(exc,component=operation)
            if stderr is not None:
                result.update(stderr.diagnostics(env))
            row.update(state="failed",message=result["error"],result=result,finished_at=now(),auth_url=None,device_code=None)
            write_json(path,row)
        finally:
            source.unlink(missing_ok=True);output.unlink(missing_ok=True)
            with self.lock: self.process=None
            old=sorted(self.folder.glob("*.state.json"),key=lambda p:p.stat().st_mtime,reverse=True)[30:]
            for item in old: item.unlink(missing_ok=True)

    @staticmethod
    def _kill(proc):
        if proc.poll() is not None: return
        if os.name!="nt":
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        else:
            # Kill only our spawned tree, not any installed browser/application workers.
            subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        try: proc.wait(timeout=3)
        except subprocess.TimeoutExpired: pass

    def cancel(self,identifier):
        if isinstance(identifier, str) and identifier.startswith('ap:'):
            import asyncio
            from applypilot.orchestration.client import cancel
            return asyncio.run(cancel(identifier, root=self.root))
        if not re.fullmatch(r"[0-9a-f]{24}",str(identifier)): raise ValueError("Invalid task identifier")
        with self.lock:
            if identifier!=self.current: raise ValueError("That task is no longer running in this viewer")
            path=self.folder/(identifier+".state.json");row=read_json(path,{})
            if row.get("state") not in {"queued","running"}: raise ValueError("Task already finished")
            row.update(state="cancelled",message="Stopped. Already saved draft/account changes are not undone; inspect before retrying.",finished_at=now(),auth_url=None,device_code=None)
            write_json(path,row)
            if self.process: self._kill(self.process)
        return {"cancelled":True}

    def close(self):
        with self.lock:
            if self.process: self._kill(self.process)

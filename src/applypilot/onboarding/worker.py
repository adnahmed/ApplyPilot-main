"""Internal fixed-operation entrypoint; the website cannot choose a command."""
from __future__ import annotations
import os
import re
import sys
from .storage import read_json, write_json


def main():
    identifier=sys.argv[1] if len(sys.argv)==2 else ""
    if not re.fullmatch(r"[0-9a-f]{24}",identifier): raise SystemExit(2)
    from applypilot.config import APP_DIR
    root=APP_DIR;folder=root/"setup"/"tasks"
    request=read_json(folder/(identifier+".input.json"),{},limit=12_000_000)
    if request.get("operation") in {"session_import", "apply_change"}:
        # Cookie upload is private input, never durable task history.
        (folder/(identifier+".input.json")).unlink(missing_ok=True)
    path=folder/(identifier+".state.json")
    def progress(**kwargs):
        row=read_json(path,{})
        if row.get("state")=="cancelled": raise SystemExit(1)
        row.update(state="running",**kwargs);write_json(path,row)
    try:
        from applypilot.runtime_info import runtime_info
        progress(message="Working on "+str(request.get("operation")),worker_runtime=runtime_info())
        operation=request["operation"];data=request.get("payload",{})
        from contextlib import ExitStack
        with ExitStack() as contexts:
            if operation in {"apply_change", "social_connect", "session_import", "session_reuse",
                             "github_connect", "github_backup_repo", "gmail_connect", "authorize", "mcporter_auth"}:
                from applypilot.control import maintenance
                contexts.enter_context(maintenance(root, progress))
            if operation == "apply_change":
                from .service import Setup
                allowed = {"setup/connections", "setup/commit", "setup/restore", "setup/gmail-client",
                           "setup/runtime-policy", "setup/max-local", "setup/import-session", "setup/mcp-binding"}
                if data.get("route") not in allowed:
                    raise ValueError("Unsupported settings change")
                result = Setup(root)._post(data["route"], data.get("data", {}))
            elif operation.startswith("backup_"):
                raise ValueError("Legacy backup subprocess execution is retired; use native Temporal backup commands")
            elif operation in {"impact_revise", "impact_extract", "career_ops", "intake", "material_url", "suggest", "github"}:
                raise ValueError("This operation executes through Temporal; resubmit the request")
            elif operation=="verify":
                from .checks import verify
                result=verify(root,data,progress)
            elif operation=="github_connect":
                from .github_connect import connect
                result=connect(root,data,progress)
            elif operation=="github_backup_repo":
                from .github_connect import create_backup_repository
                result=create_backup_repository(root,data,progress)
            elif operation=="session_import":
                from .sessions import import_file
                result=import_file(root,data)
            elif operation=="session_reuse":
                from .sessions import reuse
                result=reuse(root,data,progress)
            elif operation=="social_connect":
                from .sessions import connect
                result=connect(root,data,progress)
            elif operation=="mcporter_scan":
                from applypilot.integrations.mcporter import scan
                result=scan(root,data,progress)
            elif operation=="mcporter_auth":
                from applypilot.integrations.mcporter import auth
                result=auth(root,data,progress)
            elif operation=="gmail_connect":
                from .gmail import connect
                result=connect(root,data,progress)
            elif operation=="authorize":
                from .checks import authorize
                result=authorize(root,data,progress)
            else: raise ValueError("Unsupported operation")
            if isinstance(result, dict):
                result.setdefault("message", result.get("note") or "Changes saved successfully.")
                if result.get("restart_required"):
                    result["restart_required"] = False
                    result["settings_applied_on_resume"] = True
    except ValueError as exc:
        if request.get("operation")=="backup_sync":
            from applypilot.backup.service import record_failure
            record_failure(root,exc)
        # Our validation messages do not carry secret values. Redact any configured secret
        # that also appears in a downstream validation message as a defense in depth.
        from .environment import SECRET,ENV_HINTS
        message=str(exc)[:700]
        for key in ENV_HINTS:
            value=os.environ.get(key,"")
            if SECRET.search(key) and len(value)>4: message=message.replace(value,"[redacted]")
        from .diagnostics import explain
        from applypilot.operations.runtime import sanitize
        result=explain(exc,component=str(request.get("operation","setup")))
        result["error"]=sanitize(message,700)
    except Exception as exc:
        if request.get("operation")=="backup_sync":
            from applypilot.backup.service import record_failure
            record_failure(root,exc)
        from .diagnostics import explain
        result=explain(exc,component=str(request.get("operation","setup")))
    write_json(folder/(identifier+".result.json"),result)


if __name__ == "__main__":
    from applypilot.config import APP_DIR
    from applypilot.processes import process_guard
    with process_guard(APP_DIR / "setup-worker.guard"):
        main()

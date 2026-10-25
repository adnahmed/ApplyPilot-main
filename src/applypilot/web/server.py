"""Loopback-only authenticated ASGI dashboard, using the existing MCP web stack.

It never starts a second application worker, bypasses a send guard, or provides
arbitrary SQL/files/commands. Writes are authenticated owner configuration, bounded setup tasks and existing
veto/budget controls. Operational API reads use read-only PostgreSQL connections.
"""
from __future__ import annotations

from applypilot.integrations.github_auth import GitHubCredentialError
from applypilot.orchestration.client import work_status

from contextlib import contextmanager, asynccontextmanager
from pathlib import Path
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
from applypilot import postgres
import threading
import time
import urllib.request
import webbrowser

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route
import uvicorn

from .data import Store, integer, safe_json, text
from .runtime import atomic_json, read_runtime

log = logging.getLogger(__name__)
STATIC = Path(__file__).with_name("static")
SESSION_SECONDS = 30 * 86400


class Security:
    def __init__(self, token, port):
        self.token = token
        self.port = port
        self.cookie = "ap_session_" + hashlib.sha256((token + ":" + str(port)).encode()).hexdigest()[:12]
        self.sessions = {}  # Fixed 30-day sessions, bounded; lost on server restart.
        self.attempts = []

    def new_session(self):
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
        if len(self.sessions) >= 50:
            self.sessions.pop(next(iter(self.sessions)))
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.sessions[sid] = (csrf, now + SESSION_SECONDS)
        return sid, csrf

    def session(self, request):
        value = self.sessions.get(request.cookies.get(self.cookie, ""))
        if value and value[1] > time.time():
            return value[0]
        return None

    def headers_ok(self, request):
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        host = request.headers.get("host", "").lower()
        if host not in allowed:
            return False
        origin = request.headers.get("origin")
        if origin and origin not in {"http://" + h for h in allowed}:
            return False
        if request.url.path.startswith(("/api/", "/ui/")) and request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
            return False
        return True


class API:
    def __init__(self, app_dir: Path, token: str, port: int, read_only=False, *, dashboard_owner=None):
        self.app_dir = app_dir.resolve()
        self.store = Store(self.app_dir / "main.pgstore")
        self.security = Security(token, port)
        self.read_only = read_only
        self.dashboard_owner = dashboard_owner
        self.cache = {}
        self.cache_lock = threading.Lock()
        self.action_lock = threading.Lock()
        self.engine_lock = threading.Lock()
        self.engine_cache = None
        from applypilot.operations.export_tasks import SupportExports, WorkExports
        self.support_exports = SupportExports(self.app_dir)
        self.work_exports = WorkExports(self.app_dir, self.store.path)
        from applypilot.onboarding.service import Setup
        self.setup = Setup(self.app_dir, read_only)
        from .events import StatusHub
        self.live = StatusHub(self.app_dir)

    def engine_snapshot(self, *, force=False):
        """Separate short cache: slow history/backup reads never own this lock."""
        from .engine import engine_snapshot
        with self.engine_lock:
            runtime = read_runtime(self.app_dir)
            key = (runtime.get("runtime_id"), runtime.get("running"), runtime.get("stale"))
            if not force and self.engine_cache and self.engine_cache[1] == key and time.monotonic() - self.engine_cache[0] < 3:
                return self.engine_cache[2]
            value = engine_snapshot(self.app_dir, self.store, runtime)
            self.engine_cache = (time.monotonic(), key, value)
            return value

    def workers_snapshot(self):
        from .workers import snapshot
        return snapshot(self.app_dir)

    def work_snapshot(self, q):
        from .work import snapshot
        from .workers import snapshot as workers
        from .engine import _scheduler
        runtime = read_runtime(self.app_dir)
        return snapshot(self.app_dir, self.store, q, runtime=runtime,
                        workers=workers(self.app_dir, runtime),
                        engine={"scheduler": {} if runtime.get('execution_owner') == 'Temporal' else
                                _scheduler(self.app_dir, runtime, time.time())},
                        setup_tasks=self.setup.jobs.status() if self.setup.jobs else ())

    def cached_summary(self, days, run_id=None):
        days = integer(days, 30, 1, 365)
        if run_id and (not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id)):
            raise ValueError('Choose a valid retained run')
        key = (days, run_id or None)
        with self.cache_lock:
            entry = self.cache.get(key)
            if entry and time.monotonic() - entry[0] < 5:
                return entry[1]
            if run_id:
                from .run_summary import summary
                value = summary(self.app_dir, self.store, run_id, days)
            else:
                value = self.store.summary(days)
            if len(self.cache) >= 10:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (time.monotonic(), value)
            return value

    def queue_status(self):
        from applypilot.live_status import read_component
        from applypilot.onboarding.integrations import effective_policy
        from applypilot.onboarding.storage import digest, read_bytes

        policy = effective_policy(self.app_dir)
        value = read_component(self.app_dir, "discovery") or {
            "available": False, "needs_initialization": True, "preparation_tasks": 0, "held": False,
            "reason": "Discovery has not published a queue inventory yet.",
        }
        return {**value, "limits": policy["buffer"], "flow_settings": policy.get("flow_control"),
                "revision": digest(read_bytes(self.app_dir / "discovery_runtime.yaml"))}

    def activity(self, q):
        """Merge the same saved setup tasks shown in Connections into Activity.

        Only a bounded display projection is included. No task payload, OAuth
        URL, device code, credential path, or raw result is copied to this feed.
        """
        from .data import now_iso
        from .presentation import humanize_issue, task_view
        from applypilot.operations.runtime import diagnostic_details
        kind = text(q.get("kind") or "all", 30)
        query = text(q.get("q")).casefold()
        value = (self.store.activity(q) if kind not in {"setup", "operations"} else
                 {"items": [], "limit": 100, "at": now_iso(), "warnings": []})
        if kind in {"all", "setup"}:
            # Jobs.status is the existing bounded task history used by Settings.
            for task in self.setup.jobs.status() if self.setup.jobs else []:
                operation = str(task.get("operation") or "setup")[:80]
                projected = task_view(self.app_dir, task)
                result = task.get("result") if isinstance(task.get("result"), dict) else {}
                details = diagnostic_details(result)
                if isinstance(result.get('exception_chain'), list):
                    from applypilot.operations.bundle import Redactor
                    details['exception_chain'] = Redactor(self.app_dir).value(result['exception_chain'])
                for key in ('details', 'error_details'):
                    if isinstance(result.get(key), dict):
                        details.update(diagnostic_details(result[key]))
                checks = []
                for check in result.get('checks') or []:
                    if not isinstance(check, dict):
                        continue
                    evidence = diagnostic_details(check)
                    if check.get('detail'):
                        evidence['detail'] = humanize_issue(check['detail'])
                    evidence.update({key: check[key] for key in ('ok', 'live', 'skipped') if isinstance(check.get(key), bool)})
                    if isinstance(check.get('diagnostics'), dict):
                        evidence['diagnostics'] = diagnostic_details(check['diagnostics'])
                    checks.append(evidence)
                if checks:
                    details['checks'] = checks
                projected['details'] = details
                searchable = json.dumps({**projected, 'event': operation}, ensure_ascii=False)
                if query and not all(term in searchable.casefold() for term in query.split()):
                    continue
                value["items"].append({**projected, "kind": "setup", "event": operation,
                    "task_state": projected["state"], "company": "",
                    "job_url": None, "current_build": bool(task.get("current_build")),
                    "runtime_build": str(task.get("runtime_build") or "")[:100],
                    "reason": projected["message"],
                    **{("authorization" if key == "authorized" else key): result[key]
                       for key in ("authorized", "saved", "verified", "connected") if isinstance(result.get(key), bool)}})
            value["setup_limit"] = 12
        from .activity import merge_operations
        return merge_operations(self.app_dir, value, q)

    def settings(self):
        from applypilot.unattended import settings
        from applypilot.referrals import store as s
        config_ok = True
        env_enabled = True
        try:
            env_enabled = settings.environment_enabled()
            cfg = settings.load()
        except (ValueError, OSError, TypeError):
            config_ok = False
            cfg = settings.validate({})
        contacts_per_job = s.DEFAULTS["contacts_per_job"]
        revision = s.digest(s.dump(cfg))
        problem = ""
        if not config_ok:
            problem = "Outreach settings could not be read. Repair the saved configuration before editing controls."
        elif not env_enabled:
            problem = "Outreach is off: APPLYPILOT_OUTREACH_ENABLED=false."
        elif not cfg["enabled"]:
            problem = "Outreach is disabled in unattended.json."
        with self.store.read() as conn:
            contacts_per_job = s.policy(conn)["contacts_per_job"] if conn is not None else s.DEFAULTS["contacts_per_job"]
            revision = settings.controls_revision(conn, cfg)
            can_edit = config_ok and conn is not None and not self.read_only
        return {"enabled": config_ok and cfg["enabled"], "env_enabled": env_enabled,
                "authorized": config_ok and cfg["enabled"], "can_edit": can_edit,
                "read_only": self.read_only, "problem": problem, "revision": revision, "contacts_per_job": contacts_per_job,
                **{k: cfg[k] for k in ("daily_send_limit", "daily_research_limit", "send_grace_seconds", "channels", "referral_first_hours", "research_grace_minutes", "cycle_seconds")}}

    @contextmanager
    def write(self):
        if self.read_only:
            raise PermissionError("This dashboard was started in read-only mode")
        if not self.store.path.exists():
            raise ValueError("Initialize the database through normal autopilot setup first")
        conn = postgres.connect(self.store.path.as_uri() + "?mode=rw", uri=True, timeout=5)
        conn.row_factory = postgres.Row
        try:
            yield conn
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.close()

    def action(self, action, data, outbox_id=None):
        # Serialize website actions, and delegate cross-process arbitration to the
        # existing short PostgreSQL transaction helper used by both sender and CLI.
        with self.action_lock:
            with self.write() as conn:
                if action == "cancel":
                    if data:
                        raise ValueError("Cancellation does not take extra settings")
                    from applypilot.unattended.grace import cancel
                    result = cancel(conn, text(outbox_id, 150))
                else:
                    if set(data) - {"daily_send_limit", "daily_research_limit", "contacts_per_job", "send_grace_seconds", "revision"}:
                        raise ValueError("Only outreach budgets and the cancel window can be changed here")
                    if not ({"daily_send_limit", "daily_research_limit", "contacts_per_job", "send_grace_seconds"} & set(data)):
                        raise ValueError("No settings supplied")
                    if not isinstance(data.get("revision"), str) or len(data["revision"]) != 64:
                        raise ValueError("Reload Settings before saving; its revision is required")
                    from applypilot.unattended.settings import update_limits
                    result = update_limits(conn, daily_limit=data.get("daily_send_limit"),
                                           daily_research_limit=data.get("daily_research_limit"),
                                           contacts_per_job=data.get("contacts_per_job"),
                                           send_grace_seconds=data.get("send_grace_seconds"),
                                           expected_revision=data.get("revision"))
                with self.cache_lock:
                    self.cache.clear()
                return result

    async def body(self, request, limit=4096):
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/json":
            raise ValueError("Use an application/json request")
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > limit:
                raise ValueError(f"Request exceeds {limit // 1024} KB")
        value = json.loads(content or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        return value

    async def session(self, request):
        security = self.security
        if request.method == "GET":
            return JSONResponse({"csrf": security.session(request), "read_only": self.read_only})
        data = await self.body(request)
        now = time.monotonic()
        security.attempts = [t for t in security.attempts if now - t < 60]
        if len(security.attempts) >= 20:
            return JSONResponse({"error": "Too many unlock attempts; retry in a minute"}, status_code=429)
        token = data.get("token", "")
        if not isinstance(token, str) or not hmac.compare_digest(token, security.token):
            security.attempts.append(now)
            return JSONResponse({"error": "Open the private dashboard link printed by ApplyPilot"}, status_code=401)
        # Reopening the private link must not invalidate action tokens in other
        # tabs sharing this cookie. Reuse a still-valid authenticated session.
        existing = security.session(request)
        if existing:
            return JSONResponse({"csrf": existing, "read_only": self.read_only})
        sid, csrf = security.new_session()
        response = JSONResponse({"csrf": csrf, "read_only": self.read_only})
        response.set_cookie(security.cookie, sid, max_age=SESSION_SECONDS, httponly=True, samesite="strict", path="/")
        return response

    async def dispatch(self, request):
        route = request.path_params.get("route", "")
        q = dict(request.query_params)
        if request.method == "POST":
            if self.read_only and route != 'logout':
                raise PermissionError("This dashboard is read-only")
            limit = 11_000_000 if route in {"setup/task/intake", "setup/import-session", "setup/task/session_import"} else 1_000_000 if route.startswith("setup") else 70_000 if route.startswith(("backups", "diary")) else 4096
            if route in {'learning/import-policy', 'learning/preview-policy'}:
                limit = 5_100_000
            if route == 'providers/credential':
                limit = 8192
            if route.startswith('targets'):
                limit = 15_000_000 if route == 'targets/import-preview' else 100_000
            if route == 'company-discovery/predictive-outcome':
                limit = 20_000
            if route == 'career-ops/run':
                limit = 350_000
            if route.startswith('opportunities/'):
                limit = 70_000
            data = await self.body(request, limit)
            if route.startswith('opportunities/'):
                from .opportunities import write
                value = await run_in_threadpool(write, self, route, data)
                self.live.mark(data=True)
                return JSONResponse(value)
            if route == 'career-ops/run':
                from .career_ops import launch
                return JSONResponse(await run_in_threadpool(launch, self, data))
            if route.startswith('company-discovery/'):
                from .targets import company_discovery
                value = await run_in_threadpool(company_discovery, self, route, data)
                self.live.mark(data=True)
                return JSONResponse(value)
            if route == 'targets' or route.startswith('targets/'):
                from .targets import write
                value = await run_in_threadpool(write, self, route, data)
                self.live.mark(data=True)
                return JSONResponse(value)
            if route.startswith('providers/'):
                if self.read_only:
                    raise PermissionError('This dashboard is read-only')
                from . import provider_health
                if route == 'providers/selection':
                    return JSONResponse(await run_in_threadpool(provider_health.save_selection, data))
                if route == 'providers/check':
                    return JSONResponse(await run_in_threadpool(provider_health.start_probe, data), status_code=202)
                if route == 'providers/credential':
                    return JSONResponse(await run_in_threadpool(provider_health.save_credential, data))
                if route == 'providers/reload':
                    from .provider_maintenance import start
                    return JSONResponse(await run_in_threadpool(start, self.app_dir, data), status_code=202)
                return JSONResponse({'error': 'Unknown provider action'}, status_code=404)
            if route == "queue/settings":
                if self.read_only:
                    raise PermissionError("This dashboard is read-only")
                if set(data) not in ({"revision", "buffer"}, {"revision", "buffer", "flow_control"}):
                    raise ValueError("Supply the displayed buffer/flow settings and revision")
                policy = {"buffer": data["buffer"]}
                if "flow_control" in data:
                    policy["flow_control"] = data["flow_control"]
                result = await run_in_threadpool(self.setup.post, "setup/runtime-policy",
                    {"revision": data["revision"], "policy": policy})
                return JSONResponse(result)
            if route == "autopilot/control":
                if self.read_only:
                    raise PermissionError("This dashboard is read-only")
                from applypilot.control import action
                return JSONResponse(await run_in_threadpool(action, self.app_dir, data, dashboard_owner=self.dashboard_owner))
            if route == 'autopilot/options':
                from .automation_preferences import save
                return JSONResponse(await run_in_threadpool(save, self.app_dir, data))
            if route == "operations/support-bundle":
                if set(data) - {'run_id'} or ('run_id' in data and not isinstance(data['run_id'], str)):
                    raise ValueError("Support export does not accept paths or SQL")
                value = await run_in_threadpool(self.support_exports.start, data.get('run_id', ''))
                return JSONResponse(value, status_code=202)
            if route == "work/exports":
                if set(data) != {"run_id"}:
                    raise ValueError("Choose one retained run to export")
                value = await run_in_threadpool(self.work_exports.start, data["run_id"])
                return JSONResponse(value, status_code=202)
            if route == 'rlm/settings':
                if self.read_only:
                    raise PermissionError('This dashboard is read-only')
                from .rlm_settings import save
                return JSONResponse(await run_in_threadpool(save, self.app_dir, data))
            if route == "temporal/cancel":
                if self.read_only:
                    raise PermissionError("This dashboard is read-only")
                from applypilot.orchestration.client import cancel
                return JSONResponse(await cancel(data.get("workflow_id", ""), root=self.app_dir))
            if route.startswith("learning/"):
                if self.read_only:
                    raise PermissionError("This dashboard is read-only")
                if route in {'learning/preview-policy', 'learning/install-preview'}:
                    from .learning_preview import preview, install
                    operation = preview if route.endswith('preview-policy') else install
                    return JSONResponse(await run_in_threadpool(operation, self.app_dir, data))
                if route == "learning/settings":
                    from applypilot.reasoning.learning_store import configure_mode
                    return JSONResponse(await run_in_threadpool(configure_mode, data), headers={"Cache-Control": "no-store"})
                if route == "learning/import-policy":
                    if set(data) != {"bundle", "trust", "replace"} or type(data["trust"]) is not bool or type(data["replace"]) is not bool:
                        raise ValueError("Supply a policy bundle, explicit trust and replacement choice")
                    from applypilot.reasoning.policies import install_bundle
                    result = await run_in_threadpool(install_bundle, data["bundle"], trust=data["trust"], replace=data["replace"])
                    return JSONResponse(result, headers={"Cache-Control": "no-store"})
                raise LookupError("Unknown learning action")
            if route.startswith("operations/"):
                if self.read_only:
                    raise PermissionError("This dashboard is read-only")
                from applypilot.operations import store as operations_store
                if route == "operations/incident":
                    return JSONResponse(await run_in_threadpool(operations_store.incident_action, self.app_dir, data))
                if route == "operations/settings":
                    value = await run_in_threadpool(operations_store.save_settings, self.app_dir, data)
                    return JSONResponse(value)
                return JSONResponse({"error": "Unknown operations action"}, status_code=404)
            if route.startswith(("backups", "diary")):
                if self.read_only: raise PermissionError("This dashboard is read-only")
                return JSONResponse(await run_in_threadpool(self.memory_action, route, data))
            if route.startswith("setup"):
                return JSONResponse(await run_in_threadpool(self.setup.post, route, data))
            if route == "settings":
                return JSONResponse(await run_in_threadpool(self.action, "settings", data))
            if route == "logout":
                self.security.sessions.pop(request.cookies.get(self.security.cookie, ""), None)
                response = JSONResponse({"ok": True})
                response.delete_cookie(self.security.cookie, path="/")
                return response
            if route.startswith("outreach/") and route.endswith("/cancel"):
                parts = route.split("/")
                if len(parts) == 3:
                    return JSONResponse(await run_in_threadpool(self.action, "cancel", data, parts[1]))
            return JSONResponse({"error": "Unknown action"}, status_code=404)
        if route == "live/status":
            return await self.live.response(request, self.security)
        if route == "status":
            return JSONResponse(await self.live.snapshot())
        if route.startswith("operations/support-bundle/"):
            parts = route.split("/")
            if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] != "download"):
                raise LookupError("Unknown support export endpoint")
            if len(parts) == 3:
                return JSONResponse(await run_in_threadpool(self.support_exports.status, parts[2]))
            path, filename, info = await run_in_threadpool(self.support_exports.download, parts[2])
            return FileResponse(path, filename=filename, stat_result=info,
                media_type="application/zip", headers={"Cache-Control": "no-store"})
        if route == "work/runs":
            from .work_export import list_runs
            return JSONResponse(await run_in_threadpool(list_runs, self.app_dir))
        if route.startswith("work/exports/"):
            parts = route.split("/")
            if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] != "download"):
                raise LookupError("Unknown work export endpoint")
            if len(parts) == 3:
                return JSONResponse(await run_in_threadpool(self.work_exports.status, parts[2]))
            path, filename, info = await run_in_threadpool(self.work_exports.download, parts[2])
            return FileResponse(path, filename=filename, stat_result=info,
                media_type="application/json", headers={"Cache-Control": "no-store"})
        if route in {"operations", "operations/events", "operations/diagnostics", "operations/export", "operations/summary", "operations/metrics", "operations/support-context"}:
            from applypilot.operations import store as operations_store
            if route == "operations/support-context":
                from applypilot.operations.support import snapshot
                return JSONResponse(await run_in_threadpool(snapshot, self.app_dir))
            if route == "operations/metrics":
                from applypilot.operations.metrics import render
                return Response(await run_in_threadpool(render, self.app_dir), media_type="text/plain")
            if route == "operations/diagnostics":
                from applypilot.operations.diagnostics import check
                return JSONResponse(await run_in_threadpool(check))
            if route == "operations/summary":
                return JSONResponse(await run_in_threadpool(operations_store.summary, self.app_dir))
            if route in {"operations/events", "operations/export"}:
                value = await run_in_threadpool(operations_store.events, self.app_dir, q)
                if route == "operations/export":
                    return Response(json.dumps(value, indent=2), media_type="application/json", headers={
                        "Content-Disposition": 'attachment; filename="applypilot-operations-page.json"'})
                return JSONResponse(value)
            return JSONResponse(await run_in_threadpool(operations_store.overview, self.app_dir))
        if route == "learning/export-policy":
            from applypilot.reasoning.policies import export_bundle
            result = await run_in_threadpool(export_bundle, task=q.get("task"))
            return JSONResponse(result, headers={"Cache-Control": "no-store",
                "Content-Disposition": 'attachment; filename="applypilot-policies.json"'})
        if route == 'rlm/status':
            from .rlm_settings import status
            return JSONResponse(await run_in_threadpool(status, self.app_dir))
        if route.startswith('rlm/traces/'):
            from .rlm_settings import trace
            return JSONResponse(await run_in_threadpool(trace, self.app_dir, route.removeprefix('rlm/traces/')),
                                headers={'Cache-Control': 'no-store'})
        if route == "learning/status":
            from applypilot.reasoning.learning_store import status as learning_status
            return JSONResponse(await run_in_threadpool(learning_status), headers={"Cache-Control": "no-store"})
        if route == 'autopilot/options':
            from .automation_preferences import read
            return JSONResponse(await run_in_threadpool(read, self.app_dir))
        if route == "workers/trace":
            from applypilot.apply.agent_trace import read_trace
            return JSONResponse(await run_in_threadpool(read_trace, self.app_dir, q),
                                headers={"Cache-Control": "no-store"})
        if route == "workers/log":
            from .worker_log import read
            return JSONResponse(await run_in_threadpool(read, self.app_dir, q),
                                headers={"Cache-Control": "no-store"})
        if route == "temporal/workflow":
            return JSONResponse(await work_status(q.get("workflow_id", ""),
                run_id=q.get("run_id") or None, root=self.app_dir))
        if route == "temporal/status":
            from applypilot.orchestration.observability import cached
            return JSONResponse(await run_in_threadpool(cached, self.app_dir))
        if route == "temporal/live":
            from applypilot.orchestration.observability import collect
            return JSONResponse(await collect(root=self.app_dir))
        if route == "workers":
            return JSONResponse(await run_in_threadpool(self.workers_snapshot))
        if route == 'providers':
            from .provider_health import snapshot
            return JSONResponse(await run_in_threadpool(snapshot, self.app_dir))
        if route.startswith('providers/check/'):
            from .provider_health import probe_status
            return JSONResponse(await run_in_threadpool(probe_status, route.rsplit('/', 1)[-1]))
        if route.startswith('providers/reload/'):
            from .provider_maintenance import status
            return JSONResponse(await run_in_threadpool(status, route.rsplit('/', 1)[-1]))
        if route == "backups":
            from applypilot.backup.service import status
            return JSONResponse(await run_in_threadpool(status, self.app_dir))
        if route == "diary/item":
            from applypilot.backup.diary_edit import detail
            return JSONResponse(await run_in_threadpool(detail,self.app_dir,q.get("kind"),q.get("id")))
        if route == "diary/history":
            from applypilot.backup.diary_edit import history
            return JSONResponse(await run_in_threadpool(history,self.app_dir))
        if route == "diary":
            from applypilot.backup.diary import view
            return JSONResponse(await run_in_threadpool(view, self.app_dir, q.get("q", ""), integer(q.get("page"), 1, 1, 100000)))
        if route == "setup" or route.startswith("setup/"):
            return JSONResponse(await run_in_threadpool(self.setup.get, route))
        if route == 'targets' or route.startswith('targets/'):
            from .targets import read
            return JSONResponse(await run_in_threadpool(read, self, route, q))
        if route == 'company-discovery':
            from .targets import company_discovery
            return JSONResponse(await run_in_threadpool(company_discovery, self, route))
        if route == 'career-ops':
            from .career_ops import overview
            return JSONResponse(await run_in_threadpool(overview, self, q.get('job_url'), q.get('report_id'), q.get('state')))
        if route == 'opportunities':
            from .opportunities import read
            return JSONResponse(await run_in_threadpool(read, self, q), headers={'Cache-Control': 'no-store'})
        if route == 'opportunities/strategies':
            from .opportunities import strategies
            return JSONResponse(await run_in_threadpool(strategies, self), headers={'Cache-Control': 'no-store'})
        if route in {'opportunities/brief', 'opportunities/brief.md'}:
            from .opportunities import brief
            value = await run_in_threadpool(brief, self, q)
            if route.endswith('.md'):
                return Response(value['markdown'], media_type='text/markdown', headers={
                    'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="forwardable-brief.md"'})
            return JSONResponse(value, headers={'Cache-Control': 'no-store'})
        if route == 'career-ops/opportunities':
            from .career_ops import opportunities
            return JSONResponse(await run_in_threadpool(opportunities, self, q.get('q', ''), q.get('job_url', '')))
        if route == "summary":
            value = await run_in_threadpool(self.cached_summary, q.get("days", 30), q.get('run_id'))
        elif route == "jobs":
            value = await run_in_threadpool(self.store.jobs, q)
        elif route == "facets":
            value = await run_in_threadpool(self.store.facets)
        elif route == "job":
            value = await run_in_threadpool(self.store.detail, q.get("url", ""))
        elif route == "outreach":
            value = await run_in_threadpool(self.store.outreach, q)
        elif route.startswith("outreach/") and len(route.split("/")) == 2:
            value = await run_in_threadpool(self.store.outreach_detail, route.split("/")[1])
        elif route == "activity":
            value = await run_in_threadpool(self.activity, q)
        elif route == "work":
            value = await run_in_threadpool(self.work_snapshot, q)
        elif route.startswith("work/") and len(route.split("/")) == 2:
            from .work import detail
            value = await run_in_threadpool(detail, self.app_dir, self.store, route.split("/", 1)[1],
                                            self.setup.jobs.status() if self.setup.jobs else ())
        elif route == "sources":
            value = await run_in_threadpool(self.store.sources)
        elif route == "system":
            value = await run_in_threadpool(self.store.system)
        elif route == "queue":
            value = await run_in_threadpool(self.queue_status)
        elif route == "engine":
            value = await run_in_threadpool(self.engine_snapshot)
        elif route == "runtime":
            value = await run_in_threadpool(read_runtime, self.app_dir)
        elif route == "settings":
            value = await run_in_threadpool(self.settings)
        elif route == "jobs.csv":
            content, meta = await run_in_threadpool(self.store.export, q)
            return Response("\ufeff" + content, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="applypilot-jobs.csv"',
                "X-Export-Truncated": str(meta["truncated"]).lower(), "X-Export-Limit": "10000"})
        elif route == "document":
            path = await run_in_threadpool(self.store.document, q.get("url", ""), q.get("kind", ""), q.get('report_id'))
            return FileResponse(path, filename=path.name, content_disposition_type="attachment")
        else:
            return JSONResponse({"error": "Unknown endpoint"}, status_code=404)
        return JSONResponse(value)

    def memory_action(self, route, data):
        from applypilot.backup import service
        from applypilot.orchestration import client
        import asyncio
        if route == "backups/settings":
            result = service.configure(self.app_dir, data)
            # Consent/configuration is durable before the native Schedule write.
            # A failed API request is retryable; no local fallback scheduler starts.
            result["temporal"] = asyncio.run(client.install_backup_schedules(
                enabled=result["config"]["enabled"], root=self.app_dir))
            return result
        if route in {"backups/restore", "backups/rollback"}:
            raise ValueError("Recovery is an offline utility: use applypilot backup restore/rollback "
                             "after stopping all Activity deployments. The dashboard never restores "
                             "a database underneath durable Workflows.")
        if route in {"diary/edit", "diary/undo"}:
            from applypilot.backup.diary_edit import edit, undo
            return (edit if route == "diary/edit" else undo)(self.app_dir, data)
        if route == "diary/note":
            from applypilot.backup.diary import save_note
            return save_note(self.app_dir, data)
        backups = {"backups/sync": "backup_full", "backups/check": "backup_check", "backups/points": "backup_points"}
        if route in backups:
            if route == "backups/sync":
                service.require_ready(self.app_dir)
            return asyncio.run(client.run_work(backups[route], {"automatic": False}, root=self.app_dir))
        # Diary model editing is a finite setup utility, outside Part 2 execution.
        operations = {"diary/extract": "impact_extract", "diary/revise": "impact_revise"}
        if route in operations:
            return self.setup.jobs.launch(operations[route], data)
        raise LookupError("Unknown backup/diary action")

    def application(self):
        from . import views
        async def identity(request):
            # Public challenge proof lets the launcher identify its own viewer
            # without sending the private unlock token to a reused/stale port.
            nonce = request.query_params.get("nonce", "")
            if not (24 <= len(nonce) <= 128 and all(c.isalnum() or c in "-_" for c in nonce)):
                return JSONResponse({"error": "Invalid challenge"}, status_code=400)
            proof = hmac.new(self.security.token.encode(), ("identity:" + nonce).encode(), hashlib.sha256).hexdigest()
            return JSONResponse({"proof": proof, "read_only": self.read_only})
        async def index(request):
            return await views.page(self, request)
        def html_handler(handler):
            async def dispatch_html(request):
                return await handler(self, request)
            return dispatch_html
        async def asset(request):
            name = request.path_params["name"]
            directory = STATIC.resolve()
            path = (directory / name).resolve()
            if path.parent != directory or path.suffix not in {".js", ".css", ".svg", ".woff2"} or not path.is_file():
                return Response(status_code=404)
            return FileResponse(path)
        routes = [Route("/", index), Route("/static/{name}", asset),
            Route('/ui/status', html_handler(views.status)),
            Route('/ui/profile-fields', html_handler(views.profile_fields)),
            Route('/ui/tasks/{identifier}', html_handler(views.task_status), methods=['GET']),
            Route('/ui/provider-tasks/{kind}/{identifier}', html_handler(views.provider_task_status)),
            Route('/ui/exports/{kind}/{identifier}', html_handler(views.export_status)),
            Route('/ui/targets/new', html_handler(views.target_page)),
            Route('/ui/targets/import', html_handler(views.import_page)),
            Route('/ui/targets/{identifier}', html_handler(views.target_page)),
            Route('/ui/fragments/{view}/{index:int}', html_handler(views.fragment)),
            Route('/ui/detail/{kind}/{identifier}', html_handler(views.detail)),
            Route('/ui/detail/{kind}', html_handler(views.detail)),
            Route('/ui/actions/{route:path}', html_handler(views.action), methods=['POST']),
            Route('/ui/{view}', html_handler(views.page)),
            Route("/api/identity", identity, methods=["GET"]),
            Route("/api/session", self.session, methods=["GET", "POST"]),
            Route("/api/{route:path}", self.dispatch, methods=["GET", "POST"])]

        async def protect(request: Request, call_next):
            sec = self.security
            if not sec.headers_ok(request):
                response = JSONResponse({"error": "Only same-origin loopback access is permitted"}, status_code=403)
            elif request.url.path.startswith(("/api/", "/ui/")) and not ((request.url.path == "/api/session" and request.method == "POST") or (request.url.path == "/api/identity" and request.method == "GET")) and not sec.session(request):
                response = (views.response('login.html') if request.url.path.startswith('/ui/') and request.method == 'GET' and not request.headers.get('hx-request')
                            else JSONResponse({"error": "Unlock with your private local dashboard link"}, status_code=401))
            elif request.method not in {"GET", "HEAD"} and request.url.path != "/api/session" and not hmac.compare_digest(request.headers.get("x-applypilot-csrf", ""), sec.session(request) or "invalid"):
                response = JSONResponse({"error": "The browser session changed. Refresh its action token and retry once.", "code": "csrf_expired"}, status_code=403)
            else:
                try:
                    if self.read_only and request.url.path.startswith('/ui/actions/') and not request.url.path.endswith('/logout'):
                        raise PermissionError('This dashboard is read-only')
                    if (self.app_dir / 'github_sync' / 'restore-active.json').exists() and request.url.path.startswith('/ui/'):
                        permitted = ('/ui/backups', '/ui/operations', '/ui/activity', '/ui/fragments/backups/', '/ui/fragments/operations/',
                                     '/ui/fragments/activity/', '/ui/actions/backups/', '/ui/actions/setup/task/cancel', '/ui/status', '/ui/tasks/')
                        if not request.url.path.startswith(permitted):
                            raise ValueError('Recovery is applying. Other views are paused until it finishes or is rolled back.')
                    if (self.app_dir / "github_sync" / "restore-active.json").exists() and request.url.path.startswith("/api/") and not request.url.path.startswith(("/api/backups", "/api/session", "/api/identity", "/api/setup/tasks", "/api/operations")):
                        raise ValueError("Recovery is applying. Other views are paused until it finishes or is rolled back.")
                    response = await call_next(request)
                except GitHubCredentialError as exc:
                    response = JSONResponse(exc.details, status_code=400)
                except (ValueError, TypeError) as exc:
                    response = JSONResponse({"error": str(exc)[:300]}, status_code=400)
                except LookupError as exc:
                    response = JSONResponse({"error": str(exc)[:300]}, status_code=404)
                except PermissionError as exc:
                    response = JSONResponse({"error": str(exc)[:300]}, status_code=403)
                except postgres.Error as exc:
                    # A bad query/schema will not heal by retrying as "busy".
                    # Do not echo SQL or exception bodies into the browser.
                    sqlstate = getattr(exc,'sqlstate',None)
                    log.warning("Dashboard database request failed: route=%s sqlstate=%s",request.url.path,sqlstate,
                        extra={"ops_database_failure":True,"ops_route":request.url.path,"ops_sqlstate":sqlstate,
                               "ops_exception":exc})
                    if postgres.is_retryable(exc):
                        response = JSONResponse({"error": "The database is busy. Last displayed data is retained; retry shortly.",
                            "code": "database_busy"}, status_code=503, headers={"Retry-After": "2"})
                    elif sqlstate == "57014":
                        response = JSONResponse({"error": "The history query exceeded its time budget. Last displayed data is retained; narrow the filters or retry.",
                            "code": "database_query_timeout"}, status_code=503, headers={"Retry-After": "5"})
                    else:
                        response = JSONResponse({"error": "A database query or schema could not be read. Last displayed data is retained. Check the dashboard server log; this is not an empty-history result.",
                            "code": "database_query_failed"}, status_code=500)
                except Exception as exc:
                    log.warning("Dashboard request failed (%s); workers unaffected", type(exc).__name__,
                                extra={"ops_exception":exc})
                    response = JSONResponse({"error": "Dashboard request failed. Application workers are unaffected."}, status_code=500)
            response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer", "Cross-Origin-Resource-Policy": "same-origin",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
                "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
            return response
        @asynccontextmanager
        async def lifespan(app):
            self.live.start()
            try:
                yield
            finally:
                self.support_exports.close()
                self.work_exports.close()
                await self.live.close()
        return Starlette(routes=routes, middleware=[Middleware(BaseHTTPMiddleware, dispatch=protect)], lifespan=lifespan)


def token_for(app_dir):
    path = app_dir / "web-dashboard.token"
    app_dir.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.stat().st_size > 256:
            raise ValueError("Invalid dashboard token file")
        token = path.read_text(encoding="ascii").strip()
        if len(token) < 40:
            raise ValueError("Invalid dashboard token file")
        if os.name != "nt":
            path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(48)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(token + "\n")
    return token


class Dashboard:
    def __init__(self, server, thread, sock, url, registry, instance, setup=None, lifetime=None):
        self.server, self.thread, self.sock = server, thread, sock
        self.url, self.registry, self.instance = url, registry, instance
        self.setup = setup
        self.lifetime = lifetime

    def stop(self):
        # Stop only the local dashboard. Durable Workflows and Schedules are
        # untouched; cancellation is an explicit Temporal control request.
        if self.lifetime:
            self.lifetime.close()
        if self.setup and self.setup.jobs:
            self.setup.jobs.close()
        self.server.should_exit = True
        self.thread.join(timeout=4)
        if self.thread.is_alive():
            self.server.force_exit = True
        try:
            self.sock.close()
            if safe_json(self.registry.read_text()).get("instance") == self.instance:
                self.registry.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


def existing_url(app_dir, *, require_read_only=False):
    """Recognize only this directory's viewer, without revealing its token."""
    path = app_dir / "web-dashboard.json"
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            return None
        data = json.loads(path.read_text())
        port = integer(data["port"], 8765, 1024, 65535)
        token = token_for(app_dir)
        nonce = secrets.token_urlsafe(32)
        expected = hmac.new(token.encode(), ("identity:" + nonce).encode(), hashlib.sha256).hexdigest()
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/identity?nonce={nonce}")
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=.6) as response:
            value = json.loads(response.read(2048))
            if hmac.compare_digest(str(value.get("proof", "")), expected):
                if require_read_only and not value.get("read_only"):
                    return None
                return f"http://127.0.0.1:{port}/#token={token}"
    except (OSError, ValueError, KeyError):
        return None
    return None


def start(*, app_dir=None, port=8765, open_browser=False, read_only=False):
    from applypilot import config
    app_dir = Path(app_dir or config.APP_DIR).resolve()
    if app_dir != config.APP_DIR.resolve():
        raise ValueError("Set APPLYPILOT_DIR before starting Python to select a different data directory")
    port = integer(port, 8765, 1024, 65535)
    if not read_only:
        # A standalone dashboard must upgrade its schema before serving queries.
        from applypilot.database import init_db
        init_db(app_dir / "main.pgstore")
    token = token_for(app_dir)
    sock = None
    for candidate in range(port, min(port + 10, 65536)):
        attempt = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Do not enable SO_REUSEPORT: only one authenticated listener owns a port.
        try:
            attempt.bind(("127.0.0.1", candidate))
            attempt.listen(64)
            sock, port = attempt, candidate
            break
        except OSError:
            attempt.close()
    if sock is None:
        raise OSError("No free local dashboard port; use --web-port to choose another range")
    from applypilot.lifecycle import DashboardLifetime
    lifetime = DashboardLifetime(app_dir)
    server = thread = None
    try:
        api = API(app_dir, token, port, read_only, dashboard_owner=lifetime.owner)
        server = uvicorn.Server(uvicorn.Config(api.application(), host="127.0.0.1", port=port,
            log_level="warning", access_log=False, log_config=None, lifespan="on", ws="none",
            timeout_keep_alive=3, timeout_graceful_shutdown=5, limit_concurrency=64, proxy_headers=False))
        def run_server():
            try:
                server.run(sockets=[sock])
            finally:
                lifetime.close()
        thread = threading.Thread(target=run_server, name="applypilot-web", daemon=True)
        thread.start()
        deadline = time.monotonic() + 4
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.025)
        if not server.started:
            server.should_exit = True
            sock.close()
            raise RuntimeError("Web dashboard could not start")
        registry = app_dir / "web-dashboard.json"
        instance = lifetime.owner
        atomic_json(registry, {"port": port, "pid": os.getpid(), "instance": instance, "read_only": read_only})
        url = f"http://127.0.0.1:{port}/#token={token}"
        dashboard = Dashboard(server, thread, sock, url, registry, instance, api.setup, lifetime=lifetime)
        if open_browser:
            log.info("Explicit system-browser launch: dashboard --open (URL withheld)")
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
        return dashboard
    except BaseException:
        lifetime.close()
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=4)
        sock.close()
        raise



def serve(*, port=8765, open_browser=False, read_only=False):
    from applypilot import config
    config.load_env()
    existing = existing_url(config.APP_DIR, require_read_only=read_only)
    if existing:
        if not read_only:
            # Reopening a running dashboard also applies pending migrations.
            from applypilot.database import init_db
            init_db(config.DB_PATH)
        print("ApplyPilot dashboard: " + existing)
        if open_browser:
            log.info("Explicit system-browser launch: existing dashboard --open (URL withheld)")
            webbrowser.open(existing)
        return
    dashboard = start(port=port, open_browser=open_browser, read_only=read_only)
    print("ApplyPilot dashboard: " + dashboard.url)
    print("Opening this view does not start workers. Start/Resume is an explicit dashboard action.")
    print("Ctrl+C closes this dashboard only. Temporal Workflows and separately deployed workers remain independent.")
    print("Autopilot started separately in a terminal is independent; use its own Stop control.")
    try:
        while dashboard.thread.is_alive():
            time.sleep(.5)
    except KeyboardInterrupt:
        pass
    finally:
        dashboard.stop()

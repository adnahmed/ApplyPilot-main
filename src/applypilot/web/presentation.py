"""Small, factual display models over saved task and connection observations."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

from applypilot.onboarding.jobs import COMPONENT_LABELS, task_identity
from applypilot.onboarding.storage import read_json
from applypilot.operations.runtime import sanitize


def _read(path, limit=1_000_000):
    try:
        value = read_json(path, {}, limit=limit)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _timestamp(value):
    if isinstance(value, (float, int)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0


def humanize_issue(reason, component=""):
    """Explain known result codes while retaining recorded error explanations."""
    if isinstance(reason, dict):
        reason = reason.get("message") or reason.get("detail") or reason.get("error") or reason.get("summary") or reason.get("code")
    text = str(reason or '').strip()
    if text.removeprefix('failed:') == 'application_route_unresolved':
        return 'Could not reach an application form for this job. The attempt stopped before submission.'
    if '[argument omitted]' in text:
        text = text.split('[argument omitted]', 1)[0].rstrip(': ,;=-')
        text = re.sub(r'\s+(?:for|from|to|at|in|after|with)$', '', text).strip()
    return sanitize(text, 4000) or "No explanation was recorded."


def _component(root, task):
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    valid = {*COMPONENT_LABELS, "backup", "outreach", "material", "settings", "connections"}
    for candidate in (task.get("component"), result.get("component")):
        if candidate in valid and candidate != "connections":
            return candidate
    operation = str(task.get("operation") or "")
    if operation != "verify":
        return task_identity(operation, {"provider": result.get("provider")})["component"]
    names = {str(row.get("component") or "").lower() for row in result.get("checks", []) if isinstance(row, dict)}
    if names & {"ddgs", "playwright", "twikit", "linkedin_scraper", "searxng", "freehire credential"}:
        return "sources"
    for candidate in ("gmail", "llm", "browseros", "github"):
        if any(name.startswith(candidate) for name in names):
            return candidate
    message = (str(task.get("message") or "") + " " + str(result.get("code") or "")).lower()
    if any(value in message for value in ("gmail", "google", "oauth_expired", "invalid_grant")):
        return "gmail"
    if "browseros" in message or "page_id_missing" in message:
        return "browseros"
    identifier = str(task.get("id") or task.get("task_id") or "")
    if re.fullmatch(r"[0-9a-f]{24}", identifier):
        source = _read(Path(root) / "setup" / "tasks" / (identifier + ".input.json"), 16_384)
        payload = source.get("payload") if isinstance(source.get("payload"), dict) else {}
        candidate = payload.get("component")
        if candidate in COMPONENT_LABELS:
            return candidate
    return "connections"


def _action(component):
    destinations = {"backup": ("/ui/settings?section=backups", "Open backups"),
                    "material": ("/ui/evidence", "Open evidence & impact"),
                    "outreach": ("/ui/outreach", "Open outreach"),
                    "settings": ("/ui/settings", "Open settings")}
    if component in destinations:
        href, label = destinations[component]
    else:
        target = component if component in {'gmail', 'github', 'sources', 'browseros', 'linkedin', 'x', 'llm'} else 'llm'
        href = "/ui/settings?section=connections#connection-" + target
        label = "Open " + COMPONENT_LABELS.get(component, "connection settings")
    return {"action_href": href, "action_label": label}


def _failed_check(row):
    name = str(row.get("component") or "Check")
    detail = str(row.get("detail") or row.get("error_code") or "Check failed")
    return name + ": " + humanize_issue(detail)


def task_view(root, task, allow_auth=False):
    task = task if isinstance(task, dict) else {}
    component = _component(root, task)
    operation = str(task.get("operation") or "")
    metadata = task_identity(operation, {"component": component})
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    state = str(task.get("state") or "queued")
    failed = [row for row in result.get("checks", []) if isinstance(row, dict) and row.get("ok") is False and not row.get("skipped")]
    message = "; ".join(_failed_check(row).rstrip(". ") for row in failed[:2]) if failed else result.get("error") or task.get("message") or result.get("message") or result.get("note")
    if state == "completed" and operation == "github_connect":
        message = "GitHub account identity verified."
    elif state == "completed" and operation == "gmail_connect":
        message = "Gmail sign-in and mailbox identity verified; no message sent."
    if state == "queued":
        message = "Waiting to start. Progress will appear here automatically."
    elif state == "running" and (not message or str(message).startswith(("Starting", "Working on"))):
        title = metadata["display_title"]
        message = ("Checking " + title.removeprefix("Check ") if title.startswith("Check ") else title) + "…"
    labels = {"queued": "Queued", "running": "Running", "completed": "Completed", "failed": "Failed",
              "interrupted": "Interrupted", "cancelled": "Stopped", "timed_out": "Timed out", "deferred": "Waiting"}
    view = {"id": task.get("id") or task.get("task_id") or "", "title": metadata["display_title"],
            "category": "Backup" if component == "backup" else "Material" if component == "material" or operation == "github" else
                        "Settings" if component == "settings" else "Outreach" if component == "outreach" else "Connection",
            "component": component, "state": state, "status_label": labels.get(state, state.replace("_", " ").capitalize()),
            "message": humanize_issue(message, component), "at": task.get("finished_at") or task.get("started_at"),
            "historical": state not in {"queued", "running"}, **_action(component)}
    if (component == "sources" and failed and state == "failed"
            and any(isinstance(row, dict) and row.get("ok") is True for row in result.get("checks", []))):
        view["status_label"] = "Partly available"
    if state == 'completed' and operation in {'social_connect', 'session_import', 'session_reuse'} and result.get('verified') is not True:
        view['status_label'] = 'Session saved'
    if operation == "github":
        view.update(action_href="/ui/evidence", action_label="Open repository evidence")
    if component == "gmail" and any(word in str(message).lower() for word in ("google", "oauth", "refresh token")):
        view["action_label"] = "Reconnect Gmail"
    if component == "backup" and state == "failed" and any(word in str(message).lower() for word in ("locked", "exit 11")):
        health = _read(Path(root) / "github_sync" / "repository-health.json")
        if health.get("status") == "accessible" and _timestamp(health.get("checked_time")) > _timestamp(view["at"]):
            view.update(status_label="Recovered since", next_action="Repository access was verified after this failure.")
    if allow_auth and state in {"queued", "running"}:
        url = task.get("auth_url")
        if isinstance(url, str) and url.startswith("https://"):
            view["auth_url"] = url
        code = task.get("device_code")
        if isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code):
            view["device_code"] = code
    return view


def connection_rows(root):
    """One latest saved observation per integration; no connectivity probes on page reads."""
    root = Path(root)
    from applypilot.onboarding.environment import child_env
    from applypilot.onboarding.gmail_state import status as gmail_status
    env = child_env(root)
    gmail = gmail_status(root, env)
    saved = _read(root / "setup" / "checks.json")
    accounts = _read(root / "unattended.json").get("accounts") or {}
    github = _read(root / "setup" / "github-auth.json")
    from applypilot.onboarding.sessions import status as session_status
    sessions = session_status(root, env)
    configured = {"llm": bool(env.get("LLM_URL") or any(env.get(key) for key in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"))),
                  "gmail": gmail.get("configured", False), "github": bool(github.get("login") or env.get("GITHUB_TOKEN") or env.get("GH_TOKEN")),
                  "browseros": bool(env.get("BROWSEROS_MCP_URL")), "sources": True,
                  "linkedin": any(row['session_ready'] for row in sessions if row['provider'].startswith('linkedin')),
                  "x": any(row['session_ready'] for row in sessions if row['provider'] == 'twikit')}
    observations = {key: [] for key in COMPONENT_LABELS}

    def observe(component, at, ok, message, *, label=None, state=None):
        if component in observations:
            observations[component].append({"at": at, "ok": ok, "message": message, "label": label, "state": state})

    for session in sessions:
        if session['session_ready']:
            path = Path(session['path'])
            at = datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat()
            component = 'linkedin' if session['provider'].startswith('linkedin') else 'x'
            observe(component, at, True, 'Account session saved. Check confirms live reader access.',
                    label='Session saved', state='configured')

    for group, record in saved.items():
        if not isinstance(record, dict):
            continue
        checks = [row for row in record.get("checks", []) if isinstance(row, dict)]
        at = record.get("checked_at")
        if group == 'sources' and record.get('probe_version') != 2:
            observe(group, at, True, 'Previous check only inspected installed tools. Check again to verify live search and reader access.',
                    label='Live check needed', state='configured')
            continue
        if group == 'gmail':
            from applypilot.onboarding.gmail_state import credential_fingerprint
            try:
                if record.get('credential_fingerprint') != credential_fingerprint(root, env):
                    continue
            except (OSError, ValueError):
                continue
        if group in {"llm", "gmail", "github", "sources"}:
            failed = [row for row in checks if row.get("ok") is False and not row.get('skipped')]
            message = "; ".join(_failed_check(row).rstrip(". ") for row in failed[:2]) if failed else (
                "Live search and reader requests completed. Unconfigured optional sources were skipped." if group == "sources" else
                "Gateway model catalog responded; inference is checked when used." if group == "llm" else
                "Mailbox identity and read access checked; no message sent." if group == "gmail" else "GitHub repository access checked.")
            observe(group, at, bool(record.get("ok")), message,
                    label="Partly available" if group == "sources" and failed and any(row.get("ok") for row in checks) else None)
        if group in {'sources', 'linkedin', 'x'}:
            for component, providers in (('linkedin', {'linkedin_scraper', 'linkedin_rs'}), ('x', {'twikit'})):
                matches = [row for row in checks if row.get('component') in providers and row.get('live') and not row.get('skipped')]
                if matches:
                    passed = next((row for row in matches if row.get('ok')), None)
                    failed = [row for row in matches if not row.get('ok')]
                    observe(component, at, not failed, failed[0].get('detail') if failed else passed.get('detail'),
                            label='Partly available' if passed and failed else None)
        if group in {"browseros", "authorization"}:
            for component in ("browseros", "gmail", "linkedin", "x"):
                if component == 'gmail':
                    continue  # Only grant-scoped Gmail health/checks establish its current state.
                matches = [row for row in checks if str(row.get("component") or "").startswith(component)]
                if matches:
                    failed = next((row for row in matches if row.get("ok") is False), None)
                    observe(component, at, failed is None, (failed or matches[0]).get("detail"))
    if github.get("verified_at"):
        observe("github", github["verified_at"], True, "GitHub account identity verified.")
    health_path = root / "setup" / "gmail-health.json"
    health = _read(health_path)
    if gmail.get('verified'):
        at = health.get("checked_at") or (datetime.fromtimestamp(health_path.stat().st_mtime).astimezone().isoformat() if health_path.exists() else None)
        observe("gmail", at, True, "Gmail sign-in was verified; no message sent.")
    paths = sorted((root / "setup" / "tasks").glob("*.state.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:30]
    for path in paths:
        task = _read(path)
        if task.get("operation") not in {"verify", "gmail_connect", "github_connect", "social_connect"} or task.get("state") not in {"failed", "completed", "timed_out"}:
            continue
        component = _component(root, task)
        result = task.get("result") if isinstance(task.get("result"), dict) else {}
        if component == 'gmail' and not gmail.get('health_current'):
            continue  # Old task failures describe another or an unidentified credential grant.
        if task.get("operation") == "verify" and result.get("checks"):
            continue  # The saved check has the complete per-service result, including partial availability.
        if task.get("state") == "completed":
            if task.get("operation") == "social_connect" and result.get("verified") is not True:
                if result.get('saved'):
                    view = task_view(root, task)
                    observe(component, view['at'], True, result.get('message') or 'Account session saved.',
                            label='Session saved', state='configured')
                continue
            if task.get("operation") == "verify":
                continue  # Successful checks are already recorded with their exact scope.
        view = task_view(root, task)
        observe(component, view["at"], task.get("state") == "completed", view["message"])
    rows = []
    for component in ("llm", "gmail", "github", "browseros", "linkedin", "x", "sources"):
        latest = max(observations[component], key=lambda item: _timestamp(item["at"]), default=None)
        row = {"component": component, "title": COMPONENT_LABELS[component].capitalize() if component == "sources" else COMPONENT_LABELS[component],
               "state": "configured" if configured[component] else "unchecked", "status_label": "Saved" if configured[component] else "Not connected",
               "message": gmail['message'] if component == 'gmail' else "Settings saved; check confirms live access." if configured[component] else "Connect or configure this integration.",
               "checked_at": None, **_action(component)}
        if latest:
            row.update(state=latest.get('state') or ("connected" if latest["ok"] else "needs_attention"), status_label=latest.get("label") or ("Connected" if latest["ok"] else "Needs attention"),
                       message=humanize_issue(latest["message"], component), checked_at=latest["at"])
        if component in {'linkedin', 'x'}:
            row['session_saved'] = configured[component]
            row['connect_label'] = 'Reconnect' if configured[component] else 'Connect'
            row['provider'] = 'linkedin_scraper' if component == 'linkedin' else 'twikit'
        if component == 'sources':
            source_check = saved.get('sources') or {}
            from applypilot.integrations.recovery import source_check_view
            row['checks'] = [source_check_view(check) for check in source_check.get('checks', [])
                             if isinstance(check, dict)] if source_check.get('probe_version') == 2 else []
        if component == "gmail" and gmail.get('reconnect_required'):
            row.update(state="needs_attention", status_label="Reconnect needed", message=gmail['message'], action_label="Reconnect Gmail")
        rows.append(row)
    return rows

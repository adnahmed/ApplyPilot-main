"""Authenticated web setup API, reusing core schema validators and existing policy guards."""
from __future__ import annotations
from pathlib import Path
import json
import os
import re
import time
import yaml
from . import environment, planning
from .storage import (atomic, backup, digest, now, public, read_bytes, read_json, require_stopped,
                      revision, running, setup_lock, write_json)


class Setup:
    def __init__(self,root,read_only=False):
        from applypilot.runtime_info import runtime_info
        runtime_info()  # Cache the boot identity; history from older processes is labelled separately.
        self.root=root;self.read_only=read_only;self.jobs=None
        if not read_only:
            from .jobs import Jobs
            self.jobs=Jobs(root)

    def brief(self):
        missing=[name for name in ("profile.json","searches.yaml","resume.txt") if not (self.root/name).is_file()]
        journal=read_json(self.root/"setup"/"commit.json",{})
        return {"missing_files":missing,"needs_setup":bool(missing) or journal.get("state")=="applying",
                "gate":gate_status(self.root),"running":running(self.root),"read_only":self.read_only}

    def state(self):
        from .checks import readiness
        from .integrations import state as integration_state
        from applypilot.integrations.registry import SOURCES
        value=planning.preview(self.root)
        from .portfolio import portfolio
        from applypilot.compensation import DEFAULT_OBJECTIVE
        from .workspace import inventory, material_sources
        from .gmail_state import status as gmail_status
        from applypilot.unattended.settings import validate
        cfg=validate(read_json(self.root/"unattended.json",{}))
        sources=material_sources(self.root)
        checked=read_json(self.root/"setup"/"checks.json",{})
        config_stamp=environment.stamp(self.root) + revision(self.root,("profile.json","unattended.json"))
        for item in checked.values(): item["stale"]=item.get("config_revision")!=config_stamp
        backups=[]
        for path in sorted((self.root/"setup"/"backups").glob("*/manifest.json"),reverse=True)[:12]:
            m=read_json(path,{})
            if "profile.json" in m.get("files",{}): backups.append({"id":path.parent.name,"created_at":m.get("created_at")})
        return {**self.brief(),**value,"integrations":integration_state(self.root),"workspace":inventory(self.root),
                "portfolio":portfolio(self.root,value["draft"]["profile"]),"compensation_default":DEFAULT_OBJECTIVE,
                "gmail":gmail_status(self.root,environment.child_env(self.root)),"sources":[{k:v for k,v in s.items() if k not in {"text","payload"}} for s in sources],
                "github":read_json(self.root/"setup"/"github.json",[],limit=32_000_000),"checks":checked,
                "readiness":readiness(self.root),"backups":backups,"tasks":self.jobs.status() if self.jobs else [],
                "source_registry":[{"repository":r,"status":s,"description":d} for r,s,d in SOURCES],
                "outreach":{k:cfg.get(k) for k in ("enabled","channels","daily_send_limit","send_grace_seconds")},
                "committed":read_json(self.root/"setup"/"commit.json",{})}

    def get(self,route):
        if route=="setup/profile":
            saved = read_json(self.root / 'profile.json', {}, limit=8_000_000)
            if not isinstance(saved, dict):
                raise ValueError('Saved profile must be an object')
            from .workspace import canonical_profile, connected_identity
            saved = canonical_profile(saved)
            profile = {section: public(saved.get(section) or {}) for section in planning.PROFILE_EDIT_FIELDS}
            from applypilot.compensation import preferences
            profile['compensation'] = preferences(saved.get('compensation') or {})
            from .workspace import resume_material
            from .profile_completion import completion
            return {"profile": profile, "resume_text": (resume_material(self.root) or {}).get('text', ''),
                    'completion': completion(self.root, saved, None if self.read_only else self.jobs),
                    'github_identity': connected_identity(self.root, 'github', saved),
                    'linkedin_identity': connected_identity(self.root, 'linkedin', saved)}
        if route == 'setup/portfolio':
            from .portfolio import portfolio
            return portfolio(self.root, read_json(self.root / 'profile.json', {}, limit=8_000_000))
        if route=="setup/searches":
            path = self.root / 'searches.yaml' if (self.root / 'searches.yaml').is_file() else self.root / 'searches.target.yaml'
            searches = yaml.safe_load(read_bytes(path).decode('utf-8-sig')) or {}
            if not isinstance(searches, dict):
                raise ValueError('Saved search preferences must be an object')
            return {"searches": searches}
        if route=="setup": return self.state()
        if route=="setup/brief": return self.brief()
        if route=="setup/connections": return environment.catalog(self.root)
        if route=="setup/tasks":
            from applypilot.runtime_info import runtime_info
            return {"tasks":self.jobs.status() if self.jobs else [], "runtime":runtime_info()}
        if route=="setup/runtime-policy":
            from applypilot.config import validate_discovery_runtime
            from applypilot.discovery.capabilities import CAPABILITIES,JOBSPY_BOARDS
            raw=read_bytes(self.root/"discovery_runtime.yaml")
            from .integrations import effective_policy, maximum_applied
            return {"revision":digest(raw),"policy":effective_policy(self.root),"maximum_applied":maximum_applied(self.root),
                    "providers":sorted(set(CAPABILITIES)|{f"jobspy:{b}" for b in JOBSPY_BOARDS})}
        raise LookupError("Unknown setup view")

    def post(self,route,data):
        if self.read_only:
            raise PermissionError("This dashboard is read-only")
        coordinated = {"setup/connections", "setup/commit", "setup/restore", "setup/gmail-client",
                       "setup/runtime-policy", "setup/max-local", "setup/import-session", "setup/mcp-binding"}
        if route in coordinated:
            from applypilot.control import maintenance
            if running(self.root):
                return self.jobs.launch("apply_change", {"route": route, "data": data})
            with maintenance(self.root):
                return self._post(route, data)
        return self._post(route, data)

    def _post(self,route,data):
        if self.read_only: raise PermissionError("This dashboard is read-only")
        if route=="setup/connections": return environment.save(self.root,data)
        if route=="setup/reveal-secret": return environment.reveal(self.root,data)
        if route=="setup/draft": return {"draft":planning.update(self.root,data)}
        if route=="setup/save-profile":
            result = planning.save_profile(self.root,data)
            from .profile_completion import completion
            saved = read_json(self.root / 'profile.json', {}, limit=8_000_000)
            progress = completion(self.root, saved, self.jobs)
            if progress.get('state') in {'queued', 'running', 'waiting'}:
                result['message'] += ' Missing details are being filled from your saved material.'
            return result
        if route=='setup/work-authorization': return planning.save_work_authorization(self.root,data)
        if route=="setup/save-searches": return planning.save_searches(self.root,data)
        if route=="setup/commit": return planning.commit(self.root,data)
        if route=="setup/restore": return planning.restore(self.root,data)
        if route=="setup/evidence":
            from .github import curate
            return curate(self.root,data)
        if route == 'setup/evidence/edit':
            from .evidence_edit import edit
            return edit(self.root, data)
        if route=="setup/gmail-client":
            from .gmail import upload_client
            return upload_client(self.root,data)
        if route=="setup/remove-source":
            with setup_lock(self.root):
                path=self.root/"setup"/"sources.json";sources=read_json(path,[])
                chosen=next((s for s in sources if s["id"]==data.get("id")),None)
                if not chosen: raise ValueError("Source not found")
                write_json(path,[s for s in sources if s["id"]!=chosen["id"]])
                (self.root/"setup"/"uploads"/(chosen["id"]+".pdf")).unlink(missing_ok=True)
            return {"removed":True,"note":"The selected intake copy was removed. Already generated drafts/backups are separate and remain local."}
        if route=="setup/launch":
            if data.get("confirm") is not True:
                raise ValueError("Confirm starting applications with your committed settings")
            from applypilot.control import action
            return action(self.root, {"action": "start"})
        if route=="setup/runtime-policy":
            from .integrations import save_policy
            return save_policy(self.root,data)
        if route=="setup/max-local":
            from .integrations import maximum
            return maximum(self.root,data)
        if route=="setup/import-session":
            from .sessions import import_file
            return import_file(self.root,data)
        if route=="setup/mcp-binding":
            from applypilot.integrations.mcporter import bind
            return bind(self.root,data)
        task=route.removeprefix("setup/task/")
        if route.startswith("setup/task/"):
            if task=="cancel": return self.jobs.cancel(data.get("id"))
            result = self.jobs.launch(task,data)
            if task == 'suggest' and data.get('apply_profile') is True and result.get('task_id'):
                from .profile_completion import remember
                remember(self.root, result['task_id'])
            return result
        raise LookupError("Unknown setup action")


def gate_status(root):
    row=read_json(root/"setup"/"gate.json",{})
    if not row: return {"waiting":False}
    # The marker is not enough: only a live process heartbeat offers a Start button.
    fresh=time.time()-float(row.get("heartbeat",0))<8
    return {"waiting":row.get("waiting") is True and fresh,"nonce":row.get("nonce") if fresh else None}


def signal_launch(root,data):
    restart = read_json(root/"github_sync"/"restart-required.json", {})
    gate_owner = read_json(root/"setup"/"gate.json", {})
    if restart.get("pid") and restart.get("pid") == gate_owner.get("pid"):
        raise ValueError("Recovery finished. Stop the waiting setup command and restart autopilot so it opens the restored databases. No profile/search wizard is required.")
    from .checks import readiness
    gate=gate_status(root)
    if not gate["waiting"] or not isinstance(data.get("nonce"),str) or data["nonce"]!=gate.get("nonce"):
        raise ValueError("No autopilot process is waiting in setup. Start the same autopilot command from your terminal/service.")
    rows=readiness(root)
    problems=[r["component"] for r in rows if r["required"] and not r["ok"]]
    if problems: raise ValueError("Resolve required setup items first: "+", ".join(problems))
    if data.get("confirm") is not True: raise ValueError("Confirm starting the normal application pipeline")
    write_json(root/"setup"/"launch.json",{"nonce":gate["nonce"],"revision":revision(root),"at":now()})
    return {"starting":True,"command":"applypilot autopilot","new_process_spawned":False}

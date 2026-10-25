"""Allowlisted environment controls; saved secrets are exposed only by explicit owner POST."""
from __future__ import annotations
import io
import re
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import dotenv_values, set_key, unset_key
from .env_keys import ENV_HINTS
from .resolution import application_values, gateway_values, stamp, display_defaults, registry
from .storage import atomic, backup, read_bytes, setup_lock

GROUPS = {
    "llm": ("AI & LiteLLM", "Your existing model gateway for scoring, writing and setup suggestions."),
    "browser": ("BrowserOS & application browser", "Reuse your logged-in browser. BrowserOS and the earlier source integrations coexist."),
    "gmail": ("Gmail", "Authorize your own mailbox once; connection checks never send a message."),
    "backup": ("GitHub backup & recovery", "Private GitHub repository access. Configure schedules in Backups; these values are never uploaded."),
    "github": ("GitHub evidence", "Reuse the saved GitHub account. Private intake is opt-in; model sharing and public disclosure require separate review. No repository code is executed."),
    "sources": ("Free search & social readers", "Existing DDGS, SearXNG, Playwright, X and LinkedIn source adapters."),
    "alerts": ("Notifications", "Your private exception/digest webhook. Treat its URL as a password."),
    "gateway": ("Gateway provider keys · advanced", "Optional keys for the separately managed LiteLLM fabric. No new paid enrichment services."),
    "runtime": ("Runtime · advanced", "Installation-specific settings. Executables and arbitrary MCP command files stay local-file managed."),
}
LABELS = {
 "LLM_URL":"LiteLLM / OpenAI-compatible URL", "LLM_API_KEY":"Gateway API key",
 "BROWSEROS_MCP_URL":"BrowserOS MCP endpoint", "APPLYPILOT_BROWSER_RUNTIME":"Application browser runtime",
 "GITHUB_TOKEN":"GitHub read-only token", "GH_TOKEN":"GitHub token (legacy alias)",
 "SEARXNG_URL":"Your SearXNG URL", "APPLYPILOT_SEARCH_BACKEND":"Search backend",
 "APPLYPILOT_CRAWLER":"Page reader", "MCP_CONFIG_DIR":"Gmail configuration folder",
 "APPLYPILOT_ALERT_WEBHOOK":"Private notification webhook",
 "APPLYPILOT_AGENT_BASE_URL":"Claude Code gateway URL", "APPLYPILOT_AGENT_API_KEY":"Claude Code gateway key",
 "FREEHIRE_ENABLED":"Use FreeHire", "FREEHIRE_TOKEN":"FreeHire token", "FREEHIRE_API_URL":"FreeHire API URL",
}
CHOICES = {"FREEHIRE_ENABLED": ["true", "false"], "APPLYPILOT_LEARNING_MODE": ["full", "optimize", "capture"], "APPLYPILOT_BROWSER_RUNTIME":["auto","browseros","playwright"],
           "APPLYPILOT_SEARCH_BACKEND":["auto","ddgs","searxng"],"APPLYPILOT_CRAWLER":["auto","hybrid","http","playwright"],
           "APPLYPILOT_SOURCE_RUNTIME":["auto","local","docker"],"APPLYPILOT_GMAIL_RUNTIME":["auto","local","docker"],
           "APPLYPILOT_RESEARCH_ON_ENRICH":["0","1"]}
LOCAL_ONLY = {"APPLYPILOT_MCP_CONFIG","APPLYPILOT_LINKEDIN_RS_BIN","CHROME_PATH"}
LOCAL_ONLY |= {k for k in ENV_HINTS if k.endswith("_PYTHON")}
RETIRED_MODEL_KEYS={"LLM_MODEL","LLM_MODEL_QUALITY","APPLYPILOT_AGENT_MODEL","ANTHROPIC_DEFAULT_HAIKU_MODEL","ANTHROPIC_DEFAULT_SONNET_MODEL"}
LOCAL_ONLY |= RETIRED_MODEL_KEYS
LOCAL_ONLY |= {"APPLYPILOT_DATABASE_URL", "APPLYPILOT_PGDATA"}
SECRET = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|COOKIE|PROXY|WEBHOOK|CLIENT_ID|DATABASE_URL", re.I)


def group(key):
    if key in {'APPLYPILOT_SEC_USER_AGENT', 'DEALROOM_API_KEY', 'CRUNCHBASE_API_KEY', 'MAGNITT_API_KEY'}:
        return 'sources'
    if key.startswith("APPLYPILOT_BACKUP_"): return "backup"
    if key in {"LLM_URL","LLM_API_KEY","LLM_MODEL","LLM_MODEL_QUALITY","GEMINI_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY"}: return "llm"
    if key in {"GITHUB_TOKEN","GH_TOKEN"}: return "github"
    if key == "APPLYPILOT_GMAIL_RUNTIME": return "gmail"
    if key == "APPLYPILOT_SOURCE_RUNTIME": return "sources"
    if key == "AUTH_SERVER_PORT" or key.startswith(("GMAIL_", "MCP_CONFIG_DIR")) or key in {"CLIENT_ID","CLIENT_SECRET","REFRESH_TOKEN","AUTH_SERVER_PORT"}: return "gmail"
    if "WEBHOOK" in key: return "alerts"
    if key.startswith(("BROWSEROS_","CHROME_","APPLYPILOT_AGENT_")) or key == "APPLYPILOT_BROWSER_RUNTIME": return "browser"
    if key.startswith(("FREEHIRE_", "APPLYPILOT_DDGS","APPLYPILOT_CRAWL","APPLYPILOT_TW","APPLYPILOT_LINKEDIN","APPLYPILOT_MCP","APPLYPILOT_SEARCH","APPLYPILOT_RESEARCH","SEARXNG","XACTIONS")): return "sources"
    if SECRET.search(key) or "API_BASE" in key or re.search(r"_KEY_\d+$",key): return "gateway"
    return "runtime"


def values(root: Path):
    raw = read_bytes(root / ".env", 1_000_000).decode("utf-8-sig")
    # No ${...} expansion here: keys/credentials must not silently substitute each other.
    return {k: v or "" for k, v in dotenv_values(stream=io.StringIO(raw), interpolate=False).items()}


def child_env(root):
    env, _ = application_values(root)
    env["APPLYPILOT_DIR"] = str(root)
    env["TELEMETRY_ENABLED"] = "false"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def catalog(root):
    saved = values(root)
    effective, origins = application_values(root)
    gateway, gateway_path = gateway_values()
    fields = []
    defaults=display_defaults(root,effective)
    for key, hint in registry(root).items():
        val = effective.get(key, "")
        origin = origins.get(key, {"kind":"unset", "label":"Optional / not configured", "path":None})
        configured = bool(val) and origin.get("kind") != "default"
        # Only gateway fields may be read from the independent Compose scope.
        external = bool(gateway.get(key)) and (group(key) == "gateway" or (not val and key in {"GEMINI_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY"}))
        if external:
            val = gateway[key]; configured = True
            origin = {"kind":"gateway", "label":"Docker gateway .env", "path":gateway_path}
        default = defaults.get(key, "")
        if not val and default:
            val = default
            origin = {"kind":"default", "label":"Runtime default", "path":None}
        secret = bool(SECRET.search(key)) and not key.endswith(("_PATH", "_DIR", "_DB", "_COOKIES"))
        if "://" in val:
            try:
                u = urlsplit(val)
                secret = secret or bool(u.username or u.password or u.query or u.fragment)
            except ValueError:
                secret = True
        fields.append({"key":key,"label":LABELS.get(key,key.replace("_"," ").title()),
            "group":group(key),"secret":secret,"value":"" if secret else val,
            "configured":configured,"stored":key in saved,"local_only":key in LOCAL_ONLY or external,
            "external":external,"origin":origin,"default":default,"effective_default":origin["kind"]=="default",
            "process_differs":key in saved and saved[key] != effective.get(key,""),
            "choices":CHOICES.get(key,[]),"hint":("Legacy model override retained; current workload aliases are application policy." if key in RETIRED_MODEL_KEYS else hint),
            "placeholder":"•••••••• · saved" if secret and configured else "Optional"})
    return {"fields":fields,"groups":[{"id":k,"title":v[0],"description":v[1]} for k,v in GROUPS.items()],
            "revision":stamp(root),"workspace":str(root),
            "local_services":local_services(),
            "unmanaged_keys":sorted(set(saved)-set(registry(root))),
            "notice":"Existing settings are detected automatically. Defaults do not require saving. Docker gateway values are shown read-only; application saves do not rewrite the gateway. Dashboard changes pause and resume managed workers automatically; stopped workers use the latest settings when you press Play."}


def reveal(root, data):
    """Single allowlisted secret, explicit POST only; never part of catalog/state/logs."""
    if set(data)-{"key","revision","confirm"} or data.get("confirm") is not True:
        raise ValueError("Confirm showing this saved secret on this device")
    current=catalog(root)
    if data.get("revision") != current["revision"]:
        raise ValueError("Settings changed; refresh before revealing a secret")
    field=next((f for f in current["fields"] if f["key"]==data.get("key")),None)
    if not field or not field["secret"] or not field["configured"]:
        raise ValueError("Choose a configured secret field")
    vals,_=application_values(root)
    if field["external"]: vals,_=gateway_values()
    return {"key":field["key"],"value":vals.get(field["key"],""),"hide_after_seconds":30}


def validate_key(key, val):
    if key not in ENV_HINTS or key in LOCAL_ONLY:
        raise ValueError("Unknown or local-file-managed setting: " + str(key)[:80])
    if not isinstance(val,str) or len(val)>8192 or any(c in val for c in ("\n","\r","\x00")):
        raise ValueError(key+" must be a single line of at most 8192 characters")
    if val and key in CHOICES and val not in CHOICES[key]:
        raise ValueError(key+" must be one of "+", ".join(CHOICES[key]))
    if key in {"APPLYPILOT_RESOURCE_WORKERS", "APPLYPILOT_BROWSER_WORKERS", "APPLYPILOT_PREPARATION_MAX_PENDING"} and val:
        if not val.isascii() or not val.isdigit() or int(val) < 1:
            raise ValueError(key + " must be a positive integer")
    if key == "APPLYPILOT_DB_POOL_SIZE" and val not in {"", "auto"}:
        if not val.isascii() or not val.isdigit() or int(val) < 1:
            raise ValueError(key + " must be auto or a positive integer")
    if key == "APPLYPILOT_RESEARCH_COMPANIES" and val:
        if val != "-1" and not val.isdigit():
            raise ValueError("Research companies must be -1 (uncapped), 0 (paused), or a positive integer")
        from applypilot.limits import work_limit
        work_limit(int(val), "research companies")
    if val and (key.endswith(("_URL","_BASE","_BASE_URL")) or key=="APPLYPILOT_ALERT_WEBHOOK"):
        u=urlsplit(val)
        if u.scheme not in {"http","https"} or not u.hostname or u.username or u.password or u.fragment:
            raise ValueError(key+" must be an HTTP(S) endpoint without embedded credentials or a fragment")
        if u.scheme=="http" and u.hostname not in {"127.0.0.1","localhost","::1"}:
            raise ValueError(key+" requires HTTPS outside this computer")
    return val


def save(root, data):
    if set(data)-{"revision","values","clear"}: raise ValueError("Unknown connection setting payload")
    patch=data.get("values",{});clear=data.get("clear",[])
    if not isinstance(patch,dict) or not isinstance(clear,list) or len(patch)+len(clear)>250:
        raise ValueError("Invalid settings update")
    numeric = {'APPLYPILOT_RESOURCE_WORKERS', 'APPLYPILOT_BROWSER_WORKERS', 'APPLYPILOT_PREPARATION_MAX_PENDING'}
    patch = {key: ('' if value is None else str(value)) if key in numeric and (value is None or type(value) is int) else value
             for key, value in patch.items()}
    external={f['key'] for f in catalog(root)['fields'] if f['external']}
    if (set(patch)|set(clear)) & external:
        raise ValueError("These keys are managed by the Docker gateway environment, shown read-only here")
    for key,val in patch.items(): validate_key(key,val)
    for key in clear: validate_key(key,"")
    if {"APPLYPILOT_RESOURCE_WORKERS", "APPLYPILOT_BROWSER_WORKERS"} & (set(patch) | set(clear)):
        effective, _ = application_values(root)
        effective.update(patch)
        effective.update({key: "" for key in clear})
        workers = int(effective.get("APPLYPILOT_RESOURCE_WORKERS") or 4)
        browsers = int(effective.get("APPLYPILOT_BROWSER_WORKERS") or min(2, workers))
        if browsers > workers:
            raise ValueError("Browser slots cannot exceed shared workers")
    with setup_lock(root):
        raw=read_bytes(root/".env",1_000_000)
        if data.get("revision")!=stamp(root):
            raise ValueError("Connection settings changed in another tab or process. Reload before saving.")
        # Use dotenv's quoting/parser and its atomic rewrite, preserving unrelated keys/comments.
        folder=backup(root,[".env"])
        target=root/"setup"/"env-staging"
        atomic(target,raw.decode("utf-8-sig").encode("utf-8"))
        try:
            for key,val in {**patch,**{k:"" for k in clear}}.items():
                if key in CHOICES and not val:
                    unset_key(str(target),key)
                else:
                    set_key(str(target),key,val,quote_mode="always")
            atomic(root/".env",read_bytes(target,1_000_000))
        finally:
            target.unlink(missing_ok=True)
    return {"saved":True,"restart_required":True,"backup":folder.name,"catalog":catalog(root)}


def local_services():
    from applypilot.integrations.container_runtime import runtime
    rt=runtime()
    return {"source_tools_running":bool(rt),"image":rt["image"] if rt else None,
      "command":"docker compose -f docker-compose.litellm.yml up -d --build searxng source-tools",
      "directory":"docs/guides", "searxng_default":"http://127.0.0.1:8888",
      "note":"Local SDKs need no host installs. External sites and account logins are still required. Multiple source-tools containers require stopping the extra instance."}

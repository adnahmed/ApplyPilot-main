"""Reuse owner-managed MCP configuration; no installer, token import or shell endpoint.

Browser discovery reads configuration only and probes loopback endpoints. Full
MCP discovery/auth is an explicit dashboard job, with short per-server timeouts.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import threading
import time
import tomllib
from urllib.parse import urlsplit

from .types import SourceError

_cache = {}
_lock = threading.Lock()
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}")


def command():
    """Avoid cmd.exe interpretation of JSON arguments in npm's Windows shim."""
    path = shutil.which("mcporter") or shutil.which("mcporter.cmd")
    if not path:
        return None
    if Path(path).suffix.lower() in {".cmd", ".bat", ".ps1"}:
        node = shutil.which("node")
        base = Path(path).parent / "node_modules" / "mcporter"
        try:
            package = json.loads((base / "package.json").read_text(encoding="utf-8"))
            entry = package["bin"]
            entry = entry.get("mcporter") if isinstance(entry, dict) else entry
            target = (base / entry).resolve()
            if node and target.is_file() and target.is_relative_to(base.resolve()):
                return [node, str(target)]
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return None
    return [path]


def jsonc(text):
    """Remove comments/trailing commas outside strings, without damaging URLs."""
    out, i, quoted, escaped = [], 0, False, False
    while i < len(text):
        c = text[i]
        if quoted:
            out.append(c)
            if escaped: escaped = False
            elif c == "\\": escaped = True
            elif c == '"': quoted = False
            i += 1; continue
        if c == '"':
            quoted = True; out.append(c); i += 1; continue
        if text[i:i+2] == "//":
            end = text.find("\n", i); i = len(text) if end < 0 else end; continue
        if text[i:i+2] == "/*":
            end = text.find("*/", i+2)
            if end < 0: raise ValueError("Unclosed JSON comment")
            out.append(" "); i = end+2; continue
        out.append(c); i += 1
    # JSON strings have already survived intact; a second stateful pass removes
    # only commas immediately followed by a closing bracket outside a string.
    text = "".join(out); out = []; quoted = escaped = False
    for i, c in enumerate(text):
        if not quoted and c == "," and text[i+1:].lstrip().startswith(("}", "]")):
            continue
        out.append(c)
        if quoted:
            if escaped: escaped = False
            elif c == "\\": escaped = True
            elif c == '"': quoted = False
        elif c == '"': quoted = True
    return json.loads("".join(out))


def config_files(root=None):
    from applypilot.config import APP_DIR
    root = Path(root or APP_DIR); home = Path.home(); cwd = Path.cwd()
    paths = []
    explicit = os.environ.get("MCPORTER_CONFIG")
    if explicit:
        paths.append(Path(explicit).expanduser())
    for base in dict.fromkeys([cwd, root, home]):
        paths += [base / "config/mcporter.json", base / "config/mcporter.jsonc",
                  base / ".mcporter/mcporter.json", base / ".mcporter/mcporter.jsonc",
                  base / ".codex/config.toml", base / ".cursor/mcp.json",
                  base / ".mcp.json", base / "mcp.json", base / ".claude.json",
                  base / "opencode.json", base / "opencode.jsonc"]
    appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    for base in (appdata, xdg, home / "Library/Application Support"):
        paths += [base / "Code/User/mcp.json", base / "Cursor/User/mcp.json",
                  base / "opencode/opencode.json", base / "opencode/opencode.jsonc"]
    if os.environ.get("OPENCODE_CONFIG"):
        paths.insert(0, Path(os.environ["OPENCODE_CONFIG"]).expanduser())
    return list(dict.fromkeys(paths))


def definitions(root=None):
    """Configuration presence is not a healthy connection or permission to run."""
    found = {}
    for path in config_files(root):
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000: continue
            text = path.read_text(encoding="utf-8-sig")
            doc = tomllib.loads(text) if path.suffix == ".toml" else jsonc(text)
            if not isinstance(doc, dict): continue
            for group in ("mcp_servers", "mcpServers", "servers", "mcp"):
                servers = doc.get(group, {})
                if not isinstance(servers, dict): continue
                for name, cfg in servers.items():
                    if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(cfg, dict): continue
                    if name in found: continue
                    found[name] = {"name": name, "source": str(path), "config": cfg,
                                   "disabled": cfg.get("enabled") is False or cfg.get("disabled") is True}
        except (OSError, ValueError):
            continue
    return list(found.values())


def loopback_url(value):
    if not isinstance(value, str): return None
    try:
        p = urlsplit(value.strip())
        if (p.scheme in {"http", "https"} and p.hostname in {"127.0.0.1", "localhost", "::1"}
                and not p.username and not p.password and not p.query and not p.fragment):
            _ = p.port
            return value.strip()
    except ValueError:
        pass
    return None


def browser_candidates(root=None):
    from applypilot.config import APP_DIR
    from applypilot.onboarding.storage import read_json
    rows = []
    configured = definitions(root)
    configured_names = {r["name"] for r in configured}
    for row in configured:
        if row.get("disabled"): continue
        if "browseros" not in row["name"].lower(): continue
        cfg = row["config"]
        # Authenticated/custom-header transports must stay in their MCP client,
        # rather than dropping their authentication silently on direct HTTP.
        if cfg.get("headers") or cfg.get("http_headers") or cfg.get("auth"): continue
        url = loopback_url(cfg.get("url") or cfg.get("baseUrl") or cfg.get("serverUrl"))
        if url: rows.append({"name": row["name"], "url": url, "source": row["source"]})
    saved = read_json(Path(root or APP_DIR) / "setup" / "mcporter.json", {})
    for row in saved.get("servers", []):
        if row.get("name") in configured_names: continue
        if "browseros" in str(row.get("name", "")).lower() and row.get("status") == "ok":
            url = loopback_url(row.get("url"))
            if url: rows.append({"name": row["name"], "url": url, "source": "mcporter discovery"})
    return sorted(rows, key=lambda r: ("neo" not in r["name"].lower(), r["name"]))


def browser_endpoint(root=None, *, refresh=False):
    """An explicit endpoint wins, including an explicit blank to disable it."""
    if "BROWSEROS_MCP_URL" in os.environ:
        return os.environ["BROWSEROS_MCP_URL"].strip()
    key = (str(root), str(Path.home()), str(Path.cwd()))
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if not refresh and cached and cached[0] > now:
            return cached[1]
        rows = browser_candidates(root)
        for row in rows:
            p = urlsplit(row["url"])
            try:
                with socket.create_connection((p.hostname, p.port or (443 if p.scheme == "https" else 80)), timeout=.25):
                    _cache[key] = (time.monotonic()+30, row["url"])
                    return row["url"]
            except OSError:
                continue
        # Preserve the old local default as a last resort, not an environment setting.
        endpoint = "http://127.0.0.1:9012/mcp"
        _cache[key] = (time.monotonic()+10, endpoint)
        return endpoint


def invoke(args, *, timeout=12, allow_error_json=False):
    argv = command()
    if not argv:
        raise SourceError("mcporter is not available to this ApplyPilot process. Run the dashboard under the same OS account as mcporter.")
    import tempfile
    # Large/misbehaving upstream output never grows an in-memory pipe unboundedly.
    with tempfile.TemporaryFile() as out:
        try:
            result = subprocess.run([*argv, *args], stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            if out.tell() > 4_000_000: raise ValueError("oversized output")
            out.seek(0); data = json.loads(out.read().decode("utf-8-sig"))
            if result.returncode and not allow_error_json: raise ValueError("failed command")
            return data
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SourceError("mcporter did not return a bounded JSON result; inspect its local configuration", "network") from exc


def scan(root, data=None, progress=lambda **kw: None):
    from applypilot.onboarding.storage import now, setup_lock, write_json
    progress(message="Checking installed MCP servers with a 2-second per-server timeout; no login is started.")
    result = invoke(["list", "--json", "--timeout", "2000"], timeout=15, allow_error_json=True)
    if not isinstance(result, dict) or not isinstance(result.get("servers"), list):
        raise SourceError("Unsupported mcporter list JSON shape", "schema")
    rows = []
    for raw in result["servers"][:100]:
        if not isinstance(raw, dict): continue
        name = raw.get("name")
        if not isinstance(name, str) or not NAME.fullmatch(name): continue
        tools = raw.get("tools", [])
        names = [x.get("name") if isinstance(x, dict) else x for x in tools] if isinstance(tools, list) else []
        transport = raw.get("transport", "")
        match = re.search(r"https?://[^\s]+", transport) if isinstance(transport, str) else None
        url = loopback_url(match[0]) if match else None
        status = raw.get("status") if raw.get("status") in {"ok", "auth", "offline", "http", "error"} else "error"
        rows.append({"name": name, "status": status, "tools": [n for n in names if isinstance(n, str)],
                     "url": url, "tool_count": len(names), "can_auth": status == "auth"})
    saved = {"checked_at": now(), "servers": rows}
    with setup_lock(root): write_json(root / "setup" / "mcporter.json", saved)
    browser_endpoint(root, refresh=True)
    return {**saved, "note": "Only supported, authorized readers enter the pipeline. Other MCP servers remain available to their own clients."}


def binding(provider, root=None):
    from applypilot.config import APP_DIR
    from applypilot.onboarding.storage import read_json
    row = read_json(Path(root or APP_DIR) / "setup" / "integration-bindings.json", {}).get(provider)
    if not isinstance(row, dict) or row.get("read_only_confirmed") is not True:
        return None
    return row.get("server") if isinstance(row.get("server"), str) and NAME.fullmatch(row["server"]) else None


def bind(root, data):
    from .mcp import READ_TOOLS
    from applypilot.onboarding.storage import read_json, require_stopped, setup_lock, write_json
    require_stopped(root)
    provider, name = data.get("provider"), data.get("server")
    if provider not in READ_TOOLS or not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("Choose a supported social reader and a discovered MCP server")
    if data.get("read_only_confirmed") is not True:
        raise ValueError("Confirm this is your local reader with background writes and paid modes disabled")
    listed = read_json(root / "setup" / "mcporter.json", {}).get("servers", [])
    required = {"linkedin_mcp": "search_posts", "xactions": "x_search_tweets", "x_use": "search_tweets"}[provider]
    selected = next((r for r in listed if r.get("name") == name), None)
    if not selected or selected.get("status") != "ok" or required not in selected.get("tools", []):
        raise ValueError("Discover a healthy server exposing this reader's required search tool before binding it")
    with setup_lock(root):
        path = root / "setup" / "integration-bindings.json"
        rows = read_json(path, {})
        rows[provider] = {"server": name, "read_only_confirmed": True}
        write_json(path, rows)
    return {"saved": True, "restart_required": True}


def read(provider, name=None, arguments=None):
    from .mcp import READ_TOOLS, _arguments
    from .fallback import reader_error
    from types import SimpleNamespace
    server = binding(provider)
    if not server: raise SourceError("Choose this MCP reader in Connections first")
    listing = invoke(["list", server, "--json", "--timeout", "5000"], timeout=10)
    if not isinstance(listing, dict) or listing.get("name") != server or listing.get("status") != "ok":
        raise reader_error(listing)
    tools = {t["name"]: t for t in listing.get("tools", []) if isinstance(t, dict) and t.get("name") in READ_TOOLS[provider]}
    if name is None:
        return [{"name": n, "input_schema": t.get("inputSchema", {})} for n, t in tools.items()]
    if name not in tools: raise SourceError("This server does not expose the required read-only tool", "unsupported")
    args = _arguments(SimpleNamespace(inputSchema=tools[name].get("inputSchema", {})), arguments or {})
    data = invoke(["call", "--server", server, "--tool", name, "--args", json.dumps(args), "--output", "json", "--timeout", "120000"], timeout=125)
    if isinstance(data, dict) and (data.get("isError") or data.get("error") or data.get("ok") is False or data.get("success") is False):
        raise reader_error(data)
    if isinstance(data, dict) and ("structuredContent" in data or isinstance(data.get("content"), list)):
        from .mcp import _decode
        return _decode(SimpleNamespace(isError=data.get("isError", False),
            structuredContent=data.get("structuredContent"),
            content=[SimpleNamespace(**item) for item in data.get("content", []) if isinstance(item, dict)]))
    return data


def auth(root, data, progress=lambda **kw: None):
    """Use mcporter's own OAuth storage/browser for one owner-selected server."""
    from applypilot.onboarding.storage import read_json, require_stopped, setup_lock, write_json, now
    require_stopped(root)
    name = data.get("server")
    rows = read_json(root / "setup" / "mcporter.json", {})
    if (data.get("consent") is not True or not isinstance(name, str) or not NAME.fullmatch(name)
            or not any(r.get("name") == name for r in rows.get("servers", []))):
        raise ValueError("Select a discovered MCP server and confirm its browser sign-in")
    argv = command()
    if not argv:
        raise ValueError("mcporter is not installed for the dashboard's OS account")
    progress(message="Complete the browser sign-in opened by mcporter on the machine running ApplyPilot. Existing credentials are not deleted.")
    try:
        result = subprocess.run([*argv, "auth", name], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240, check=False)
    except subprocess.TimeoutExpired:
        raise ValueError("MCP sign-in timed out. No credential reset was requested; retry Authenticate.") from None
    if result.returncode:
        raise ValueError("MCP sign-in did not complete. Inspect that server in your existing MCP client; its details were not logged.")
    checked = invoke(["list", name, "--json", "--timeout", "5000"], timeout=10, allow_error_json=True)
    if not isinstance(checked, dict) or checked.get("name") != name or checked.get("status") != "ok":
        raise ValueError("Sign-in finished but MCP tools could not be verified. Run Discover again.")
    with setup_lock(root):
        rows = read_json(root / "setup" / "mcporter.json", {})
        for row in rows.get("servers", []):
            if row.get("name") == name:
                row.update(status="ok", can_auth=False, tool_count=len(checked.get("tools", [])),
                           tools=[t["name"] for t in checked.get("tools", []) if isinstance(t, dict) and isinstance(t.get("name"), str)])
        rows["checked_at"] = now()
        write_json(root / "setup" / "mcporter.json", rows)
    return {"connected": True, "server": name, "sent": 0,
            "note": "MCP tools verified. No deployment, repository write or outreach was performed."}

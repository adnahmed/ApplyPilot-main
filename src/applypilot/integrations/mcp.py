"""Explicit local MCP readers. No model-selected tools, writes, or paid mode.

The operator installs and trusts the executable separately. MCP annotations are
not an authorization boundary: this client enforces its own fixed allowlists.
"""
from __future__ import annotations

from applypilot import operations as ops

import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import re
import shutil

from .types import SourceError, bounded_int
from .worker import minimal_env

READ_TOOLS = {
    "linkedin_mcp": frozenset({
        "get_person_profile", "get_my_profile", "get_sidebar_profiles", "search_people",
        "get_company_profile", "get_company_posts", "search_companies", "get_company_employees",
        "search_jobs", "get_job_details", "get_saved_jobs", "get_feed", "search_posts",
        "get_inbox", "get_conversation", "search_conversations",
    }),
    "xactions": frozenset({"x_get_profile", "x_get_tweets", "x_search_tweets", "x_get_thread",
                             "x_get_replies", "x_get_mentions", "x_get_followers", "x_get_following"}),
    "x_use": frozenset({"search_tweets", "search_profile", "get_tweet", "get_metrics"}),
}
_ENV_ALLOWED = {
    "linkedin_mcp": frozenset({"LINKEDIN_BROWSER_PROFILE_DIR"}),
    "xactions": frozenset({"XACTIONS_SESSION_COOKIE"}),
    "x_use": frozenset(),
}


def configuration(provider: str, path=None):
    from applypilot.config import APP_DIR
    if provider not in READ_TOOLS:
        raise SourceError("Unsupported local MCP provider")
    path = Path(path or os.environ.get("APPLYPILOT_MCP_CONFIG", APP_DIR / "free_sources_mcp.json"))
    try:
        if path.stat().st_size > 64_000:
            raise ValueError
        whole = json.loads(path.read_text(encoding="utf-8"))
        cfg = whole[provider]
        if not isinstance(cfg, dict) or set(cfg) - {"command", "args", "env_passthrough", "cwd", "timeout", "read_only_confirmed"}:
            raise ValueError
        if cfg.get("read_only_confirmed") is not True:
            raise SourceError("Confirm a dedicated server with background writes disabled: read_only_confirmed=true")
        command = cfg["command"]
        args = cfg.get("args", [])
        names = cfg.get("env_passthrough", [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or any(not isinstance(a, str) for a in args):
            raise ValueError
        if not isinstance(names, list) or not set(names) <= _ENV_ALLOWED[provider]:
            raise SourceError("Only this provider's explicitly allowed session environment names may be passed")
        # No network installers or shell wrappers at runtime. Use a reviewed,
        # already installed executable, not npx/uvx/curl with unpinned packages.
        if Path(command).name.lower() in {"npx", "uvx", "uv", "pip", "bash", "sh", "cmd", "powershell", "curl", "wget"}:
            raise SourceError("Use an installed server executable, not an installer or shell wrapper")
        resolved = shutil.which(command) or str(Path(command).expanduser())
        if not Path(resolved).is_file():
            raise SourceError(f"Local MCP executable is not installed for {provider}")
        if any(re.search(r"(?i)(api[_-]?key|auth[_-]?token|password|cookie|secret|private[_-]?key)", a) for a in args):
            raise SourceError("Do not put credentials in MCP command arguments")
        if provider == "linkedin_mcp" and "--no-auto-import" not in args:
            args = [*args, "--no-auto-import"]
        timeout = bounded_int(cfg.get("timeout", 120), "timeout", 10, 180)
        cwd = str(Path(cfg["cwd"]).expanduser().resolve()) if cfg.get("cwd") else None
        if cwd is not None and not Path(cwd).is_dir():
            raise SourceError("Configured MCP working directory does not exist")
        return {"command": str(Path(resolved).resolve()), "args": args, "env_passthrough": names,
                "cwd": cwd, "timeout": timeout}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise SourceError("Invalid/missing free_sources_mcp.json; see docs/free-sources/mcp.example.json") from exc


def _arguments(tool, arguments):
    if not isinstance(arguments, dict) or len(json.dumps(arguments)) > 32_000:
        raise SourceError("MCP arguments must be a bounded JSON object", "schema")
    schema = tool.inputSchema or {}
    properties = schema.get("properties", {})
    if set(arguments) - set(properties):
        raise SourceError("Unknown MCP arguments; inspect sources mcp-tools", "schema")
    if set(schema.get("required", [])) - set(arguments):
        raise SourceError("Required MCP arguments missing; inspect sources mcp-tools", "schema")
    args = dict(arguments)
    # Bound all recognized collection/scroll parameters even if omitted by caller.
    for key in ("limit", "count", "max_results", "max_tweets", "maxResults"):
        if key in properties:
            args[key] = bounded_int(args.get(key, 20), key, 1, 50)
    for key in ("max_pages", "max_scrolls"):
        if key in properties:
            args[key] = bounded_int(args.get(key, 1), key, 1, 2)
    if "include_images" in properties:
        args["include_images"] = False
    if "platform" in properties:
        args["platform"] = "twitter"
    if "instance" in args:
        raise SourceError("Arbitrary federated-instance requests are outside this connector", "access_denied")
    return args


def _decode(result):
    from .fallback import reader_error
    if getattr(result, "isError", False):
        data = getattr(result, "structuredContent", None)
        texts = [str(c.text) for c in getattr(result, "content", []) if getattr(c, "type", "") == "text"]
        raise reader_error({"structured": data, "text": "\n".join(texts)[:16000]})
    data = getattr(result, "structuredContent", None)
    if data is None:
        texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
        if not texts or sum(map(len, texts)) > 2_000_000:
            raise SourceError("MCP reader returned no bounded text/JSON", "schema")
        try:
            values = [json.loads(text) for text in texts]
            data = values[0] if len(values) == 1 else values
        except ValueError:
            data = {"text": "\n".join(texts)}
    if len(json.dumps(data, ensure_ascii=True)) > 2_000_000:
        raise SourceError("MCP result exceeds 2 MB", "schema")
    if isinstance(data, dict) and (data.get("ok") is False or data.get("success") is False or data.get("error")):
        raise reader_error(data)
    return data


async def _exchange(provider, cfg, name=None, arguments=None):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = minimal_env(cfg["env_passthrough"])
    if provider == "xactions":
        env["XACTIONS_MODE"] = "local"  # Never forward remote/payment variables.
    async with asyncio.timeout(cfg["timeout"]):
        # Do not let upstream logs dump cookie/session objects into our log.
        with open(os.devnull, "w", encoding="utf-8") as errlog:
            async with AsyncExitStack() as stack:
                streams = await stack.enter_async_context(stdio_client(
                    StdioServerParameters(command=cfg["command"], args=cfg["args"],
                                          env=env, cwd=cfg["cwd"]), errlog=errlog))
                session = await stack.enter_async_context(ClientSession(*streams))
                await session.initialize()
                response = await session.list_tools()
                available = {t.name: t for t in response.tools if t.name in READ_TOOLS[provider]}
                if name is None:
                    return [{"name": t.name, "input_schema": t.inputSchema} for t in available.values()]
                if name not in available:
                    raise SourceError("Requested read tool is unavailable in this server version", "unsupported")
                args = _arguments(available[name], arguments or {})
                return _decode(await session.call_tool(name, args))


@ops.observe('mcp', 'read')
def read(provider, name=None, arguments=None, *, authorized=False, config_path=None):
    if authorized is not True:
        raise SourceError("Explicit authorization is required to use your local social session", "access_denied")
    if name is not None and name not in READ_TOOLS.get(provider, ()):
        raise SourceError("Tool is not in ApplyPilot's fixed read allowlist", "access_denied")
    from .mcporter import binding, read as mcporter_read
    if config_path is None and not os.environ.get("APPLYPILOT_MCP_CONFIG") and binding(provider):
        return mcporter_read(provider, name, arguments)
    cfg = configuration(provider, config_path)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_exchange(provider, cfg, name, arguments))
    raise SourceError("Call this synchronous bridge outside an active event loop")

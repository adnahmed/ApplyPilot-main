"""Browser runtime selection and strict MCP configuration."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Literal

from applypilot.apply.browser_actions import BROWSEROS_TOOLS

log = logging.getLogger(__name__)

_BROWSEROS_HELPER_TOOLS = (
    "open_application_page",
    "solve_browser_challenge",
    "record_provider_restriction",
    "capture_submission",
    "submit_application",
    "inspect_application_uploads",
    "upload_application_document",
    "read_browser_output",
    "inspect_application_route",
    "submit_email_application",
)

BrowserRuntimeName = Literal["browseros", "playwright", "gmail"]
BrowserRuntimeSetting = Literal["auto", "browseros", "playwright"]

_RUNTIME_CACHE_TTL = 30  # seconds
_runtime_cache: dict[str, tuple[RuntimeSelection, float]] = {}
_runtime_cache_lock = threading.Lock()


@dataclass(frozen=True, slots=True)
class RuntimeSelection:
    requested: str
    selected: BrowserRuntimeName
    reason: str
    browseros_url: str
    advertised_tools: tuple[str, ...] = ()


def browseros_url() -> str:
    from applypilot.integrations.mcporter import browser_endpoint
    return browser_endpoint()


async def _browseros_health_async(url: str) -> tuple[bool, str]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(url, terminate_on_close=False) as streams:
            read_stream, write_stream, _ = streams
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=4)
                tools = await asyncio.wait_for(session.list_tools(), timeout=4)
        names = {tool.name for tool in tools.tools}
        if {"run", "name_session"} <= names:
            return True, f"BrowserOS Neo healthy ({len(names)} tools)"
        return False, "BrowserOS application bridge requires run/name_session"
    except Exception as exc:
        return False, f"BrowserOS preflight failed: {type(exc).__name__}: {exc}"


def browseros_health(url: str | None = None) -> tuple[bool, str]:
    """Initialize the MCP session and list tools without opening a tab."""
    endpoint = url or browseros_url()
    try:
        async def bounded():
            return await asyncio.wait_for(_browseros_health_async(endpoint), timeout=12)
        return asyncio.run(bounded())
    except TimeoutError:
        return False, "BrowserOS preflight exceeded 12 seconds"
    except RuntimeError as exc:
        # Worker threads normally have no event loop; retain a precise failure
        # if an embedding invokes the check from an async loop.
        return False, f"BrowserOS preflight unavailable in running event loop: {exc}"


_bridge_cache: dict[str, tuple[tuple[str, ...], float]] = {}
_bridge_state_lock = threading.Lock()


def record_browser_service_failure(selection: RuntimeSelection, reason: str | None) -> None:
    """Invalidate readiness without making the watchdog wait for a running probe."""
    if selection.selected != "browseros" or reason not in {
        "browser_session_not_connected", "browser_transport_lost", "browser_bridge_unavailable",
    }:
        return
    with _bridge_state_lock:
        _bridge_cache.pop(selection.browseros_url, None)


async def _probe_bridge(selection, session_file):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from applypilot.apply.browseros_mcp import decode
    parameters = StdioServerParameters(command=sys.executable,
        args=["-m", "applypilot.apply.browseros_mcp", "--url", selection.browseros_url,
              "--session-file", str(session_file), "--probe"])
    # Capture only a sanitized bounded tail, never inherit bridge stderr into logs.
    import tempfile
    from applypilot.operations.runtime import sanitize
    with tempfile.TemporaryFile(mode="w+b") as errors:
        try:
            async with stdio_client(parameters, errlog=errors) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    names = tuple(t.name for t in listing.tools)
                    if not {"run", "snapshot", "act", "read"} <= set(names):
                        raise RuntimeError("browser_bridge_unavailable: application capabilities missing")
                    page = None
                    try:
                        created = decode(await asyncio.wait_for(session.call_tool("run", {
                            "code": 'return {page_id:await browser.pages.newPage("about:blank")};',
                        }), 10))
                        page = created.get("page_id") if isinstance(created, dict) else None
                        if type(page) is not int or page < 0:
                            page = None
                            raise RuntimeError("browser_bridge_unavailable: owned probe page missing")
                        response = await asyncio.wait_for(session.call_tool("read", {
                            "page": page, "format": "text",
                        }), 10)
                        content = decode(response)
                        # A blank page can return plain (even empty) text. The
                        # JSON decoder still checks tool errors before this.
                        if content is None and not any(block.type == "text" for block in response.content):
                            raise RuntimeError("browser_bridge_unavailable: owned probe read missing")
                    finally:
                        if page is not None:
                            closed = decode(await asyncio.wait_for(session.call_tool("run", {
                                "code": f'await browser.pages.close({page});'
                                        f'return {{closed:!(await browser.pages.list()).some(p=>p.pageId==={page})}};',
                            }), 10))
                            if closed != {"closed": True}:
                                raise RuntimeError("browser_bridge_unavailable: owned probe cleanup unconfirmed")
                    return names
        except Exception as exc:
            from applypilot.onboarding.diagnostics import leaves

            errors.seek(0, 2)
            errors.seek(max(0, errors.tell() - 3000))
            causes = "; ".join(f"{type(error).__name__}: {error}" for error in leaves(exc))
            stderr = errors.read(3000).decode("utf-8", errors="replace").strip()
            detail = sanitize(causes + (f"; {stderr}" if stderr else ""), 700)
            raise RuntimeError(f"browser_bridge_unavailable: {detail}") from exc


def probe_browser_bridge(selection: RuntimeSelection) -> RuntimeSelection:
    """One bounded adapter probe. Temporal owns retry timing after any failure."""
    from dataclasses import replace
    from uuid import uuid4
    from applypilot import config
    endpoint = selection.browseros_url
    with _bridge_state_lock:
        cached = _bridge_cache.get(endpoint)
        if cached and time.monotonic() - cached[1] < _RUNTIME_CACHE_TTL:
            return replace(selection, advertised_tools=cached[0])
    path = config.APP_DIR / "browseros-sessions" / ("probe-" + uuid4().hex + ".json")
    try:
        async def bounded():
            return await asyncio.wait_for(_probe_bridge(selection, path), 45)
        names = asyncio.run(bounded())
        advertised = tuple(dict.fromkeys((*names, *_BROWSEROS_HELPER_TOOLS)))
        with _bridge_state_lock:
            _bridge_cache[endpoint] = (advertised, time.monotonic())
        return replace(selection, advertised_tools=advertised)
    except Exception:
        record_browser_service_failure(selection, "browser_bridge_unavailable")
        raise


def requested_runtime(setting: str | None = None) -> BrowserRuntimeSetting:
    """Resolve CLI > saved environment > auto without opening any browser."""
    requested = (setting if setting is not None else
                 os.environ.get("APPLYPILOT_BROWSER_RUNTIME", "auto")).strip().lower()
    if requested not in {"auto", "browseros", "playwright"}:
        raise ValueError("browser runtime must be auto, browseros, or playwright")
    return requested


def select_runtime(setting: str | None = None) -> RuntimeSelection:
    requested = requested_runtime(setting)
    endpoint = browseros_url()
    if requested == "playwright":
        return RuntimeSelection(requested, "playwright", "Playwright explicitly selected", endpoint)

    cache_key = f"{requested}:{endpoint}"
    now = time.monotonic()

    # Fast path: return cached result if fresh (no lock needed for reads)
    cached = _runtime_cache.get(cache_key)
    if cached:
        result, ts = cached
        if now - ts < _RUNTIME_CACHE_TTL:
            return result

    # Slow path: serialized health check — only one thread hits BrowserOS at a time.
    # Prevents 20+ concurrent MCP connections from overwhelming the server.
    with _runtime_cache_lock:
        # Double-check after acquiring lock (another thread may have refreshed)
        cached = _runtime_cache.get(cache_key)
        if cached:
            result, ts = cached
            if now - ts < _RUNTIME_CACHE_TTL:
                return result

        healthy, reason = browseros_health(endpoint)
        if healthy:
            result = RuntimeSelection(requested, "browseros", reason, endpoint)
        elif requested == "browseros":
            raise RuntimeError(reason)
        else:
            log.warning("%s; selecting preflight Playwright fallback", reason)
            result = RuntimeSelection(requested, "playwright", reason, endpoint)

        _runtime_cache[cache_key] = (result, time.monotonic())
        return result


def build_browser_mcp_config(
    selection: RuntimeSelection,
    *,
    cdp_port: int,
    worker_id: int,
    attempt_id: str | None = None,
    context_root: str | None = None,
) -> dict:
    """Build the strict MCP configuration for one application agent."""
    if selection.selected == "browseros":
        from uuid import uuid4
        from applypilot import config

        session_file = config.APP_DIR / "browseros-sessions" / f"{attempt_id or uuid4().hex}.json"
        browser = {
            "browseros_neo": {
                "command": sys.executable,
                "args": ["-m", "applypilot.apply.browseros_mcp", "--url", selection.browseros_url,
                         "--session-file", str(session_file), *([] if attempt_id else ["--read-only"])],
            }
        }
        from applypilot.tracking.gmail_read_mcp import mcp_config
        browser["gmail"] = mcp_config()
    else:
        from applypilot.apply.launcher import _make_mcp_config

        browser = _make_mcp_config(cdp_port, worker_id=worker_id)["mcpServers"]
        if attempt_id:
            browser["cloudflare"] = {
                "command": sys.executable,
                "args": ["-m", "applypilot.apply.cloudflare_tool", "--cdp-port", str(cdp_port),
                         "--attempt-id", attempt_id],
            }

    # A narrow local MCP records intent/proof in PostgreSQL before and after the
    # outbound side effect.  It receives no secrets through command arguments.
    if attempt_id:
        browser["applypilot_control"] = {
            "command": sys.executable,
            "args": [
                "-m",
                "applypilot.apply.control_mcp",
                "--attempt-id", attempt_id,
            ],
            "env": {"APPLYPILOT_CONTEXT_STAGE": "applications"},
        }

    if context_root and "browseros_neo" in browser:
        browser["browseros_neo"].setdefault("env", {})["APPLYPILOT_CONTEXT_ROOT"] = context_root
    return {"mcpServers": browser}


async def capture_external_tool_catalogs(servers: dict, trace_id: str, env: dict, *, allowed_tools=None) -> None:
    """Capture schemas for external servers that cannot record their own catalog.

    Listing tools performs no browser actions. Close this catalog-only connection
    before the SDK starts the application session.
    """
    import tempfile
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from applypilot.reasoning.learning_store import capturing, remember_tools

    if not capturing() or not trace_id:
        return
    for name in ("playwright", "cloudflare"):
        if name not in servers:
            continue
        server = servers[name]
        parameters = StdioServerParameters(command=server["command"], args=server.get("args", []),
                                            env={**env, **server.get("env", {})})
        try:
            with tempfile.TemporaryFile(mode="w+b") as errors, anyio.fail_after(20):
                async with stdio_client(parameters, errlog=errors) as streams:
                    async with ClientSession(*streams) as session:
                        await session.initialize()
                        catalog, cursor = [], None
                        while True:
                            result = await session.list_tools(cursor=cursor)
                            catalog.extend(tool.model_dump(mode="json", by_alias=True) for tool in result.tools)
                            cursor = result.nextCursor
                            if not cursor:
                                break
            if allowed_tools is not None:
                catalog = [tool for tool in catalog if f"mcp__{name}__{tool['name']}" in allowed_tools]
            await asyncio.to_thread(remember_tools, trace_id, name, catalog)
        except Exception as exc:
            log.warning("Tool catalog capture unavailable for %s: %s", name, type(exc).__name__)


def allowed_tools(selection: RuntimeSelection, *, dry_run: bool = False) -> list[str]:
    """Pre-approve file inspection and the selected runtime's application tools.

    The SDK's skills option owns skill discovery and invocation permission.
    """
    from applypilot.agent_skills import APPLICATION_EXECUTION_TOOLS, APPLICATION_READ_TOOLS
    if selection.selected == "browseros":
        defaults = (*sorted(BROWSEROS_TOOLS), *_BROWSEROS_HELPER_TOOLS)
        names = tuple(dict.fromkeys(selection.advertised_tools or defaults))
        if dry_run:
            names = tuple(
                name for name in names
                if name in {"tabs", "snapshot", "read", "grep", "wait", "screenshot", "read_browser_output"}
            )
        tools = ["mcp__browseros_neo__" + name for name in names]
    else:
        tools = [
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_wait_for",
            "mcp__playwright__browser_tabs",
        ]
        if not dry_run:
            tools.extend([
                "mcp__playwright__browser_click",
                "mcp__playwright__browser_hover",
                "mcp__playwright__browser_type",
                "mcp__playwright__browser_press_key",
                "mcp__playwright__browser_fill_form",
                "mcp__playwright__browser_select_option",
                "mcp__playwright__browser_file_upload",
                "mcp__playwright__browser_take_screenshot",
            ])
    tools.extend(APPLICATION_READ_TOOLS if dry_run else APPLICATION_EXECUTION_TOOLS)
    if not dry_run:
        tools.extend([
            "mcp__applypilot_control__record_application_observation",
            "mcp__applypilot_control__save_answer",
            "mcp__applypilot_control__status",
            "mcp__applypilot_control__mark_submitted",
            "mcp__applypilot_control__submit_email_application",
        ])
        if selection.selected == "playwright":
            tools.extend([
                "mcp__applypilot_control__mark_side_effect_intent",
                "mcp__cloudflare__solve_browser_challenge",
            ])
    # Gmail is intentionally read-only for OTP/receipt verification.
    tools.extend([
        "mcp__gmail__search_emails",
        "mcp__gmail__read_email",
        "mcp__gmail__get_email_content",
    ])
    return tools

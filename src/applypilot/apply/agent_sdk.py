"""Claude Agent SDK execution with ApplyPilot ownership and outcome telemetry."""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field

import anyio
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ClaudeSDKError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from applypilot.apply.dashboard import get_state, update_state
from applypilot.operations.worker_journal import agent_tool, emit, job_id

log = logging.getLogger(__name__)
_sessions: dict[int, tuple[asyncio.AbstractEventLoop, anyio.CancelScope, threading.Event]] = {}
_sessions_lock = threading.Lock()


class MiniTask:
    """Stream an explicitly requested user task through an owned SDK session."""

    def __init__(self, *, options, prompt: str, deadline_seconds: int | None = None, trace_id: str = ""):
        self.output: queue.Queue[str | None] = queue.Queue()
        self.stopped = threading.Event()
        self.stop_requested = threading.Event()
        self.loop = None
        self.cancel_scope = None
        pending = {}
        outcome = {"status": "interrupted", "infrastructure": False}
        deadline_scope = None

        async def execute():
            nonlocal deadline_scope
            self.loop = asyncio.get_running_loop()
            with anyio.CancelScope() as cancel_scope:
                self.cancel_scope = cancel_scope
                if self.stop_requested.is_set():
                    return
                with anyio.fail_after(deadline_seconds) as deadline_scope:
                    if trace_id:
                        from applypilot.apply.runtime import capture_external_tool_catalogs
                        await capture_external_tool_catalogs(options.mcp_servers, trace_id, options.env,
                                                             allowed_tools=options.allowed_tools)
                    async with ClaudeSDKClient(options=options) as client:
                        await client.query(prompt)
                        async for message in client.receive_response():
                            if isinstance(message, AssistantMessage):
                                for block in message.content:
                                    if isinstance(block, TextBlock):
                                        self.output.put(block.text)
                                    elif isinstance(block, ToolUseBlock) and block.name.startswith("mcp__"):
                                        pending[block.id] = (block.name, block.input, time.time())
                            elif isinstance(message, UserMessage) and isinstance(message.content, list) and trace_id:
                                from applypilot.reasoning.learning_store import record_tool
                                for block in message.content:
                                    if not isinstance(block, ToolResultBlock):
                                        continue
                                    request = pending.pop(block.tool_use_id, None)
                                    if request is None:
                                        continue
                                    name, arguments, request_started = request
                                    _, server, tool = name.split("__", 2)
                                    record_tool(trace_id, server, tool, arguments,
                                                {"content": block.content, "isError": bool(block.is_error)},
                                                {"origin": "user_browser_task"}, bool(block.is_error),
                                                request_started=request_started)
                            elif isinstance(message, ResultMessage):
                                from applypilot.apply.agent_cli import provider_failure
                                outcome["status"] = "agent_error" if message.is_error else "completed"
                                outcome["infrastructure"] = bool(provider_failure(message))

        def run():
            try:
                asyncio.run(execute())
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - Keep private task payloads out of the error stream.
                owner_deadline = (isinstance(exc, TimeoutError) and deadline_scope is not None
                                  and deadline_scope.cancel_called)
                outcome.update(status=type(exc).__name__, infrastructure=not owner_deadline)
                log.warning("User browser task ended: %s", type(exc).__name__)
                self.output.put("Browser task ended: " + type(exc).__name__)
            finally:
                try:
                    if trace_id:
                        from applypilot.reasoning.learning_store import finish_episode
                        finish_episode(trace_id, **outcome)
                finally:
                    self.output.put(None)
                    self.stopped.set()

        threading.Thread(target=run, name="application-user-task", daemon=True).start()

    def stop(self) -> None:
        self.stop_requested.set()
        if self.loop is not None and self.cancel_scope is not None:
            with suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self.cancel_scope.cancel)
        if not self.stopped.wait(30):
            raise RuntimeError("Previous browser task is still stopping")

    def stream(self):
        while True:
            try:
                text = self.output.get(timeout=1)
            except queue.Empty:
                if self.stopped.is_set():
                    return
                continue
            if text is None:
                return
            yield text


def stop_sessions(worker_id: int | None = None) -> bool:
    """Cancel only SDK sessions owned by this process, then await their teardown."""
    with _sessions_lock:
        sessions = [value for key, value in _sessions.items() if worker_id is None or key == worker_id]
    for loop, cancel_scope, _ in sessions:
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(cancel_scope.cancel)
    deadline = time.monotonic() + 30
    for _, _, stopped in sessions:
        stopped.wait(max(0, deadline - time.monotonic()))
    return all(stopped.is_set() for _, _, stopped in sessions)


@dataclass
class ApplicationSessionResult:
    text: list[str] = field(default_factory=list)
    structured_output: dict | None = None
    stats: dict = field(default_factory=dict)
    tool_calls: list[dict] = field(default_factory=list)
    infrastructure_failure: str | None = None
    error_detail: str = ""
    admission_failure: dict | None = None
    stop_reason: str | None = None


def run_application_session(*, options, prompt: str, worker_id: int, job_url: str,
                            application_id: str, trace, log_file, check_owner,
                            deadline_seconds: int | None = None, progress_timeout_seconds: int | None = None,
                            idle_timeout_seconds: int | None = None) -> ApplicationSessionResult:
    """Run until an SDK outcome, turn budget, or ownership cancellation.

    Optional owner-configured deadlines cancel the same SDK scope and drain its
    teardown. Elapsed time never determines submission success.
    """
    for value in (deadline_seconds, progress_timeout_seconds, idle_timeout_seconds):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("Application liveness timeouts must be positive integers or None")
    result = ApplicationSessionResult()
    stopped = threading.Event()
    pending: dict[str, tuple[str, float, dict, float]] = {}
    session_ready = False
    assistant_error = ""
    assistant_error_text = ""
    last_model_activity = last_model_wait = time.monotonic()

    def consume(message) -> None:
        nonlocal session_ready, assistant_error, assistant_error_text, last_model_activity, last_model_wait
        if isinstance(message, (AssistantMessage, StreamEvent, ResultMessage)):
            last_model_activity = time.monotonic()
            update_state(worker_id, model_activity_at=time.time())
        if isinstance(message, SystemMessage):
            if message.subtype != "init":
                return  # Retry and stream recovery belong to Claude Code.
            session_ready = True
            servers = [{"name": item.get("name"), "status": item.get("status")}
                       for item in message.data.get("mcp_servers", []) if isinstance(item, dict)]
            trace.emit("sdk_session_started", session_id=message.data.get("session_id"),
                       model=message.data.get("model"), mcp_servers=servers,
                       skills=message.data.get("skills", []), tools=message.data.get("tools", []),
                       permission_mode=message.data.get("permissionMode") or options.permission_mode)
            if any(item["name"] == "browseros_neo" and item["status"] in {"failed", "needs-auth", "disabled"}
                   for item in servers):
                result.infrastructure_failure = "browser_bridge_unavailable"
            return
        if isinstance(message, StreamEvent):
            if message.event.get("type") in {"message_start", "message_stop"}:
                native = message.event.get("message") or {}
                trace.emit("model_stream", phase=message.event["type"], model=native.get("model"),
                           message_id=native.get("id"))
            return
        if isinstance(message, AssistantMessage):
            assistant_error = message.error or ""
            assistant_error_text = "\n".join(block.text for block in message.content
                                             if isinstance(block, TextBlock)) if assistant_error else ""
            emit("agent.model_response", worker_id=worker_id, stage="applications", job_id=job_id(job_url),
                 snapshot={"application_id": application_id, "reported_model": message.model})
            for block in message.content:
                if isinstance(block, TextBlock):
                    result.text.append(block.text)
                    log_file.write(block.text + "\n")
                    trace.emit("assistant_text", text=block.text)
                elif isinstance(block, ToolUseBlock):
                    name = block.name.replace("mcp__playwright__", "").replace("mcp__gmail__", "gmail:")
                    pending[block.id] = (block.name, time.monotonic(), block.input, time.time())
                    update_state(worker_id, model_wait_since=0.0)
                    agent_tool("agent.tool_requested", worker_id=worker_id, job_url=job_url,
                               tool_name=block.name, tool_call_id=block.id)
                    trace.emit("tool_requested", tool=block.name, tool_call_id=block.id,
                               arguments="[Gmail arguments omitted]" if "gmail" in block.name.lower()
                               else block.input)
                    inputs = block.input
                    if "url" in inputs:
                        description = f"{name} {str(inputs['url'])[:60]}"
                    elif "ref" in inputs:
                        description = f"{name} {inputs.get('element', inputs.get('text', ''))}"[:50]
                    elif "fields" in inputs:
                        description = f"{name} ({len(inputs['fields'])} fields)"
                    else:
                        description = name
                    log_file.write(f"  >> {description}\n")
                    result.tool_calls.append({"tool": name, "summary": description})
                    state = get_state(worker_id)
                    update_state(worker_id, actions=(state.actions if state else 0) + 1,
                                 last_action=description[:35])
            return
        if isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if not isinstance(block, ToolResultBlock):
                    continue
                name, started, arguments, request_started = pending.pop(block.tool_use_id, ("", None, {}, 0))
                if not pending:
                    last_model_wait = time.monotonic()
                    update_state(worker_id, model_wait_since=time.time())
                if not block.is_error and name in {
                    "mcp__playwright__browser_navigate", "mcp__playwright__browser_click",
                    "mcp__playwright__browser_type", "mcp__playwright__browser_press_key",
                    "mcp__playwright__browser_fill_form", "mcp__playwright__browser_select_option",
                    "mcp__playwright__browser_file_upload",
                }:
                    update_state(worker_id, form_progress_at=time.time())
                agent_tool("agent.tool_returned", worker_id=worker_id, job_url=job_url,
                           tool_name=name, tool_call_id=block.tool_use_id, reported_error=bool(block.is_error))
                trace.emit("tool_returned", tool=name, tool_call_id=block.tool_use_id,
                           is_error=bool(block.is_error),
                           elapsed_ms=round((time.monotonic() - started) * 1000) if started else None,
                           observation="[Gmail payload omitted]" if "gmail" in name.lower() else block.content)
                if name.startswith("mcp__"):
                    _, server, tool = name.split("__", 2)
                    if server not in {"browseros", "browseros_neo"}:
                        from applypilot.reasoning.learning_store import record_tool
                        record_tool(trace.trace_id, server, tool, arguments,
                                    {"content": block.content, "isError": bool(block.is_error)}, {},
                                    bool(block.is_error), request_started=request_started)
                if block.is_error or "gmail" in name:
                    preview = ("[Gmail payload redacted]" if "gmail" in name else
                               str(block.content).replace("\n", " ")[:500])
                    log_file.write(f"  << {name}{' [ERROR]' if block.is_error else ''}: {preview}\n")
            return
        if isinstance(message, ResultMessage):
            from applypilot.apply.agent_cli import provider_failure
            usage = message.usage or {}
            result.stats = {
                "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
                "cache_read": usage.get("cache_read_input_tokens", 0),
                "cache_create": usage.get("cache_creation_input_tokens", 0),
                "cost_usd": message.total_cost_usd or 0, "turns": message.num_turns,
            }
            if isinstance(message.structured_output, dict):
                result.structured_output = message.structured_output
            if message.result:
                result.text.append(message.result)
            evidence = {"session_id": message.session_id, "subtype": message.subtype,
                        "is_error": message.is_error, "terminal_reason": message.terminal_reason,
                        "api_error_status": message.api_error_status, "usage": usage,
                        "reported_models": list(message.model_usage or {})}
            trace.emit("sdk_result", **evidence)
            log_file.write("  SDK result: " + json.dumps(evidence) + "\n")
            emit("agent.sdk_result", worker_id=worker_id, stage="applications",
                 job_id=job_id(job_url), snapshot=evidence)
            update_state(worker_id, model_wait_since=0.0)
            if message.is_error:
                from applypilot.operations.runtime import sanitize
                result.error_detail = sanitize(assistant_error_text or message.result or message.terminal_reason, 4000)
                result.infrastructure_failure = result.infrastructure_failure or "agent_cli_terminal_error"
                result.admission_failure = provider_failure(message, error_category=assistant_error,
                                                            error_text=assistant_error_text)

    async def execute() -> None:
        from applypilot.reasoning.telemetry import current_trace
        trace_token = current_trace.set(trace.trace_id)

        async def watch_owner() -> None:
            ready_at = last_progress = None
            observed_progress = 0.0
            while True:
                # Refresh bridge progress and honor persisted terminal outcomes
                # before considering a timeout. A heartbeat is not form progress.
                reason = await asyncio.to_thread(check_owner, session_ready)
                now = time.monotonic()
                if not reason and session_ready:
                    if ready_at is None:
                        ready_at = last_progress = now
                    state = get_state(worker_id)
                    progress = state.form_progress_at if state else 0.0
                    if progress > observed_progress:
                        observed_progress, last_progress = progress, now
                    idle = now - max(ready_at, last_model_activity, last_model_wait)
                    stalled = now - last_progress
                    if idle_timeout_seconds is not None and not pending and idle >= idle_timeout_seconds:
                        reason = "agent_idle_timeout"
                    elif progress_timeout_seconds is not None and stalled >= progress_timeout_seconds:
                        reason = "agent_progress_timeout"
                    if reason:
                        trace.emit("application_timeout", reason=reason,
                                   provider_idle_seconds=round(idle, 3),
                                   no_form_progress_seconds=round(stalled, 3), pending_tools=len(pending))
                if reason:
                    result.stop_reason = reason
                    owner_scope.cancel()
                    return
                await asyncio.sleep(5)

        monitor = None
        deadline_scope = None
        try:
            with anyio.CancelScope() as owner_scope:
                with _sessions_lock:
                    _sessions[worker_id] = (asyncio.get_running_loop(), owner_scope, stopped)
                monitor = asyncio.create_task(watch_owner())
                with anyio.fail_after(deadline_seconds) as deadline_scope:
                    from applypilot.apply.runtime import capture_external_tool_catalogs
                    await capture_external_tool_catalogs(options.mcp_servers, trace.trace_id, options.env,
                                                         allowed_tools=options.allowed_tools)
                    async with ClaudeSDKClient(options=options) as client:
                        if "browseros_neo" in options.mcp_servers:
                            last_status = None
                            while True:
                                try:
                                    status = await client.get_mcp_status()
                                except Exception as exc:
                                    from applypilot.operations.runtime import sanitize
                                    result.error_detail = sanitize(exc, 4000)
                                    trace.emit("sdk_browser_connection_failed", error_type=type(exc).__name__, error=result.error_detail)
                                    result.infrastructure_failure = "browser_bridge_unavailable"
                                    return
                                browser = next((server for server in status.get("mcpServers", [])
                                                if server.get("name") == "browseros_neo"), {})
                                connection = browser.get("status", "pending")
                                if connection != last_status:
                                    trace.emit("sdk_browser_connection", status=connection)
                                    last_status = connection
                                if connection == "connected":
                                    break
                                if connection != "pending":
                                    from applypilot.operations.runtime import sanitize
                                    result.error_detail = sanitize(browser.get("error") or browser.get("message") or connection, 4000)
                                    result.infrastructure_failure = "browser_bridge_unavailable"
                                    return
                                # MCP discovery can finish after SDK initialization.
                                # Ownership cancellation and the session deadline still apply.
                                await asyncio.sleep(0.5)
                        emit("application.agent_session_started", worker_id=worker_id, stage="applications",
                             job_id=job_id(job_url), application_id=application_id)
                        await client.query(prompt)
                        async for message in client.receive_response():
                            consume(message)
                            if result.infrastructure_failure == "browser_bridge_unavailable":
                                await client.interrupt()
                                break
            if owner_scope.cancel_called:
                result.stop_reason = result.stop_reason or "owner_stop"
        except TimeoutError:
            if deadline_scope is not None and deadline_scope.cancel_called:
                result.stop_reason = "agent_job_timeout"
            else:
                result.infrastructure_failure = result.infrastructure_failure or "agent_cli_exit_error"
                trace.emit("sdk_transport_failed", error_type="TimeoutError")
        except asyncio.CancelledError:
            result.stop_reason = result.stop_reason or "owner_stop"
        except ClaudeSDKError as exc:
            from applypilot.operations.runtime import sanitize
            result.error_detail = sanitize(exc, 4000)
            result.infrastructure_failure = result.infrastructure_failure or "agent_cli_exit_error"
            trace.emit("sdk_transport_failed", error_type=type(exc).__name__, error=result.error_detail)
        finally:
            if monitor is not None:
                monitor.cancel()
                with suppress(asyncio.CancelledError):
                    await monitor
            current_trace.reset(trace_token)

    try:
        asyncio.run(execute())
    finally:
        with _sessions_lock:
            _sessions.pop(worker_id, None)
            stopped.set()
    return result

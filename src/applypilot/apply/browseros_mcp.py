"""One owning BrowserOS connection per Claude invocation, with durable lifecycle evidence."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
from uuid import uuid4

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, Tool, TextContent
from jsonschema import Draft202012Validator, ValidationError

from applypilot.apply.browser_controls import inspect_uploads_code, upload_code

SESSION_META = "com.browseros.neo/session"
SESSION_FIELDS = ("session", "session_id", "sessionId")
HEARTBEAT_SECONDS = 60
HEARTBEAT_TIMEOUT_SECONDS = 10
MAX_BROWSER_OUTPUT_CHARS = 12_000
log = logging.getLogger(__name__)


def owned_page(arguments: dict, bound_pages: dict) -> int:
    from applypilot.apply.guard import GuardRejected

    page = arguments.get("page")
    if type(page) is not int or str(page) not in bound_pages:
        raise GuardRejected("Use a numeric page owned by this application session.")
    return page


def _may_change_page(name: str, arguments: dict) -> bool:
    """Whether a browser call can invalidate page-scoped handles."""
    return (name in {"run", "evaluate", "tabs", "navigate"}
            or name == "act" and arguments.get("kind") in {"click", "click_at", "press"})


def transport_failure(exc: BaseException) -> str | None:
    """Distinguish a dead owning connection from a failed page command."""
    pending, seen = [exc], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        message = str(error).lower()
        if getattr(error, "code", None) == "browser_session_not_connected" or "browser_session_not_connected" in message:
            return "browser_session_not_connected"
        if "session terminated" in message or "mcp_session_expired" in message:
            return "mcp_session_expired"
        if "browser_session_changed" in message or "browser_ownership_unknown" in message:
            return "browser_session_ownership_lost"
        if isinstance(error, (ConnectionError, EOFError)) or type(error).__name__ in {
            "ClosedResourceError", "BrokenResourceError", "EndOfStream", "ConnectError",
            "ReadError", "WriteError", "RemoteProtocolError", "ConnectTimeout",
        }:
            return "browser_transport_lost"
        pending.extend(getattr(error, "exceptions", ()))
        pending.extend(item for item in (error.__cause__, error.__context__) if item is not None)
    return None


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        from applypilot.atomic_io import replace_with_retry
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _browser_output_roots() -> tuple[Path, ...]:
    configured = os.getenv("BROWSEROS_DIR")
    roots = [Path(configured).expanduser() / "tool-output"] if configured else []
    roots.append(Path.home() / ".browseros" / "tool-output")
    local = os.getenv("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "BrowserClaw" / "User Data" / ".browseros" / "tool-output")
        for product in ("BrowserClaw", "BrowserOS"):
            application = Path(local) / product / "Application"
            if application.is_dir():
                roots.extend(application.glob("*/.browseros/tool-output"))
    # Keep the lexical roots until after lstat checks; resolve() here would hide
    # a junction/symlink at the configured output root itself.
    return tuple(dict.fromkeys(Path(os.path.abspath(root)) for root in roots))


def _safe_browser_output_path(raw: object, *, created_after: float) -> Path | None:
    if isinstance(raw, os.PathLike):
        candidate = Path(raw)
    elif isinstance(raw, str) and raw.strip():
        candidate = Path(raw.strip().strip("`\"'"))
    else:
        return None
    # BrowserOS reports extended Windows paths; compare them with the same
    # lexical spelling as the trusted roots before resolving junctions.
    spelling = str(candidate)
    if os.name == "nt" and spelling.startswith("\\\\?\\"):
        spelling = spelling[4:]
        if spelling.upper().startswith("UNC\\"):
            return None
        candidate = Path(spelling)
    if (not candidate.is_absolute() or ".." in candidate.parts
            or candidate.suffix.lower() not in {".md", ".txt", ".json"}):
        return None
    try:
        absolute = Path(os.path.abspath(candidate))
        root = next(root for root in _browser_output_roots() if absolute.is_relative_to(root))
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or getattr(root_info, "st_file_attributes", 0) & 0x400:
            return None
        relative = absolute.relative_to(root)
        current = root
        for part in relative.parts:
            current = current / part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            return None
        info = resolved.stat()
        if not stat.S_ISREG(info.st_mode):
            return None
        # A page cannot register an older output merely by echoing its path.
        if info.st_mtime < created_after - 2 or info.st_mtime > time.time() + 2:
            return None
        return resolved
    except (OSError, RuntimeError, StopIteration, ValueError):
        return None


def _read_output_snapshot(path: Path) -> str:
    with path.open("rb") as stream:
        raw = stream.read()
    return raw.decode("utf-8", errors="replace")


def _offload_paths(result: object, *, created_after: float) -> list[Path]:
    try:
        raw = result.model_dump(by_alias=True)
    except (AttributeError, TypeError, ValueError):
        return []
    candidates: list[object] = []
    # BrowserOS's own truncation marker is outside any page-content fence. Use
    # it only for a just-created file under the dedicated output directory.
    marker = re.compile(
        r"(?:inline limit|showing the first \d+ chars[^\n]*|"
        r"Content truncated at \d+ chars\.\s*Full content \(\d+ chars\))"
        r"[,;:]?\s*saved to:\s*(.+?)\s*$", re.I,
    )
    pending = [raw.get("structuredContent"), raw.get("content")]
    try:
        pending.append(decode(result))
    except Exception:
        pass  # File registration must not replace the original tool error.
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            candidates.extend(item.get(key) for key in ("writtenToFile", "written_to_file"))
            pending.extend(value for key, value in item.items() if key not in {"data", "blob"})
        elif isinstance(item, (list, tuple)):
            pending.extend(item)
        elif isinstance(item, str):
            tail = item.rsplit("[END_UNTRUSTED_PAGE_CONTENT", 1)[-1]
            for line in tail.splitlines():
                match = marker.search(line)
                if match:
                    candidates.append(match.group(1))
    paths: list[Path] = []
    for value in candidates:
        path = _safe_browser_output_path(value, created_after=created_after)
        if path is not None and path not in paths:
            paths.append(path)
    return paths


def bridge_health(path: Path) -> bool | None:
    state = read_state(path)
    if not state:
        return None
    from applypilot.processes import process_alive
    if state.get("terminal_reason") or state.get("state") in {"failed", "stopped", "closed"}:
        return False
    alive = process_alive(state.get("pid", 0))
    if not alive:
        return False
    # MCP initialization can legitimately take several health-check intervals.
    # Only a completed handshake is healthy; a live starting process is unknown.
    return True if state.get("state") == "ready" else None


def session_parameter(tool) -> str | None:
    schema = tool.inputSchema
    props = schema.get("properties", {})
    return next((key for key in SESSION_FIELDS if key in props), None)


def decode(result):
    # Shared Neo envelope decoder; it checks isError and fenced run values.
    from applypilot.unattended.browser import BrowserOS
    value = BrowserOS.decode(result)
    if isinstance(value, dict) and isinstance(value.get("ok"), bool) and any(k in value for k in ("value", "logs", "error")):
        if not value["ok"]:
            BrowserOS._tool_failure(value.get("error", ""))
        value = value.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(BrowserOS._data_text(value))
        except ValueError:
            pass
    return value


async def close_session(url: str, session_file: Path) -> bool:
    """A missing/dead connection handle is never evidence of successful cleanup."""
    state = read_state(session_file)
    if state.get("cleanup") == "complete":
        return True
    handle = state.get("session")
    if not handle or state.get("resumable") is False:
        return False
    async with streamable_http_client(url, terminate_on_close=False) as streams:
        async with ClientSession(streams[0], streams[1]) as upstream:
            await asyncio.wait_for(upstream.initialize(), 10)
            catalog = await upstream.list_tools()
            tools = {tool.name: tool for tool in catalog.tools}
            tool = tools.get("run")
            key = session_parameter(tool) if tool else None
            if not key:
                return False
            args = {key: handle, "code": 'const ps=await browser.pages.list();return {pages:ps};'}
            result = await asyncio.wait_for(upstream.call_tool("run", args), 15)
            if (result.meta or {}).get(SESSION_META, handle) != handle:
                return False
            value = decode(result)
            if not isinstance(value, dict) or not isinstance(value.get("pages"), list):
                return False
            for page in value["pages"]:
                if not isinstance(page, dict) or page.get("ownership") not in {"mine", "user", "other-agent"}:
                    return False
                if page["ownership"] == "mine":
                    if page.get("pageId") is None:
                        return False
                    args["code"] = 'await browser.pages.close(' + json.dumps(page["pageId"]) + ');return {closed:true};'
                    closed = await asyncio.wait_for(upstream.call_tool("run", args), 10)
                    if decode(closed) != {"closed": True} or (closed.meta or {}).get(SESSION_META, handle) != handle:
                        return False
            write_state(session_file, {**state, "cleanup": "complete", "state": "closed"})
            return True


async def serve(url: str, session_file: Path, *, probe: bool = False, read_only: bool = False) -> None:
    previous = read_state(session_file)
    if previous and not probe and previous.get("cleanup") != "complete" and not (previous.get("session") and previous.get("resumable") is not False):
        raise RuntimeError("browser_session_resume_unsupported: prior connection cleanup is unconfirmed")
    resuming = bool(previous.get("session") and previous.get("resumable") is not False and previous.get("cleanup") != "complete")
    if resuming and not read_only and previous.get("bound_pages") and previous.get("submission_protocol") != "owned-submit-v2":
        raise RuntimeError("browser_session_resume_unsupported: legacy application activity requires verification")
    handle = previous.get("session") if resuming else None
    retained = {key: previous[key] for key in ("submission_prepared", "submission_attempted_at", "email_handoff", "application_page", "page_seen_at")
                if resuming and key in previous}
    if previous.get("application_blocker"):
        retained["application_blocker"] = previous["application_blocker"]
    if retained.get("application_blocker") and not read_only and not probe:
        retained["terminal_reason"] = retained["application_blocker"]["reason"]
    state = {**retained, "session": handle, "resumable": False, "state": "starting", "cleanup": "unknown",
             "pid": os.getpid(), "invocation": uuid4().hex, "started_at": datetime.now(UTC).isoformat(),
             "tool_mode": "read_only" if read_only else "full"}
    write_state(session_file, state)
    state_lock = asyncio.Lock()
    # Captured files remain scoped to this reservation and bridge invocation.
    context_dir = session_file.with_suffix(".outputs") / state["invocation"]
    output_paths: dict[str, str] = {}
    context_store = None
    if os.getenv("APPLYPILOT_CONTEXT_ROOT") and not probe:
        from applypilot.reasoning.source_files import SourceFiles
        context_store = SourceFiles(Path(os.environ["APPLYPILOT_CONTEXT_ROOT"]))
    path_outputs: dict[Path, str] = {}
    upload_targets: dict[str, dict[str, object]] = {}
    application_lock = asyncio.Lock()
    catalog_capture = None
    learning_captures = set()

    def capture_learning(function, *args, **kwargs):
        async def capture():
            try:
                await asyncio.to_thread(function, *args, **kwargs)
            except Exception as exc:
                wrapper_failure("learning_capture_failed", exc)

        task = asyncio.create_task(capture())
        learning_captures.add(task)
        task.add_done_callback(learning_captures.discard)
        return task

    def wrapper_failure(event: str, exc: BaseException) -> None:
        error_code = getattr(exc, "code", None)
        safe_detail = str(exc)[:240] if type(exc).__name__ == "BrowserUnavailable" else None
        log.warning(
            "browseros_wrapper_failure attempt_id=%s invocation=%s transport_session_id=%s event=%s error_type=%s error_code=%s detail=%s",
            session_file.stem, state["invocation"], state.get("transport_session_id"), event,
            type(exc).__name__, error_code, safe_detail,
        )

    async def persist_state(changes: dict[str, object], *, event: str, critical: bool) -> bool:
        async with state_lock:
            state.update(changes)
            snapshot = dict(state)
            write_task = asyncio.create_task(asyncio.to_thread(write_state, session_file, snapshot))
            try:
                await asyncio.shield(write_task)
                return True
            except asyncio.CancelledError:
                # A thread doing Windows replace retries cannot be cancelled.
                # Keep the serialization lock until that write has really ended.
                with suppress(Exception):
                    await write_task
                raise
            except Exception as exc:
                wrapper_failure(event, exc)
                if critical:
                    raise
                return False

    async def expose_output_refs(result: object, tool_name: str, started_at: float) -> object:
        refs = []
        # Reserved metadata is set only by this adapter, never by upstream page data.
        metadata = dict(getattr(result, "meta", None) or {})
        metadata.pop("applypilot_observation_sources", None)
        metadata.pop("applypilot_action_receipt", None)
        metadata.pop("applypilot_failure_processed", None)
        result.meta = metadata
        replacements: list[tuple[str, str]] = []
        for path in _offload_paths(result, created_after=started_at):
            output_id = path_outputs.get(path)
            if output_id is None:
                before = path.stat()
                content_text = await asyncio.to_thread(_read_output_snapshot, path)
                after = path.stat()
                fingerprint_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                fingerprint_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                if (fingerprint_before != fingerprint_after
                        or _safe_browser_output_path(path, created_after=started_at) != path):
                    continue
                output_id = uuid4().hex
                path_outputs[path] = output_id
                output_paths[output_id] = content_text
                context_dir.mkdir(parents=True, exist_ok=True)
                await asyncio.to_thread((context_dir / (output_id + ".txt")).write_text,
                                        content_text, encoding="utf-8")
            reference = {"output_id": output_id, "tool": tool_name, "max_chars_per_read": MAX_BROWSER_OUTPUT_CHARS,
                         "form_context_id": output_id}
            if context_store is not None:
                record = await asyncio.to_thread(context_store.put,
                    "browser output " + state["invocation"] + "/" + output_id, output_paths[output_id])
                reference["context_ref"] = record["ref"]
                reference["context_path"] = record["path"]
            refs.append(reference)
            variants = [str(path), str(path).replace("\\", "/")]
            if os.name == "nt":
                variants.insert(0, "\\\\?\\" + str(path))
            for spelling in variants:
                replacements.append((spelling, output_id))
                replacements.append((json.dumps(spelling)[1:-1], output_id))
        if refs:
            notice = TextContent(type="text", text=json.dumps({"applypilot_browser_outputs": refs}, separators=(",", ":")))
            content = list(getattr(result, "content", ()) or ())
            for block in content:
                if isinstance(block, TextContent):
                    for path, output_id in replacements:
                        block.text = block.text.replace(path, "output_id:" + output_id)
            content.append(notice)
            result.content = content
            structured = dict(getattr(result, "structuredContent", None) or {})
            def replace_paths(value):
                if isinstance(value, str):
                    for spelling, output_id in replacements:
                        value = value.replace(spelling, "output_id:" + output_id)
                    return value
                if isinstance(value, dict):
                    return {key: replace_paths(item) for key, item in value.items()}
                if isinstance(value, list):
                    return [replace_paths(item) for item in value]
                return value
            structured = replace_paths(structured)
            structured["applypilotBrowserOutputs"] = refs
            if len(refs) == 1:
                structured["output_id"] = refs[0]["output_id"]
            result.structuredContent = structured
            result.meta = {**metadata, "applypilot_observation_sources": [
                {"ref": ref["context_ref"]} for ref in refs if "context_ref" in ref
            ]}
        return result

    def form_progress(name: str, arguments: dict[str, object]) -> bool:
        if name == "upload":
            return True
        if name == "act":
            return str(arguments.get("kind") or "") in {
                "click", "click_at", "type", "type_at", "fill", "press",
                "check", "uncheck", "select", "drag", "drag_at",
                "dialog_accept", "dialog_dismiss",
            }
        if name == "run":
            code = str(arguments.get("code") or "")
            return any(marker in code for marker in ("browser.input(", "browser.upload("))
        return False

    try:
        async with streamable_http_client(url, terminate_on_close=False) as streams:
            async with ClientSession(streams[0], streams[1], read_timeout_seconds=timedelta(seconds=120)) as upstream:
                initialized = await asyncio.wait_for(upstream.initialize(), 15)
                if len(streams) > 2 and callable(streams[2]):
                    state["transport_session_id"] = streams[2]()
                tools = {}
                cursor = None
                while True:
                    listing = await asyncio.wait_for(upstream.list_tools(cursor=cursor), 15)
                    tools.update({tool.name: tool for tool in listing.tools})
                    cursor = listing.nextCursor
                    if not cursor:
                        break
                if not {"run", "name_session"} <= tools.keys():
                    raise RuntimeError("browser_tool_contract: run/name_session required")
                naming = {"name": "applypilot probe" if probe else "applypilot apply", "category": "form-filling",
                          "summary": "Validate application browser connection." if probe else "Complete one approved application."}
                naming_key = session_parameter(tools["name_session"])
                if handle:
                    if not naming_key or not session_parameter(tools["run"]):
                        raise RuntimeError("browser_session_resume_unsupported")
                    naming[naming_key] = handle
                named = await asyncio.wait_for(upstream.call_tool("name_session", naming), 15)
                value = decode(named)
                returned = (named.meta or {}).get(SESSION_META)
                if not returned and isinstance(value, dict):
                    returned = next((value.get(k) for k in SESSION_FIELDS if value.get(k)), None)
                if returned is not None and (not isinstance(returned, str) or not returned):
                    raise RuntimeError("browser_session_invalid")
                if handle and returned != handle:
                    raise RuntimeError("browser_session_resume_failed")
                handle = returned
                for tool in tools.values():
                    key = session_parameter(tool)
                    if key in tool.inputSchema.get("required", []) and not handle:
                        raise RuntimeError("browser_session_required_by_schema")
                state.update(session=handle, resumable=bool(handle and naming_key and session_parameter(tools["run"])))
                browser_requests = 0

                async def forward(name, arguments, *, record_progress=False, expose_outputs=False,
                                  persist_transport=True, source="adapter"):
                    nonlocal browser_requests
                    started_at = time.time()
                    args = {k: v for k, v in arguments.items() if k not in SESSION_FIELDS}
                    key = session_parameter(tools[name])
                    if key and handle:
                        args[key] = handle
                    # Adapter observations (including CDP reads and heartbeat) do
                    # not invalidate backend node handles. Native navigation does.
                    if (name == "navigate" or name == "tabs" and args.get("action") in {"close", "reload"}
                            or name in {"run", "evaluate"} and source == "agent"):
                        page = args.get("page")
                        for target_id, target in list(upload_targets.items()):
                            if page is None or target["page"] == page:
                                upload_targets.pop(target_id, None)
                    log.info("browser_tool_started source=%s tool=%s", source, name)
                    browser_requests += 1
                    try:
                        result = await asyncio.wait_for(upstream.call_tool(name, args), 125)
                    finally:
                        browser_requests -= 1
                    if (result.meta or {}).get(SESSION_META, handle) != handle:
                        raise RuntimeError("browser_session_changed")
                    try:
                        value = decode(result)
                    except Exception as exc:
                        if source != "agent" or transport_failure(exc):
                            raise
                        # Native error text and schemas are actionable. Do not
                        # replace them with a generic adapter exception.
                        value = None
                        result.isError = True
                    log.info("browser_tool source=%s tool=%s error=%s elapsed_ms=%d",
                             source, name, bool(result.isError), (time.time() - started_at) * 1000)
                    if isinstance(value, dict) and value.get("urlChanged") is True:
                        for target_id, target in list(upload_targets.items()):
                            if args.get("page") is None or target["page"] == args["page"]:
                                upload_targets.pop(target_id, None)
                    if expose_outputs:
                        try:
                            await expose_output_refs(result, name, started_at)
                            if not result.isError:
                                value = decode(result)
                        except Exception as exc:
                            # Output registration is convenience metadata. It can
                            # never rewrite a completed browser operation as failure.
                            wrapper_failure("output_reference_publish", exc)
                    if persist_transport:
                        now = datetime.now(UTC).isoformat()
                        changes = {"last_transport_at": now}
                        made_progress = (
                            isinstance(value, dict) and bool(value.get("changed"))
                            and (name != "act" or arguments.get("kind") in {"fill", "type", "select", "check", "uncheck"}
                                 or value.get("urlChanged") is True)
                        )
                        if record_progress and made_progress:
                            changes["last_form_progress_at"] = now
                        await persist_state(changes, event="transport_progress", critical=False)
                    return result, value

                async def run(code, *, persist_transport=True, private=False):
                    source = "adapter" if persist_transport else "heartbeat"
                    return (await forward("run", {"code": code}, persist_transport=persist_transport,
                                          expose_outputs=persist_transport and not private, source=source))[1]

                async def failed_transport(exc, reason=None):
                    from applypilot.operations.runtime import sanitize
                    await persist_state({
                        "state": "failed",
                        "terminal_reason": reason or transport_failure(exc) or "browser_transport_lost",
                        "terminal_at": datetime.now(UTC).isoformat(),
                        "error": sanitize(exc, 500),
                    }, event="transport_terminal", critical=False)

                async def heartbeat():
                    while True:
                        await asyncio.sleep(HEARTBEAT_SECONDS)
                        if state.get("terminal_reason"):
                            return
                        try:
                            # This is transport activity, never agent output or job progress.
                            if browser_requests:
                                # Keep the transport alive without queuing a
                                # browser command behind an active browser RPC.
                                await asyncio.wait_for(upstream.send_ping(), HEARTBEAT_TIMEOUT_SECONDS)
                            else:
                                # Local model/DB waits also hold application_lock,
                                # but still need the browser session heartbeat.
                                await asyncio.wait_for(run('return {heartbeat:true};', persist_transport=False), HEARTBEAT_TIMEOUT_SECONDS)
                            now = datetime.now(UTC).isoformat()
                            await persist_state({"heartbeat_at": now, "last_transport_at": now},
                                                event="heartbeat", critical=False)
                        except Exception as exc:
                            wrapper_failure("heartbeat_failed", exc)
                            await failed_transport(exc, transport_failure(exc) or "browser_heartbeat_timeout")
                            return

                # Exercises the advertised execution surface, without opening a page.
                check = await run('return {applypilot_ready:"owned-page-v1"};')
                if check != {"applypilot_ready": "owned-page-v1"}:
                    raise RuntimeError("browser_run_result_missing")
                bound_pages = dict(previous.get("bound_pages") or {}) if resuming else {}
                page_urls: dict[int, str] = {}
                previous_completion = dict(previous.get("previous_completion") or {}) if resuming else {}
                completion_baseline_at = dict(previous.get("completion_baseline_at") or {}) if resuming else {}

                async def defer_blocker(reason: str) -> dict:
                    from applypilot.apply.guard import defer_application, terminal_result
                    attempt = await asyncio.to_thread(
                        defer_application, session_file.stem, reason)
                    if attempt["status"] == "submitted":
                        return {"status": "submitted", "committed": True, "guard_status": "submitted"}
                    result = terminal_result(attempt)
                    result["retry_trigger"] = "condition_change"
                    # Save the guarded disposition before signaling the watchdog.
                    await persist_state({"application_blocker": result, "terminal_reason": result["reason"],
                                         "application_terminal": result},
                                        event="application_blocked", critical=True)
                    return result

                def observation_code(page: int) -> str:
                    from applypilot.apply.submission_evidence import COMPLETION
                    return ("const s=await browser.observe(" + json.dumps(page) + ").snapshot();"
                            "const content=await browser.read(" + json.dumps(page) + ");"
                            "const complete=new RegExp(" + json.dumps(COMPLETION.pattern) + ",'i').test(s.text+'\\n'+content);"
                            "return {url:s.url,text:content.slice(0,12000),snapshot:s.text.slice(0,12000),completion_visible:complete};")

                async def reconcile_editing_state(attempt: dict) -> None:
                    """Finish bridge cleanup if a confirmed rejection was already committed."""
                    if (attempt["status"] != "prepared" or attempt.get("side_effect_intent_at")
                            or not state.get("submission_attempted_at")):
                        return
                    history = json.loads(attempt.get("browser_submit_history_json") or "[]")
                    latest = history[-1] if history else {}
                    receipt = latest.get("prevention") or latest.get("rejection") or latest.get("transition") or {}
                    if (latest.get("disposition") not in {"pre_submit", "rejected", "intermediate"}
                            or not latest.get("intent_at")
                            or receipt.get("intent_at") != latest["intent_at"]
                            or receipt.get("target") != attempt["target"]):
                        return
                    await persist_state({"submission_attempted_at": None, "submission_prepared": None},
                                        event="submission_editing_reconciled", critical=True)

                async def resume_rejected_submission(page: int) -> dict | None:
                    """Allow corrections after a visible rejection, preserving unknown outcomes."""
                    from applypilot.apply.browser_controls import submission_rejection_code
                    from applypilot.apply.guard import attempt_status, resume_browser_rejected_submission
                    attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                    prepared = state.get("submission_prepared") or {}
                    if (attempt["status"] != "side_effect_intent" or prepared.get("page") != page
                            or not state.get("submission_attempted_at") or not prepared.get("ref")):
                        return None
                    if not any(item.get("pageId") == page for item in await owned_pages()):
                        return None
                    observation = await run(observation_code(page))
                    if not isinstance(observation, dict) or observation.get("completion_visible"):
                        return None
                    try:
                        validation = await run(submission_rejection_code(page, prepared["ref"], target=prepared.get("target")))
                    except Exception as exc:
                        if transport_failure(exc):
                            raise
                        return None
                    if not isinstance(validation, dict) or validation.get("rejected") is not True:
                        return None
                    validation["completion_visible"] = False
                    receipt = {"kind": "browser-validation-rejected-v1", "clicked": True,
                               "intent_at": attempt["side_effect_intent_at"], "invocation": state["invocation"],
                               "target": attempt["target"], "page_id": page,
                               "recorded_at": datetime.now(UTC).isoformat(), "validation": validation}
                    await persist_state({"submission_rejected": receipt}, event="submission_rejected", critical=True)
                    await asyncio.to_thread(resume_browser_rejected_submission, session_file.stem)
                    await persist_state({"submission_attempted_at": None, "submission_prepared": None},
                                        event="submission_editing_resumed", critical=True)
                    return {"status": "prepared", "committed": False, "submission_disposition": "validation_rejected",
                            "validation": validation,
                            "next_action": "Correct the reported validation errors, then call submit_application again."}

                async def resume_intermediate_submission(page: int) -> dict | None:
                    from applypilot.apply.browser_controls import application_forms_code, intermediate_form_transition
                    from applypilot.apply.guard import attempt_status, resume_browser_rejected_submission
                    attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                    prepared = state.get("submission_prepared") or {}
                    if (attempt["status"] != "side_effect_intent" or prepared.get("page") != page
                            or not state.get("submission_attempted_at")):
                        return None
                    if not any(item.get("pageId") == page for item in await owned_pages()):
                        return None
                    observation = await run(observation_code(page))
                    if not isinstance(observation, dict) or observation.get("completion_visible"):
                        return None
                    try:
                        forms = await run(application_forms_code(page, target=prepared.get("target") or {}))
                    except Exception as exc:
                        if transport_failure(exc):
                            raise
                        return None
                    transition = intermediate_form_transition(prepared, forms) if isinstance(forms, dict) else None
                    if transition is None:
                        return None
                    receipt = {"kind": "browser-intermediate-step-v1", "clicked": True,
                               "intent_at": attempt["side_effect_intent_at"], "invocation": state["invocation"],
                               "target": attempt["target"], "page_id": page,
                               "recorded_at": datetime.now(UTC).isoformat(), "validation": transition}
                    await persist_state({"submission_transition": receipt}, event="submission_step", critical=True)
                    await asyncio.to_thread(resume_browser_rejected_submission, session_file.stem, intermediate=True)
                    await persist_state({"submission_attempted_at": None, "submission_prepared": None},
                                        event="submission_step_resumed", critical=True)
                    return {"status": "prepared", "committed": False, "submission_disposition": "intermediate_step",
                            "step": transition["step_evidence"],
                            "next_action": "Complete the newly observed application step, then use submit_application for its next or submit control."}

                async def email_handoff(page: int, control: dict | None = None) -> dict:
                    from applypilot.apply.browser_controls import inspect_email_links_code, is_application_email, email_link_matches_posting
                    from applypilot.apply.guard import attempt_status, GuardRejected
                    if state.get("email_handoff"):
                        return state["email_handoff"]
                    if control is None:
                        observed = await run(inspect_email_links_code(page))
                        if not isinstance(observed, dict) or not isinstance(observed.get("links"), list):
                            raise GuardRejected("Cannot inspect the application route; take a fresh snapshot.")
                        candidates = {item["href"]: item for item in observed["links"]
                                      if isinstance(item, dict) and is_application_email(item)}
                        if not candidates:
                            return {"route": "external", "next_action": "Continue with this posting's browser application."}
                        if len(candidates) != 1:
                            return {"route": "ambiguous", "next_action": "Inspect the posting's application instructions; multiple email routes are visible."}
                        control = next(iter(candidates.values())) | {key: observed.get(key) for key in ("page_url", "page_title", "page_heading")}
                    if not is_application_email(control):
                        return {"route": "blocked", "reason": "This email link is not an invitation to apply for this job.",
                                "next_action": "Use the posting's application instructions. Do not click this email link."}
                    observation = await run(observation_code(page))
                    if not isinstance(observation, dict) or observation.get("completion_visible"):
                        raise GuardRejected("Application completion must be verified before changing its delivery route.")
                    attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                    posting = json.loads(attempt.get("snapshot_json") or "{}")
                    title = " ".join(re.findall(r"\w+", str(posting.get("title") or "").casefold()))
                    visible = " ".join(re.findall(r"\w+", str(observation.get("text") or "").casefold()))
                    if not title or title not in visible or not email_link_matches_posting(control, posting.get("title", "")):
                        return {"route": "blocked", "reason": "Posting identity could not be confirmed for this email invitation.",
                                "next_action": "Inspect the current posting identity before sending an application."}
                    evidence = {"source": "browseros-application-link", "attempt_id": session_file.stem,
                                "posting_url": attempt["job_url"], "page_url": observation.get("url"),
                                "observed_href": control["href"], "label": control.get("label", ""),
                                "context": control.get("context", "")}
                    def register():
                        from applypilot.apply.email_application import register_route
                        from applypilot.database import close_connection
                        try:
                            return register_route(session_file.stem, control["href"], evidence=evidence)
                        finally:
                            close_connection()
                    try:
                        result = await asyncio.to_thread(register)
                    except ValueError:
                        return {"route": "blocked", "reason": "unsupported_application_email_link",
                                "next_action": "Use another explicit browser application route, or report unsupported_application_email_link. Do not drop required recipients."}
                    await persist_state({"email_handoff": result}, event="email_route_handoff", critical=True)
                    return result

                async def save_baseline(page: int, text: object, observed_completion: bool | None = None) -> bool:
                    if not isinstance(text, str) or not text.strip():
                        return False
                    from applypilot.apply.submission_evidence import COMPLETION
                    candidate = dict(previous_completion)
                    candidate_times = dict(completion_baseline_at)
                    candidate[str(page)] = (observed_completion if type(observed_completion) is bool
                                            else bool(COMPLETION.search(text)))
                    candidate_times[str(page)] = datetime.now(UTC).isoformat()
                    await persist_state({"previous_completion": candidate,
                                         "completion_baseline_at": candidate_times},
                                        event="completion_baseline", critical=True)
                    previous_completion.clear()
                    previous_completion.update(candidate)
                    completion_baseline_at.clear()
                    completion_baseline_at.update(candidate_times)
                    return True

                async def initial_observation(page: int) -> dict[str, object] | None:
                    try:
                        value = await run(observation_code(page))
                    except Exception as exc:
                        if transport_failure(exc):
                            raise
                        wrapper_failure("initial_observation_unavailable", exc)
                        return None
                    if not isinstance(value, dict):
                        return None
                    text = value.get("text")
                    if await save_baseline(page, text, value.get("completion_visible")):
                        return {"url": value.get("url"), "text": text, "snapshot": value.get("snapshot")}
                    return None

                catalog = []
                for name, original in tools.items():
                    available = ({"tabs", "snapshot", "read", "grep", "wait", "screenshot"} if read_only
                                 else tools.keys())
                    if name not in available:
                        continue
                    if not probe and not read_only and name in {"run", "evaluate"}:
                        # Unrestricted JavaScript can activate Submit without the
                        # reservation guard. Adapter-owned scripts remain available.
                        continue
                    tool = original.model_copy(deep=True)
                    for key in SESSION_FIELDS:
                        tool.inputSchema.get("properties", {}).pop(key, None)
                    if "required" in tool.inputSchema:
                        tool.inputSchema["required"] = [k for k in tool.inputSchema["required"] if k not in SESSION_FIELDS]
                    tool.description = "ApplyPilot supplies the owning session handle automatically. " + (tool.description or "")
                    catalog.append(tool)
                helpers = []
                if not probe and not read_only:
                    helpers.append(Tool(name="record_provider_restriction", description="Record an actual LinkedIn restriction on this owned page and defer only this account through Temporal. Quote the displayed restriction; include retry_after_seconds only when the provider states a delay.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}, "evidence": {"type": "string", "minLength": 8, "maxLength": 4000}, "retry_after_seconds": {"type": "number", "exclusiveMinimum": 0}}, "required": ["page", "evidence"], "additionalProperties": False}))
                    helpers.append(Tool(name="solve_browser_challenge", description="Recover a challenge on this application's owned page using its browser route, then return the observed page.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"], "additionalProperties": False}))
                    helpers.append(Tool(name="submit_email_application", description="Send the email application for route_id. Handles Gmail fallback and returns committed=true when sent.", inputSchema={"type": "object", "properties": {"route_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"}}, "required": ["route_id"], "additionalProperties": False}))
                    helpers.append(Tool(name="inspect_application_route", description="Find email application instructions on this posting and return route_id for submit_email_application. Use when the posting asks applicants to email.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}, "ref": {"type": "string", "pattern": "^e[0-9]+$"}}, "required": ["page"], "additionalProperties": False}))
                    helpers.append(Tool(name="open_application_page", description="Open the assigned posting, or reuse its existing tab. Returns page_id and page content.", inputSchema={"type": "object", "properties": {}, "additionalProperties": False}))
                    helpers.append(Tool(name="capture_submission", description="Check the result after Submit without clicking again. Returns confirmed completion or validation errors to fix.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"], "additionalProperties": False}))
                    helpers.append(Tool(name="submit_application", description="Activate this application step's Next/Continue/Submit once and inspect its transition. committed=true means done; otherwise follow next_action.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}, "ref": {"type": "string", "pattern": "^e[0-9]+$"}}, "required": ["page", "ref"], "additionalProperties": False}))
                    helpers.append(Tool(name="inspect_application_uploads", description="Find upload fields, including hidden inputs. Returns target_id handles for upload_application_document.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}}, "required": ["page"], "additionalProperties": False}))
                    helpers.append(Tool(name="upload_application_document", description="Attach the supplied resume or cover_letter to a file-input ref or inspected target_id, and check the attachment.", inputSchema={"type": "object", "properties": {"page": {"type": "integer"}, "document": {"type": "string", "enum": ["resume", "cover_letter"]}, "ref": {"type": "string", "pattern": "^e[0-9]+$"}, "target_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}, "required": ["page", "document"], "oneOf": [{"required": ["ref"], "not": {"required": ["target_id"]}}, {"required": ["target_id"], "not": {"required": ["ref"]}}], "additionalProperties": False}))
                if not probe:
                    helpers.append(Tool(name="read_browser_output", description="Read more text using an output_id or raw_output_id returned for this application. Pass either as output_id; use next_offset to continue.", inputSchema={"type": "object", "properties": {"output_id": {"type": "string"}, "offset": {"type": "integer", "minimum": 0, "default": 0}, "max_chars": {"type": "integer", "minimum": 1, "maximum": MAX_BROWSER_OUTPUT_CHARS, "default": MAX_BROWSER_OUTPUT_CHARS}}, "required": ["output_id"], "additionalProperties": False}))
                helper_validators = {tool.name: Draft202012Validator(tool.inputSchema) for tool in helpers}
                catalog.extend(helpers)
                available_tools = {tool.name for tool in catalog}
                # Tools alone are not the complete MCP contract. Forward the
                # installed browser's own usage instructions instead of relying
                # solely on a separately versioned local skill.
                server = Server("ApplyPilot BrowserOS", instructions=initialized.instructions)
                log.info("browser_contract version=%s instructions_present=%s",
                         initialized.serverInfo.version, bool(initialized.instructions))
                await persist_state({"state": "ready", "tools": [t.name for t in catalog],
                                     "bound_pages": bound_pages, "previous_completion": previous_completion,
                                     "completion_baseline_at": completion_baseline_at,
                                     "submission_protocol": previous.get("submission_protocol") if read_only or probe else "owned-submit-v2"},
                                    event="bridge_ready", critical=True)

                def commit_capture(evidence):
                    from applypilot.apply.guard import mark_submitted
                    from applypilot.database import get_connection, close_connection
                    conn = get_connection()
                    try:
                        attempt = mark_submitted(session_file.stem, conn=conn, **evidence)
                        captured = json.loads(attempt.get("proof") or "{}")
                        return {"status": attempt["status"], "committed": attempt["status"] == "submitted",
                                "attempt_id": session_file.stem, "submitted_at": attempt.get("submitted_at"),
                                "guard_status": attempt["status"], "current_url": captured.get("url", ""),
                                "confirmation": captured.get("completion", ""), **evidence}
                    finally:
                        close_connection()

                async def owned_pages():
                    value = await run('return {pages:await browser.pages.list()};')
                    pages = value.get("pages") if isinstance(value, dict) else None
                    if not isinstance(pages, list) or any(not isinstance(p, dict) or p.get("ownership") not in {"mine", "user", "other-agent"} for p in pages):
                        raise RuntimeError("browser_ownership_unknown")
                    return [p for p in pages if p["ownership"] == "mine"]

                async def reconcile_pages() -> list[dict]:
                    """Bind actual task-owned pages, including native-tool popups."""
                    from applypilot.apply.guard import attempt_status
                    attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                    pages = await owned_pages()
                    live_ids = {item.get("pageId") for item in pages}
                    for target_id, target in list(upload_targets.items()):
                        if target["page"] not in live_ids:
                            upload_targets.pop(target_id, None)
                    seen = dict(state.get("page_seen_at") or {})
                    candidate = dict(bound_pages)
                    for item in pages:
                        page = item.get("pageId")
                        if type(page) is int:
                            current_url = item.get("url")
                            if isinstance(current_url, str):
                                if page in page_urls and page_urls[page] != current_url:
                                    for target_id, target in list(upload_targets.items()):
                                        if target["page"] == page:
                                            upload_targets.pop(target_id, None)
                                page_urls[page] = current_url
                            candidate[str(page)] = attempt["target"]
                            seen.setdefault(str(page), datetime.now(UTC).isoformat())
                    # Keep historical bindings for confirmation in replacement tabs.
                    if candidate != bound_pages or seen != state.get("page_seen_at"):
                        await persist_state({"bound_pages": candidate, "page_seen_at": seen},
                                            event="owned_pages_reconciled", critical=True)
                        bound_pages.clear()
                        bound_pages.update(candidate)
                    return pages

                async def native_call(name, arguments):
                    return (await forward(name, arguments, expose_outputs=True))[0]

                async def verify_documents(page: int, inputs: list[dict], attempt: dict) -> None:
                    """Reconcile native uploads against the reserved artifact bytes."""
                    from applypilot.apply.guard import begin_browser_upload, finish_browser_upload, GuardRejected
                    artifacts = json.loads(attempt.get("artifact_hashes_json") or "{}")
                    receipts = json.loads(attempt.get("browser_uploads_json") or "{}").get("documents", {})
                    for item in inputs:
                        if not item.get("has_file"):
                            continue
                        observed = await run(upload_code(page, ref=None, backend_node_id=item["backend_node_id"], attach=False))
                        if not isinstance(observed, dict) or observed.get("verified") is not True:
                            raise GuardRejected("The selected application file could not be verified.")
                        document = next((kind for kind, artifact in artifacts.items()
                                         if artifact and artifact["files"][artifact["path"]] == observed.get("sha256")), None)
                        if document not in {"resume", "cover_letter"}:
                            raise GuardRejected("The selected file does not match a supplied application document.")
                        receipt = receipts.get(document, {})
                        if (receipt.get("status") == "verified"
                                and receipt.get("input_key") == f"{page}:{item['backend_node_id']}"
                                and all(receipt.get(key) == observed.get(key) for key in ("sha256", "name", "size"))):
                            continue
                        await asyncio.to_thread(begin_browser_upload, session_file.stem, document, page)
                        await asyncio.to_thread(finish_browser_upload, session_file.stem, document, page, observed)

                async def terminal_outcome() -> dict | None:
                    from applypilot.apply.guard import attempt_status, terminal_result
                    result = terminal_result(await asyncio.to_thread(attempt_status, session_file.stem))
                    if result and state.get("application_terminal") != result:
                        changes = {"application_terminal": result}
                        if result["status"] == "blocked":
                            changes.update(application_blocker=result, terminal_reason=result["reason"])
                        await persist_state(changes, event="application_terminal", critical=True)
                    return result

                async def follow_completion_page(page: int, attempt: dict) -> None:
                    """Carry a pre-submit baseline to a newly opened confirmation tab."""
                    if str(page) in previous_completion or attempt["status"] != "side_effect_intent":
                        return
                    source = (state.get("submission_prepared") or {}).get("page")
                    if (previous_completion.get(str(source)) is not False
                            or bound_pages.get(str(page)) != attempt["target"]):
                        return
                    try:
                        seen_at = datetime.fromisoformat(state["page_seen_at"][str(page)])
                        intent_at = datetime.fromisoformat(attempt["side_effect_intent_at"])
                    except (KeyError, TypeError, ValueError):
                        return
                    if seen_at < intent_at:
                        return  # An older tab needs its own pre-submit baseline.
                    previous_completion[str(page)] = False
                    completion_baseline_at[str(page)] = completion_baseline_at[str(source)]
                    await persist_state({"previous_completion": dict(previous_completion),
                                         "completion_baseline_at": dict(completion_baseline_at),
                                         "completion_page_parent": {"page": page, "source": source}},
                                        event="completion_tab_followed", critical=True)

                @server.list_tools()
                async def list_tools():
                    nonlocal catalog_capture
                    from applypilot.reasoning.learning_store import remember_tools
                    if catalog_capture is None:
                        # Corpus persistence must not delay the MCP readiness handshake.
                        catalog_capture = capture_learning(
                            remember_tools, os.getenv("APPLYPILOT_AGENT_TRACE_ID", ""), "browseros_neo",
                            [tool.model_dump(mode="json", by_alias=True) for tool in catalog])
                    return catalog

                async def dispatch_tool(name: str, arguments: dict):
                    try:
                        if not probe and not read_only:
                            terminal = await terminal_outcome()
                            if terminal:
                                return [TextContent(type="text", text=json.dumps(terminal))]
                            if name in {"inspect_application_route", "inspect_application_uploads", "upload_application_document",
                                        "submit_application", "capture_submission", "solve_browser_challenge", "record_provider_restriction"}:
                                await reconcile_pages()
                        if name == "record_provider_restriction" and not probe and not read_only:
                            from applypilot.apply.provider_observations import capture_restriction, notify_account
                            from applypilot.apply.guard import defer_application, terminal_result
                            page = owned_page(arguments, bound_pages)
                            observed = await run(observation_code(page), private=True)
                            fact = await asyncio.to_thread(capture_restriction, session_file.stem, page,
                                state['invocation'], observed, arguments['evidence'], arguments.get('retry_after_seconds'))
                            attempt = await asyncio.to_thread(defer_application, session_file.stem,
                                arguments['evidence'], detail=arguments['evidence'])
                            await notify_account(fact)
                            result = terminal_result(attempt) or attempt
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name == "solve_browser_challenge" and not probe and not read_only:
                            from applypilot.apply.cloudflare_tool import solve_owned_page
                            from applypilot.apply.guard import attempt_status, GuardRejected
                            page = owned_page(arguments, bound_pages)

                            async def check_recovery():
                                attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                                if attempt["status"] != "prepared":
                                    raise GuardRejected("Reconcile the existing submission before navigating its page")

                            await check_recovery()
                            result = await solve_owned_page(page, lambda code: run(code, private=True),
                                proxy=os.environ.get("APPLYPILOT_BROWSER_PROXY_URL"), check=check_recovery)
                            for target_id, target in list(upload_targets.items()):
                                if target["page"] == page:
                                    upload_targets.pop(target_id, None)
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name == "submit_email_application" and not probe and not read_only:
                            from applypilot.apply.email_application import submit_email_application
                            from applypilot.tracking.gmail_browser import BridgeGmailPages
                            owned = BridgeGmailPages(run, persist_state, native=native_call,
                                                    tool_schemas={key: tool.inputSchema for key, tool in tools.items()})
                            result = await submit_email_application(session_file.stem, arguments["route_id"], browser=owned)
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name == "inspect_application_route" and not probe and not read_only:
                            from applypilot.apply.browser_controls import inspect_control_code
                            page = owned_page(arguments, bound_pages)
                            control = (await run(inspect_control_code(page, arguments["ref"]))) if arguments.get("ref") else None
                            if control is not None and not str(control.get("href", "")).lower().startswith("mailto:"):
                                result = {"route": "external", "next_action": "Continue with this posting's browser application."}
                            else:
                                result = await email_handoff(page, control)
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name == "open_application_page" and not probe and not read_only:
                            from applypilot.apply.guard import attempt_status, GuardRejected
                            attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                            if state.get("email_handoff"):
                                return [TextContent(type="text", text=json.dumps(state["email_handoff"]))]
                            pages = await reconcile_pages()
                            live = {item["pageId"] for item in pages}
                            page = state.get("application_page")
                            if page not in live:
                                page = next((item["pageId"] for item in pages), None)
                            existing = page is not None
                            if not existing:
                                if attempt["status"] != "prepared":
                                    raise GuardRejected("application page cannot be opened after submit intent")
                                from applypilot.apply.routes import application_route_blocker
                                posting = json.loads(attempt.get("snapshot_json") or "{}")
                                route_reason = application_route_blocker({**posting, "application_url": attempt["target"],
                                                                         "application_route": attempt["channel"]})
                                if route_reason:
                                    return [TextContent(type="text", text=json.dumps(
                                        await defer_blocker("application_route_unresolved")))]
                                from applypilot.discovery.url_normalize import is_linkedin_auth_url
                                if is_linkedin_auth_url(attempt["target"]):
                                    # Older stored routes have not passed current normalization.
                                    # Do not change a fenced reservation or browse a signup/feed detour.
                                    return [TextContent(type="text", text=json.dumps(
                                        await defer_blocker("application_route_unresolved")))]
                                from applypilot.apply import circuit_breaker
                                domain = circuit_breaker.domain_from_target(attempt["target"])
                                if domain and (await asyncio.to_thread(circuit_breaker.target_status, attempt["target"]))["open"]:
                                    raise GuardRejected("Application domain is currently restricted; do not start the form.")
                                value = await run('return {page_id:await browser.pages.newPage(' + json.dumps(attempt["target"]) + ')};')
                                page = value.get("page_id") if isinstance(value, dict) else None
                                if isinstance(page, bool) or not isinstance(page, int):
                                    raise RuntimeError("browser_page_id_missing")
                                bound_pages[str(page)] = attempt["target"]
                                # This persistence is the ownership boundary. If it
                                # fails, an in-process retry returns this same page.
                                await persist_state({"bound_pages": dict(bound_pages), "application_page": page},
                                                    event="page_binding", critical=True)
                            observation = None
                            if str(page) not in previous_completion and attempt["status"] == "prepared":
                                observation = await initial_observation(page)
                            payload = {
                                "page_id": page,
                                "existing": existing,
                                "baseline_ready": str(page) in previous_completion,
                                "initial_observation": observation,
                            }
                            payload["application_route"] = {"route": "uninspected",
                                "next_action": "Read the posting's availability and application instructions. Use inspect_application_route only for an explicit email application."}
                            return [TextContent(type="text", text=json.dumps(payload))]
                        if name == "inspect_application_uploads" and not probe and not read_only:
                            from applypilot.apply.guard import record_required_browser_uploads
                            page = owned_page(arguments, bound_pages)
                            observed = await run(inspect_uploads_code(page))
                            inputs = observed.get("inputs") if isinstance(observed, dict) else None
                            if not isinstance(inputs, list) or any(
                                not isinstance(item, dict) or type(item.get("backend_node_id")) is not int
                                for item in inputs
                            ):
                                raise RuntimeError("Could not inspect file inputs on this application page.")
                            await asyncio.to_thread(record_required_browser_uploads, session_file.stem, page, inputs)
                            for target_id, target in list(upload_targets.items()):
                                if target["page"] == page:
                                    upload_targets.pop(target_id, None)
                            result_inputs = []
                            for item in inputs:
                                target_id = uuid4().hex
                                upload_targets[target_id] = {"page": page, "node": item["backend_node_id"]}
                                result_inputs.append({key: value for key, value in item.items() if key != "backend_node_id"} | {"target_id": target_id})
                            return [TextContent(type="text", text=json.dumps({"page": page, "inputs": result_inputs}))]
                        if name == "upload_application_document" and not probe and not read_only:
                            from applypilot.apply.guard import begin_browser_upload, finish_browser_upload
                            page = owned_page(arguments, bound_pages)
                            ref, target_id = arguments.get("ref"), arguments.get("target_id")
                            node = None
                            if ref is None:
                                target = upload_targets.get(target_id)
                                if not target or target["page"] != page:
                                    raise ValueError("Unknown upload target for this page; call inspect_application_uploads again.")
                                node = target["node"]
                            document = arguments.get("document")
                            receipt = await asyncio.to_thread(begin_browser_upload, session_file.stem, document, page)
                            schema = tools["upload"].inputSchema.get("properties", {}) if "upload" in tools else {}
                            file_key = "files" if "files" in schema else "file" if "file" in schema else None
                            if ref is not None and file_key:
                                args = {"page": page, "ref": ref,
                                        file_key: [receipt["path"]] if file_key == "files" else receipt["path"]}
                                # No automatic retry after an uncertain upload.
                                # Inspect the selected bytes before accepting a failed response.
                                try:
                                    await forward("upload", args)
                                except Exception:
                                    observed = await run(upload_code(page, ref=ref, backend_node_id=None, attach=False))
                                    if not isinstance(observed, dict) or observed.get("sha256") != receipt["sha256"]:
                                        raise
                                else:
                                    observed = await run(upload_code(page, ref=ref, backend_node_id=None, attach=False))
                            else:
                                log.info("browser_fallback operation=upload reason=%s backend=cdp",
                                         "hidden_input_without_ref" if ref is None else "native_upload_unavailable")
                                observed = await run(upload_code(page, ref=ref, backend_node_id=node, path=receipt["path"]))
                            if not isinstance(observed, dict):
                                raise RuntimeError("Upload executed but no file metadata was returned; attachment is unverified.")
                            verified = await asyncio.to_thread(finish_browser_upload, session_file.stem, document, page, observed)
                            await persist_state({"last_form_progress_at": datetime.now(UTC).isoformat()},
                                                event="document_uploaded", critical=False)
                            return [TextContent(type="text", text=json.dumps(verified))]
                        if name == "read_browser_output" and not probe:
                            from applypilot.apply.guard import GuardRejected
                            output_id = arguments.get("output_id")
                            offset = arguments.get("offset", 0)
                            maximum = arguments.get("max_chars", MAX_BROWSER_OUTPUT_CHARS)
                            content = output_paths.get(output_id)
                            if content is None and len(output_id) == 64 and context_store is not None:
                                try:
                                    source = await asyncio.to_thread(context_store.read, output_id)
                                    content = source["text"]
                                except (OSError, ValueError) as exc:
                                    raise GuardRejected("browser output is not from this application's context") from exc
                            if content is None:
                                raise GuardRejected("browser output is not from this wrapper invocation")
                            from applypilot.reasoning.observation import browser_output_excerpt
                            body = browser_output_excerpt(content, output_id, offset=offset, max_chars=maximum,
                                                          budget_chars=MAX_BROWSER_OUTPUT_CHARS)
                            return [TextContent(type="text", text=json.dumps(body, ensure_ascii=False, separators=(",", ":")))]
                        if name == "submit_application" and not probe and not read_only:
                            from applypilot.apply.guard import (attempt_status, mark_side_effect_intent, require_verification, browser_activity_uncertain,
                                                               record_required_browser_uploads, GuardRejected)
                            from applypilot.apply.submission_evidence import capture, require_baseline
                            from applypilot.apply.browser_controls import inspect_control_code
                            from applypilot.apply import circuit_breaker
                            page = owned_page(arguments, bound_pages)
                            ref = arguments["ref"]
                            attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                            await reconcile_editing_state(attempt)
                            if attempt["status"] == "side_effect_intent":
                                rejected = await resume_rejected_submission(page) or await resume_intermediate_submission(page)
                                if rejected:
                                    return [TextContent(type="text", text=json.dumps(rejected))]
                            if attempt["status"] != "prepared" or state.get("submission_attempted_at"):
                                raise GuardRejected("Submission was already attempted or is held; use capture_submission, never click again.")
                            if await asyncio.to_thread(browser_activity_uncertain, attempt):
                                from applypilot.apply.guard import defer_application
                                await asyncio.to_thread(defer_application, session_file.stem, "unknown_browser_activation")
                                raise GuardRejected("Earlier browser activation has an unknown outcome. This reservation requires verification.")
                            domain = circuit_breaker.domain_from_target(attempt["target"])
                            if domain and (await asyncio.to_thread(circuit_breaker.target_status, attempt["target"]))["open"]:
                                raise GuardRejected("Application domain is currently restricted; submission was not clicked.")
                            if not any(p.get("pageId") == page for p in await owned_pages()):
                                raise GuardRejected("The application page is no longer owned by this connection.")
                            observation = await run(observation_code(page))
                            if not isinstance(observation, dict) or not observation.get("text"):
                                raise GuardRejected("The application page could not be observed before submission.")
                            if observation.get("completion_visible"):
                                await asyncio.to_thread(require_verification, session_file.stem,
                                                        reason="The page already shows completion before the guarded Submit command.", quarantine=True)
                                raise GuardRejected("Completion is already visible. Do not submit this application again.")
                            await save_baseline(page, observation["text"], False)
                            require_baseline(page, bound_pages, previous_completion, completion_baseline_at, attempt["target"])
                            control = await run(inspect_control_code(page, ref))
                            if isinstance(control, dict) and str(control.get("href", "")).lower().startswith("mailto:"):
                                return [TextContent(type="text", text=json.dumps(await email_handoff(page, control)))]
                            if not isinstance(control, dict) or control.get("disabled"):
                                raise ValueError("Choose an enabled final application Submit control from a fresh snapshot.")
                            uploads = await run(inspect_uploads_code(page))
                            if not isinstance(uploads, dict) or not isinstance(uploads.get("inputs"), list):
                                raise GuardRejected("Required uploads could not be inspected before Submit.")
                            await asyncio.to_thread(record_required_browser_uploads, session_file.stem, page, uploads["inputs"])
                            await verify_documents(page, uploads["inputs"], attempt)
                            await persist_state({"submission_prepared": {"page": page, "ref": ref,
                                                                          "label": control.get("label"),
                                                                          "form_identity": control.get("form_identity"),
                                                                          "target": control.get("submit_target")}},
                                                event="submission_prepared", critical=True)
                            await asyncio.to_thread(mark_side_effect_intent, session_file.stem)
                            try:
                                await persist_state({"submission_attempted_at": datetime.now(UTC).isoformat()},
                                                    event="submission_attempt", critical=True)
                                await forward("act", {"page": page, "kind": "click", "ref": ref}, record_progress=True)
                            except Exception:
                                current = await asyncio.to_thread(attempt_status, session_file.stem)
                                if current["status"] == "side_effect_intent":
                                    await asyncio.to_thread(require_verification, session_file.stem,
                                                            reason="The final submission command did not return; never click Submit again.", quarantine=True)
                                raise
                            try:
                                evidence = await capture(session_file.stem, page, bound_pages, previous_completion,
                                                         completion_baseline_at, owned_pages, run, state["invocation"])
                                result = await asyncio.to_thread(commit_capture, evidence)
                            except GuardRejected:
                                result = await resume_rejected_submission(page) or await resume_intermediate_submission(page) or {
                                    "status": "side_effect_intent", "committed": False,
                                    "next_action": "Wait for the page response, then call capture_submission to confirm success or inspect a form rejection. Do not repeat an uncertain submission."}
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name == "capture_submission" and not probe and not read_only:
                            from applypilot.apply.guard import attempt_status, GuardRejected
                            from applypilot.apply.submission_evidence import capture, evidence_path
                            page = owned_page(arguments, bound_pages)
                            attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                            await follow_completion_page(page, attempt)
                            saved = read_state(evidence_path(session_file.stem))
                            if saved.get("kind") and saved.get("token") and saved.get("page_id") == page:
                                try:
                                    result = await asyncio.to_thread(
                                        commit_capture, {"proof_kind": saved["kind"], "proof": saved["token"]})
                                except GuardRejected:
                                    pass
                                else:
                                    return [TextContent(type="text", text=json.dumps(result))]
                            try:
                                evidence = await capture(session_file.stem, page, bound_pages,
                                                         previous_completion, completion_baseline_at,
                                                         owned_pages, run, state["invocation"])
                            except GuardRejected:
                                rejected = await resume_rejected_submission(page) or await resume_intermediate_submission(page)
                                if rejected:
                                    return [TextContent(type="text", text=json.dumps(rejected))]
                                raise
                            result = await asyncio.to_thread(commit_capture, evidence)
                            return [TextContent(type="text", text=json.dumps(result))]
                        if name not in tools:
                            raise ValueError("Unknown BrowserOS tool")
                        if read_only and name == "tabs" and arguments.get("action") not in {"new", "list"}:
                            raise ValueError("Read-only applications may only open or list their inspection pages.")
                        if not probe and not read_only:
                            from applypilot.apply.guard import attempt_status, GuardRejected
                            if name in {"run", "evaluate"}:
                                raise GuardRejected("Use native read/snapshot/act tools; application JavaScript cannot bypass guarded submission.")
                            page = arguments.get("page")
                            if page is not None:
                                if str(page) not in bound_pages:
                                    await reconcile_pages()
                                owned_page(arguments, bound_pages)
                            if name == "act":
                                kind = str(arguments.get("kind") or "")
                                if kind in {"click_at", "type_at"}:
                                    raise GuardRejected("Use a current element ref; coordinate actions cannot identify the guarded application control.")
                                key = arguments.get("key", "")
                                key_parts = [part.strip().casefold() for part in re.split(r"[+\-]", key)] if isinstance(key, str) else []
                                if kind == "press" and (key == " " or key_parts and key_parts[-1] in {"enter", "return", "space", "spacebar"}):
                                    raise GuardRejected("Activate the intended button by ref so application submission can be guarded.")
                                if kind == "click":
                                    from applypilot.apply.browser_controls import inspect_control_code
                                    page = owned_page(arguments, bound_pages)
                                    ref = arguments.get("ref")
                                    if not isinstance(ref, str) or not re.fullmatch(r"e\d+", ref):
                                        raise GuardRejected("Clicks require a current element ref.")
                                    control = await run(inspect_control_code(page, ref))
                                    if not isinstance(control, dict):
                                        raise GuardRejected("The action control could not be inspected; refresh the snapshot.")
                                    if str(control.get("href") or "").lower().startswith("mailto:"):
                                        return [TextContent(type="text", text=json.dumps(await email_handoff(page, control)))]
                                    if control.get("submission_capable"):
                                        return await dispatch_tool("submit_application", {"page": page, "ref": ref})
                                if kind in {"click", "fill", "type", "check", "uncheck", "select", "press", "dialog_accept", "drag", "drag_at"}:
                                    attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                                    if attempt["status"] != "prepared" or state.get("submission_attempted_at"):
                                        raise GuardRejected("A submission outcome is pending; use capture_submission before changing the form.")
                            if name in {"navigate", "tabs"}:
                                target_url = str(arguments.get("url") or "")
                                if target_url and not target_url.lower().startswith(("https://", "http://")):
                                    raise GuardRejected("Application navigation requires an HTTP(S) URL.")
                            if name == "navigate" or name in {"tabs", "history", "windows"} and arguments.get("action") != "list":
                                attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                                if attempt["status"] != "prepared" or state.get("submission_attempted_at"):
                                    raise GuardRejected("Preserve the pending submission page; use capture_submission before navigating.")
                        page_may_change = _may_change_page(name, arguments)
                        result, _ = await forward(
                            name, arguments, record_progress=form_progress(name, arguments),
                            expose_outputs=True, source="agent",
                        )
                        if not probe and not read_only and page_may_change:
                            try:
                                await reconcile_pages()
                            except Exception as exc:
                                # Return the native result even if follow-up inspection
                                # fails; retrying a completed action could duplicate it.
                                wrapper_failure("page_reconciliation", exc)
                        return result
                    except Exception as exc:
                        from applypilot.apply.guard import GuardRejected
                        if isinstance(exc, GuardRejected):
                            raise
                        from applypilot.operations.runtime import sanitize
                        reason = transport_failure(exc)
                        if reason:
                            wrapper_failure("transport_command_failed", exc)
                            await failed_transport(exc, reason)
                        else:
                            wrapper_failure("browser_command_failed", exc)
                            await persist_state({
                                "last_command_error": sanitize(exc, 500),
                                "last_command_error_at": datetime.now(UTC).isoformat(),
                            }, event="command_error", critical=False)
                        raise

                # BrowserOS validates its own tools. Validate only our helpers here.
                @server.call_tool(validate_input=False)
                async def call_tool(name: str, arguments: dict):
                    # Concurrent model tool calls must not race refs, pending upload
                    # receipts, or completion observations on the same application.
                    async with application_lock:
                        request_started = time.time()
                        action_may_have_executed = False
                        try:
                            if name not in available_tools:
                                raise ValueError("Unknown or unavailable BrowserOS tool; use the advertised catalogue.")
                            if name in helper_validators:
                                helper_validators[name].validate(arguments)
                            action_may_have_executed = True
                            result = await dispatch_tool(name, arguments)
                        except ValidationError as exc:
                            message = f"Invalid {name} input at {exc.json_path}: {exc.validator}"
                            result = CallToolResult(isError=True, content=[TextContent(type="text", text=message)])
                        except Exception as exc:
                            result = CallToolResult(isError=True, content=[TextContent(type="text", text=str(exc))])
                        if isinstance(result, CallToolResult) and result.isError:
                            from applypilot.reasoning.observation import process_browser_failure
                            from applypilot.reasoning.learning_store import record_tool
                            wire = await asyncio.to_thread(process_browser_failure, result, context_store,
                                "browser failure " + state["invocation"] + " " + str(time.time_ns()) + " " + name,
                                action_may_have_executed=action_may_have_executed)
                            raw_id = json.loads(wire["content"][0]["text"])["raw_output_id"]
                            # The raw failure is already archived. Slow learning
                            # storage must not hold the application command lock.
                            capture_learning(record_tool, os.getenv("APPLYPILOT_AGENT_TRACE_ID", ""),
                                "browseros_neo", name, arguments, wire, {"ref": raw_id} if raw_id else {}, True,
                                request_started=request_started)
                            return CallToolResult.model_validate(wire)
                        if isinstance(result, list) and name in {
                            "open_application_page", "capture_submission", "submit_application",
                            "inspect_application_uploads", "upload_application_document",
                            "inspect_application_route", "submit_email_application", "act",
                        }:
                            # Redirected native acts return this same adapter-owned
                            # list; ordinary native calls return CallToolResult.
                            # The perception model must not rewrite the disposition.
                            receipt = {}
                            for block in result:
                                if isinstance(block, TextContent):
                                    try:
                                        body = json.loads(block.text)
                                    except ValueError:
                                        continue
                                    if isinstance(body, dict):
                                        receipt.update({key: value for key, value in body.items() if key in {
                                            "status", "committed", "guard_status", "submission_disposition",
                                            "next_action", "reason", "proof", "proof_kind", "route", "route_id",
                                            "page", "current_url", "url", "target_id", "document", "sha256",
                                        }})
                            result = CallToolResult(content=result, _meta={"applypilot_action_receipt": receipt})
                        return result

                heartbeat_task = asyncio.create_task(heartbeat(), name="browseros-owning-session-heartbeat")
                try:
                    async with stdio_server() as (read, write):
                        await server.run(read, write, server.create_initialization_options())
                finally:
                    heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await heartbeat_task
                    # Only this still-owning connection may clean connection-scoped pages.
                    preserve = False
                    if not probe and not read_only:
                        try:
                            from applypilot.apply.guard import attempt_status
                            attempt = await asyncio.to_thread(attempt_status, session_file.stem)
                            preserve = attempt["channel"] != "email" and attempt["status"] in {"side_effect_intent", "verification_required", "verification_quarantined"}
                        except Exception:
                            preserve = True
                    cleanup = "preserved" if preserve else "unknown"
                    try:
                        if not preserve:
                            for page in await owned_pages():
                                await run('await browser.pages.close(' + json.dumps(page["pageId"]) + ');return {closed:true};')
                            cleanup = "complete"
                    except Exception:
                        cleanup = "unknown"
                    await persist_state({"cleanup": cleanup, "state": "closed"},
                                        event="bridge_closed", critical=True)
    except BaseException as exc:
        from applypilot.operations.runtime import sanitize
        await persist_state({
            "state": "failed",
            "error": sanitize(exc, 500),
            "terminal_reason": state.get("terminal_reason") or transport_failure(exc) or "browser_bridge_unavailable",
            "terminal_at": state.get("terminal_at") or datetime.now(UTC).isoformat(),
        }, event="bridge_failed", critical=False)
        raise
    finally:
        try:
            if learning_captures:
                # Keep every capture owned through physical completion, including
                # cancellation; the learning corpus remains active by default.
                captures = asyncio.gather(*learning_captures)
                try:
                    await asyncio.shield(captures)
                except asyncio.CancelledError:
                    while not captures.done():
                        try:
                            await asyncio.shield(captures)
                        except asyncio.CancelledError:
                            continue
                    raise
        finally:
            import shutil
            shutil.rmtree(context_dir, ignore_errors=True)


def main() -> None:
    from applypilot.apply.agent_trace import child_logging
    child_logging("browser")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--session-file", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--read-only", action="store_true")
    args = parser.parse_args()
    from applypilot.processes import process_guard
    with process_guard(args.session_file.with_suffix(".lock")):
        asyncio.run(serve(args.url, args.session_file, probe=args.probe, read_only=args.read_only))


if __name__ == "__main__":
    main()

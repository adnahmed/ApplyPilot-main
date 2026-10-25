"""Turn captured episodes into causally ordered, explicitly proxy-scored cases.

Historical exports without launch prompts/tool schemas remain diagnostics. They
are not invented replay fixtures, positive labels, or independently verified runs.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from .learning_store import (add_example, database, dumps, finish_episode, record_tool,
                             root, start_episode)


def diagnostics(calls: list[dict]) -> dict:
    """Conservative, inspectable behavioral signals, never ground-truth rewards."""
    counts = {name: 0 for name in ("repeated_fill", "repeated_error", "same_observation",
              "option_ref_available_before_dom_search", "repeated_context_load", "aleph_admin_attempt")}
    previous, loaded = None, set()
    for row in calls:
        args = row.get("arguments", {})
        text = dumps(row.get("observation", ""))
        signature = dumps([row.get("server"), row.get("tool"), args])
        tool = row.get("tool", "")
        if row.get("server") == "aleph":
            if tool in {"configure", "run_command", "add_remote_server", "list_remote_servers", "remote_call_tool"}:
                counts["aleph_admin_attempt"] += 1
            if tool in {"load_file", "load_context", "load_workspace_manifest"}:
                if signature in loaded:
                    counts["repeated_context_load"] += 1
                loaded.add(signature)
        if previous:
            old, old_signature, old_text = previous
            if signature == old_signature:
                if args.get("kind") in {"fill", "type", "type_text"} or tool in {"fill", "type_text"}:
                    counts["repeated_fill"] += 1
                if old.get("is_error"):
                    counts["repeated_error"] += 1
            if text == old_text:
                counts["same_observation"] += 1
            if tool in {"run", "evaluate", "query", "grep"} and re.search(r'option.{0,160}(?:ref=|"ref")', old_text, re.I):
                if re.search(r"querySelector|role.{0,20}option", dumps(args), re.I):
                    counts["option_ref_available_before_dom_search"] += 1
        previous = row, signature, text
    return {"signals": counts, "basis": "heuristics_for_reflection_not_success_labels", "calls": len(calls)}


def read_events(segments: list[dict]) -> list[dict]:
    seen, result = set(), []
    for segment in sorted(segments, key=lambda s: s.get("segment", 0), reverse=True):
        for line in str(segment.get("text", "")).splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
            except ValueError:
                continue
            key = hashlib.sha256(dumps(row).encode()).hexdigest()
            if key not in seen:
                seen.add(key)
                result.append(row)
    return result  # Preserve segment/stream order, not sorted timestamp guesses.


def import_bundle(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("format") != "applypilot_run_debug_bundle":
        raise ValueError("Use an ApplyPilot run debug export")
    imported, reports = 0, []
    for item in value.get("application_traces", []):
        trace_id = item.get("trace_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", trace_id):
            continue
        with database() as conn:
            if conn.execute("SELECT 1 FROM episodes WHERE trace_id=?", (trace_id,)).fetchone():
                continue
        events = read_events(item.get("sources", {}).get("agent", {}).get("segments", []))
        pending, calls, unmatched = {}, [], 0
        for event in events:
            if event.get("event") == "tool_requested":
                pending[event.get("tool_call_id")] = event
            elif event.get("event") == "tool_returned":
                request = pending.pop(event.get("tool_call_id"), None)
                if request is None:
                    unmatched += 1
                    continue
                name = request.get("tool", "")
                match = re.fullmatch(r"mcp__(.+?)__(.+)", name)
                if not match:
                    continue
                server, tool = match.groups()
                calls.append({"server": server, "tool": tool, "arguments": request.get("arguments", {}),
                              "observation": event.get("observation", ""), "is_error": bool(event.get("is_error"))})
        # An expired worker report is not a terminal episode. No original
        # launch prompt or exact schemas are present in older trace formats.
        start_episode(trace_id, group_id="historical:"+trace_id, attempt_id="", prompt="", system_prompt="",
                      model="historical_unknown", context_root="", revision="historical_unknown")
        for call in calls:
            record_tool(trace_id, **call, source={"origin": "uploaded_debug_export"})
        finish_episode(trace_id, status="imported_incomplete", infrastructure=False)
        report = {"trace_id": trace_id, **diagnostics(calls), "unmatched_results": unmatched,
                  "pending_requests": len(pending), "eligible_for_replay": False,
                  "reason": "Original launch prompt and exact tool catalog are unavailable; diagnostic evidence only."}
        reports.append(report)
        imported += len(calls)
    result = {"imported_calls": imported, "episodes": reports, "private_data": True}
    from .source_files import SourceFiles
    root().mkdir(parents=True, exist_ok=True, mode=0o700)
    SourceFiles._write(root()/"last-import.json", dumps(result))
    return result


def hydrate_history(messages: list[dict], trace_id: str, conn) -> list[dict]:
    """Expand a captured prefix without duplicating every prefix in the corpus."""
    state = json.loads(messages[0]["content"])
    source = state.get("history_source")
    if not source:
        return messages
    if source["trace_id"] != trace_id:
        raise ValueError("Replay history belongs to a different episode")
    rows = conn.execute("SELECT server,tool,arguments,observation,is_error FROM tool_calls "
                        "WHERE trace_id=? AND created<? ORDER BY id", (trace_id, source["before_request"])).fetchall()
    history = [{**dict(row), "arguments": json.loads(row["arguments"]),
                "observation": json.loads(row["observation"])} for row in rows]
    if len(history) >= len(state["history"]):
        state["history"] = history
    state["history_window_partial"] = len(history) != source["captured_calls"]
    state.pop("history_source")
    return [{**messages[0], "content": dumps(state)}, *messages[1:]]


def materialize_cases() -> dict:
    """Build one-step cases using only observations BEFORE each recorded action.

    Last-action labels are kept as diagnostic output, never used as an answer
    key. Independent fixed-rubric evaluation scores a NEW predicted action.
    """
    from .application_policy import BEGIN, contract_schema
    from .settings import BROWSER_TASKS
    schemas = {task: contract_schema(task) for task in BROWSER_TASKS}
    added, skipped = 0, 0
    with database() as conn:
        episodes = [dict(row) for row in conn.execute("SELECT * FROM episodes WHERE finished IS NOT NULL ORDER BY created DESC LIMIT 100")]
    for episode in episodes:
        task = episode.get("task_kind", "application_agent")
        if task not in schemas:
            skipped += 1
            continue
        schema = schemas[task]
        prompt = json.loads(episode["prompt"])
        from .artifacts import schema_digest
        from .settings import routing_fingerprint
        if (not isinstance(prompt, str) or BEGIN not in prompt or episode["infrastructure"]
                or episode["contract_hash"] != schema_digest(schema)
                or episode["routing"] != routing_fingerprint()):
            skipped += 1
            continue
        with database() as conn:
            catalog = {r["server"]: json.loads(r["catalog"]) for r in conn.execute("SELECT * FROM tools WHERE trace_id=?", (episode["trace_id"],))}
            calls = [dict(r) for r in conn.execute("SELECT * FROM tool_calls WHERE trace_id=? ORDER BY id", (episode["trace_id"],))]
        if not catalog or (task != "application_assist" and not json.loads(episode["skill_files"] or "{}")):
            skipped += 1
            continue
        parsed = [{**r, "arguments": json.loads(r["arguments"]), "observation": json.loads(r["observation"])} for r in calls]
        for row in calls:
            call = {**row, "arguments": json.loads(row["arguments"]), "observation": json.loads(row["observation"])}
            # Captured catalog covers exactly what the production proxy exposed.
            if row["server"] not in catalog or row["tool"] not in {t["name"] for t in catalog[row["server"]]}:
                continue
            started = row.get("request_started", 0)
            if not started:
                continue  # Old captures cannot establish pre-decision ordering.
            overlapping = any(other["id"] != row["id"] and other.get("request_started", 0)
                and other["request_started"] < row["created"] and other["created"] > started for other in calls)
            visible_history = [h for h in parsed if h["created"] < started]
            if not overlapping:
                state = {"task_kind": task, "prompt": prompt, "system_prompt": json.loads(episode["system_prompt"]),
                         "model": episode["model"], "tools": catalog,
                         "skill_files": json.loads(episode["skill_files"] or "{}"),
                         "history": [{k: h[k] for k in ("server", "tool", "arguments", "observation", "is_error")} for h in visible_history[-6:]],
                         "history_source": {"trace_id": episode["trace_id"], "before_request": started,
                                            "captured_calls": len(visible_history)},
                         "ordering": "only results completed before this request; overlapping requests excluded",
                         "history_window_partial": len(visible_history) > 6,
                         "scope": "next action only; no future result or terminal outcome is shown"}
                message = [{"role": "user", "content": dumps(state)}]
                add_example(task, message, schema, output={"tool": f"mcp__{row['server']}__{row['tool']}",
                            "arguments": call["arguments"]}, group_id=episode["group_id"],
                            provenance="browser_next_action_replay", trace_id=episode["trace_id"],
                            params={"model": episode["model"], "max_tokens": 4096, "temperature": 0.0, "routing": episode["routing"]})
                added += 1
    return {"cases_considered": added, "episodes_skipped": skipped}

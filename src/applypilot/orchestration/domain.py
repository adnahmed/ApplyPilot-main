"""Native core adapters, plus explicitly deferred non-core service integrations.

This boundary is intentionally explicit rather than a new plugin/DSL framework.
The pre-existing model, evidence, document and external-action implementations
remain the source of business behavior. See the feature audit for constraints
inside those implementations which have NOT yet been migrated.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .contracts import ActivityRequest, Observation, WorkPage, digest
from . import store


def options(root: dict, stop) -> dict:
    from applypilot.config import DEFAULTS
    raw = root['options']
    return {"context": store.context_from(root), "stop_event": stop,
        "min_score": DEFAULTS['min_score'] if raw.get('min_score') is None else raw['min_score'],
        "max_score": raw.get('max_score'),
        "max_age_days": DEFAULTS['max_job_age_days'] if raw.get('max_age_days') is None else raw['max_age_days'],
        "doc_format": raw.get('doc_format') or 'pdf',
        "defer_document_rendering": True}


def _document_path(value: str, root: Path) -> Path:
    """Cross-worker references are workspace-relative, never caller host paths."""
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not value:
        raise ValueError("Document references must be relative to the workspace")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Document reference escaped its workspace")
    return resolved


def plan(request: ActivityRequest, stop) -> WorkPage:
    from applypilot.database import get_connection
    from applypilot.discovery import repository
    from applypilot.work_context import stage_scope
    conn = get_connection()
    root = store.get(conn, request.reference)
    cfg = options(root, stop)
    context = cfg["context"]
    # No source-cycle lease is acquired. This only publishes the policy snapshot.
    repository.activate_context(context, conn, refresh_limit=0,
                                schema_initialized=True)
    kind = request.options["kind"]
    size = request.options["page_size"]
    from .contracts import validate_request_options
    raw = validate_request_options(root["options"])
    remaining_limit = None
    if request.options.get("native_selection") and raw.get("limit") is not None:
        selected_count = request.options.get("selected")
        if type(selected_count) is not int or selected_count < 0:
            raise ValueError("Native selection requires a nonnegative selected entity count")
        remaining_limit = max(0, raw["limit"] - selected_count)
        size = min(size, remaining_limit)
        if not size:
            conn.commit()
            return WorkPage()
    if kind in {"discover", "source_refresh"}:
        from dataclasses import asdict
        from applypilot.discovery.planner import plan_tasks
        from applypilot.discovery.capabilities import access_reason, execution_reason
        if raw.get("target_ids") is not None:
            # An exact owner target must not depend on unrelated provider
            # inventory files, signal-reader configuration or registry expansion.
            from applypilot.targets.intent import tasks as target_tasks
            tasks = [task for task in target_tasks(context)
                     if task.target.scope in raw["target_ids"]]
        else:
            tasks = list(plan_tasks(context))
            tasks = [task for task in tasks if task.target.provider != "company_targets"]
        from applypilot.discovery.free_signals import reader_targets
        from applypilot.discovery.transport import TaskDeferred
        from dataclasses import replace
        expanded = []
        for task in tasks:
            try:
                targets = reader_targets(task.target, context)
            except TaskDeferred:
                # Missing configuration belongs to this provider's child, not
                # the planner or unrelated providers. It is not an empty success.
                targets = (task.target,)
            expanded.extend(replace(task, target=target, logical_request_key=target.request_key())
                            for target in targets)
        tasks = expanded
        if raw.get("target_ids") is None and "ats_registry" in context.intent.sources_enabled:
            from applypilot.discovery.ats_adapter import list_registry_targets, union_board_targets
            from applypilot.discovery.models import SearchTask
            board = [task for task in tasks if task.target.kind == "board_refresh"
                     and task.target.provider in {"greenhouse", "lever", "ashby", "ats_registry"}]
            targets = union_board_targets((task.target for task in board), list_registry_targets(conn, context))
            tasks = [task for task in tasks if task not in board]
            tasks.extend(SearchTask(target=target, logical_request_key=target.request_key()) for target in targets)
        # Access grants are never inferred from having a Temporal worker online.
        tasks = [task for task in tasks if task.target and task.target.kind != "deferred"
                 and not access_reason(task.target, context.runtime) and not execution_reason(task.target)]
        # Explicit dashboard/API selections identify business targets, not workers.
        if raw.get("target_ids") is not None:
            tasks = [task for task in tasks if task.target.provider == "company_targets"
                     and task.target.scope in raw["target_ids"]]
        if raw.get("source_request_keys") is not None:
            tasks = [task for task in tasks if task.target.request_key() in raw["source_request_keys"]]
        tasks = sorted({task.target.request_key(): task for task in tasks}.items())
        if kind == "source_refresh" and request.options.get("native_selection"):
            from applypilot.discovery.freshness import recently_observed
            recent = recently_observed(conn, root["context_key"],
                request.options["selection_time"], request.options["source_refresh_age_seconds"])
            tasks = [(key, task) for key, task in tasks if key not in recent]
        remaining = [(key, task) for key, task in tasks if key > (request.cursor or "")]
        selected = remaining[:size]
        items = []
        for key, task in selected:
            reference = store.put(conn, {"snapshot": request.reference,
                "target": asdict(task.target), "kind": "discover"})
            items.append({"kind": "target" if task.target.provider == "company_targets" else "discover",
                          "subject": digest(key), "reference": reference})
        conn.commit()
        more = len(remaining) > size and (remaining_limit is None or size < remaining_limit)
        return WorkPage(items, selected[-1][0] if more else None)
    if kind not in {"jobs", "apply"}:
        raise ValueError("Unknown dispatch kind")
    # Keyset scan, not OFFSET. A page bound limits payload size, not total work.
    # Input rows are not claimed/reserved; Workflow IDs provide durable dispatch
    # deduplication, and domain output checks make completed phases no-ops.
    from .job_domain import query_for
    queries, parameters = [], []
    phases = request.options["job_phases"]
    selected_phases = (("apply",) if kind == "apply" and raw.get("limit") is not None and request.options.get("native_selection")
                       else (*phases, *(("apply",) if kind == "apply" else ())))
    for phase in selected_phases:
        query, params = query_for(conn, phase, cfg)
        queries.append("SELECT candidate.url FROM (" + query + ") candidate")
        parameters.extend(params)
    if not queries:
        conn.commit()
        return WorkPage()
    query = "SELECT DISTINCT url FROM (" + " UNION ALL ".join(queries) + ") pending WHERE url>? ORDER BY url LIMIT ?"
    rows = conn.execute(query, (*parameters, request.cursor or "", size + 1)).fetchall()
    selected = rows[:size]
    items = []
    for row in selected:
        if stop.is_set():
            from applypilot.execution import ExecutionStopped
            raise ExecutionStopped("Plan cancelled")
        from .contracts import job_identity
        entity_id = job_identity(request.options["workspace"], row["url"])
        reference = store.put(conn, {"snapshot": request.reference, "job_url": row["url"],
            "job_id": entity_id, "workspace": request.options["workspace"], "kind": kind})
        items.append({"kind": kind, "subject": entity_id, "reference": reference})
    conn.commit()
    more = len(rows) > size and (remaining_limit is None or size < remaining_limit)
    return WorkPage(items, selected[-1]["url"] if more else None)


def execute(request: ActivityRequest, stop, heartbeat) -> Observation:
    if request.phase in {"learning_collect", "learning_build_dataset", "learning_evaluate_current",
                         "learning_optimize", "learning_evaluate_candidate", "learning_publish"}:
        from .learning_domain import execute as learning_operation
        return learning_operation(request, stop, heartbeat)
    if request.phase in {"application_account_enqueue", "application_account_history"}:
        from .account_domain import execute as account_operation
        return account_operation(request)
    if request.phase in {"backup", "backup_full", "backup_check", "backup_points"}:
        from .backup_domain import execute as backup_operation
        return backup_operation(request, stop, heartbeat)
    from .mailbox_domain import PHASES as MAILBOX_PHASES, execute as mailbox_operation
    if request.phase in MAILBOX_PHASES:
        return mailbox_operation(request, stop, heartbeat)
    from applypilot.database import get_connection
    value, root = store.work_payload(get_connection(), request.reference)
    cfg = options(root, stop)
    phase = request.phase
    core = {"enrich", "company_research", "score", "tailor", "cover", "brief", "document_inspect",
            "application_prepare", "application_attempt", "application_evidence", "application_review"}
    if phase in core:
        from .contracts import job_identity
        expected = job_identity(request.options["workspace"], value["job_url"])
        supplied = request.options.get("job_id", request.options.get("application_id"))
        if value.get("job_id") != expected or supplied != expected:
            raise ValueError("Core operation identity does not match its immutable input")
    if phase in {"enrich", "score", "tailor", "cover", "brief"}:
        from .job_domain import execute_job
        return execute_job(phase, value, root, cfg)
    if phase == "document_inspect":
        from applypilot.document_recovery import inspect_job_documents
        result = inspect_job_documents(value["job_url"], min_score=cfg["min_score"],
            max_age_days=cfg["max_age_days"], doc_format=cfg["doc_format"])
        return Observation("complete", {"repairs": result["queued"]})
    elif phase in {"application_prepare", "application_attempt", "application_evidence", "application_review"}:
        from applypilot.apply import service
        semantic = {"application_prepare": service.prepare, "application_attempt": service.submit,
                    "application_evidence": service.inspect, "application_review": service.review}[phase]
        result = semantic(value, root, request.options)
        return Observation(result["state"], {key: value for key, value in result.items() if key != "state"})
    elif phase == "discovery_page":
        from .source_page import execute_page
        return execute_page(request, value, root, cfg, heartbeat)
    elif phase == "admission":
        from .job_domain import maintain_business_evidence
        return maintain_business_evidence(cfg, request.cursor)
    else:
        raise ValueError("Unsupported finite operation: " + phase)
    # Do not leak email bodies, generated documents, optimizer datasets or any
    # unknown plugin payload into Temporal history. Full results stay local.
    result = result if isinstance(result, dict) else {}
    state = str(result.get("state", result.get("status", "complete")))
    numbers = {key: value for key, value in result.items()
               if key in {"processed", "generated", "sent", "attempted", "complete", "reassessed", "repairs"}
               and type(value) in (int, float)}
    return Observation(state, numbers)

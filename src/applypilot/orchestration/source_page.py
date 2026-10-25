"""One source page per Activity, without discovery's Dask/budget/lease scheduler."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from importlib import import_module
from temporalio.exceptions import ApplicationError

from .contracts import Observation, digest
from . import store


def _target(value):
    from applypilot.discovery.models import SourceTarget
    return SourceTarget(**{**value,
        "request_params": tuple(tuple(pair) for pair in value.get("request_params", ())),
        "origins": tuple(value.get("origins", ())),
        "native_targets": tuple(value.get("native_targets", ()))})


def execute_page(request, value, root, cfg, heartbeat):
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery import repository
    from applypilot.discovery.capabilities import access_reason, capability_for, source_key
    from applypilot.discovery.models import DiscoveryPage, FailureKind, SearchTask
    from temporalio.exceptions import CancelledError
    context = cfg["context"]
    target = _target(value["target"])
    denied = access_reason(target, context.runtime)
    if denied:
        return Observation("source_access_denied", {"reason": denied})
    if target.provider == "company_targets":
        return Observation("owner_target", {"target_id": digest(target.request_key())})
    sweep = request.options.get("sweep")
    if not isinstance(sweep, str) or not sweep:
        raise ApplicationError("Missing discovery sweep identity", type="InvalidWorkInput", non_retryable=True)
    receipt_key = digest((sweep, request.reference, request.cursor))
    def replay_receipt(prior):
        acknowledgement = prior.get("signal_ack")
        if acknowledgement:
            from types import SimpleNamespace
            from applypilot.discovery.signal_cache import cache_for
            cache_for(context).acknowledge_publications(target.provider, root["context_key"],
                SimpleNamespace(**acknowledgement))
        return Observation(**prior["result"])

    try:
        prior = store.receipt(get_connection(), receipt_key)
    finally:
        close_connection()
    if prior:
        if prior["sequence"] != request.sequence:
            raise ApplicationError("Source pagination revisited a committed cursor",
                                   type="SourceContractError", non_retryable=True)
        return replay_receipt(prior)

    def before_request(resource="http", *args, **kwargs):
        # This is cancellation/access validation, not a capacity reservation.
        # Paid transport needs a real spend integration; never fabricate a grant.
        if cfg["stop_event"].is_set():
            raise CancelledError("Source Activity cancelled")
        if resource == "paid":
            raise ApplicationError("Paid source requires a verified billing adapter",
                                   type="SourceAccessDenied", non_retryable=True)
        heartbeat("source_request")
        return True

    def before_requests(resources):
        for resource in resources:
            before_request(resource)
        return True

    captured_signals = []

    def capture_signal(provider, post, revision, hiring_thread=False):
        from applypilot.signals.service import capture
        before_request()
        reference = capture(value['snapshot'], provider, post, revision, hiring_thread)
        if reference not in captured_signals:
            captured_signals.append(reference)

    context = replace(context, reserve_budget=before_request, reserve_budgets=before_requests,
                      capture_signal=capture_signal)
    task = SearchTask(task_id=digest((sweep, request.reference)), target=target,
        logical_request_key=target.request_key(), refresh_generation=0,
        intent_fingerprint=context.intent.fingerprint(), profile_version=context.applicant.fingerprint())
    adapter = import_module("applypilot.discovery." + capability_for(target).module)
    from applypilot.discovery.transport import TaskDeferred, browser_recovery, deferred_page
    from applypilot.integrations.types import SourceError
    recovery_reference = request.options.get("browser_recovery")
    recovery_url = None
    if recovery_reference:
        try:
            recovery = store.get(get_connection(), recovery_reference)
        finally:
            close_connection()
        if (recovery.get("kind") != "source_browser_challenge"
                or recovery.get("reference") != request.reference
                or recovery.get("cursor") != request.cursor):
            raise ValueError("Browser recovery does not belong to this source page")
        recovery_url = recovery["url"]
    try:
        with browser_recovery(recovery_url):
            page = adapter.execute_task(task, context, request.cursor)
    except SourceError as exc:
        challenge_url = getattr(exc, "challenge_url", None)
        recovery_policy = context.runtime["recovery"]
        allowed = recovery_policy.get("allowed_providers")
        if (not challenge_url or recovery_reference or not recovery_policy["enabled"]
                or allowed is not None and target.provider not in allowed):
            from applypilot.integrations.search_providers import retry_after
            delay = retry_after(str(exc.retry_after)) if exc.retry_after is not None else None
            if delay is not None and delay > 0:
                raise ApplicationError("Source provider did not complete the request",
                    next_retry_delay=timedelta(seconds=delay)) from exc
            raise
        try:
            conn = get_connection()
            reference = store.put(conn, {"kind": "source_browser_challenge", "url": challenge_url,
                "reference": request.reference, "cursor": request.cursor})
            conn.commit()
        finally:
            close_connection()
        return Observation("browser_challenge", {"reference": reference})
    except TaskDeferred as exc:
        page = deferred_page(task, context, exc, cursor=request.cursor)
    if not isinstance(page, DiscoveryPage) or page.task_id != task.task_id:
        raise ValueError("Source returned an invalid page contract")
    if page.failures:
        # History retains provider status/delay facts, not guessed error classes.
        statuses = sorted({f.http_status for f in page.failures if f.http_status is not None})
        from applypilot.integrations.search_providers import retry_after
        delays = [seconds for f in page.failures if f.retry_after is not None
                  if (seconds := retry_after(str(f.retry_after))) is not None and seconds > 0]
        raise ApplicationError("Source page failed" + (": HTTP " + ",".join(map(str, statuses)) if statuses else ""),
            next_retry_delay=timedelta(seconds=max(delays)) if delays else None)
    # Incomplete coverage describes evidence, not a reason to stop a cursor.
    terminal = bool(page.exhausted or (page.capped and page.next_cursor is None))
    if not terminal and (page.next_cursor is None or page.next_cursor == request.cursor):
        raise ValueError("Nonterminal provider page did not advance")
    # Fetch/model work has FINISHED before the short publication transaction.
    # Serialize only duplicate receipts for this exact page; no renewable lock,
    # scan under an allocation lease or DB transaction across external I/O.
    conn = get_connection()
    with repository.transaction(conn, resource="temporal-page:" + receipt_key):
        prior = store.receipt(conn, receipt_key)
        if prior:
            if prior["sequence"] != request.sequence:
                raise ApplicationError("Source cursor cycle", type="SourceContractError", non_retryable=True)
            committed = prior
        else:
            committed = None
        if committed is not None:
            # Duplicate delivery is settled after leaving this transaction.
            pass
        else:
            params = dict(target.request_params)
            counts = repository.publish_records(page.records, target.provider, context=context,
                tenant=target.scope, api_region=params.get("api_region"), origins=target.origins, conn=conn)
            if target.provider == "company_targets":
                from applypilot.discovery.company_targets import published
                published(conn, page)
            if target.kind == "company_inventory":
                from applypilot.discovery.freehire_registry import Company, store_companies
                rows = page.request_metadata.get("companies")
                if not isinstance(rows, list):
                    raise ValueError("Missing company inventory metadata")
                store_companies(conn, [Company.from_freehire(row) for row in rows], commit=False)
            elif page.request_metadata.get("board_leads"):
                from applypilot.discovery.freehire_registry import store_board_leads
                store_board_leads(conn, page.request_metadata["board_leads"], origin="temporal:" + task.task_id)
            state = "coverage_partial" if page.capped or page.partial or not page.complete else "exhausted" if page.exhausted else "published"
            coverage_complete = bool(page.complete and not page.partial and not page.capped)
            if terminal and page.exhausted and coverage_complete and request.options.get("coverage_clean") is True:
                from applypilot.discovery.freshness import record_complete
                record_complete(conn, root["context_key"], target.request_key(), receipt_key)
            result = Observation(state, {"found": counts.fetched, "new": counts.new,
                                         "terminal": terminal, "signal_ids": captured_signals,
                                         "coverage_complete": coverage_complete},
                                 page.next_cursor)
            committed = {"sequence": request.sequence, "result": asdict(result)}
            if page.request_metadata.get("signal_publication_receipts"):
                committed["signal_ack"] = {"task_id": page.task_id,
                    "records": [{"source_signal_id": row.get("source_signal_id"),
                                 "source_revision": row.get("source_revision")} for row in page.records],
                    "request_metadata": {"signal_publication_receipts": page.request_metadata["signal_publication_receipts"]}}
            store.save_receipt(conn, receipt_key, committed)
    # This can fail/retry independently without losing the publication receipt.
    return replay_receipt(committed)

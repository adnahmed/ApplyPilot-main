"""Native SDK Activity boundary; heartbeats/cancellation stay out of domain code."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import replace
import logging
import threading
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from .contracts import ActivityRequest, Observation, PHASE_ROLES, WorkPage
from .activity_runtime import provider_retry_delay

log = logging.getLogger(__name__)


@contextmanager
def execution(request):
    from applypilot.execution import activity_scope, ExecutionStopped
    from applypilot.database import close_connection
    info = activity.info()
    stop = threading.Event()
    finished = threading.Event()
    interval = request.options["heartbeat_interval_seconds"]
    if type(interval) not in (int, float) or not 0 < interval <= 60:
        raise ApplicationError("Invalid heartbeat interval", type="InvalidWorkInput", non_retryable=True)
    identity = f"{info.workflow_id}/{info.workflow_run_id}/{info.activity_id}"
    failures = []

    def heartbeat(phase="running"):
        # A keepalive reports the live Activity process, NOT useful progress.
        # Start-to-close still detects a stuck process that keeps heartbeating.
        activity.heartbeat({"phase": phase, "attempt": info.attempt})
        if activity.is_cancelled() or activity.is_worker_shutdown():
            stop.set()

    def monitor():
        while not finished.wait(interval):
            try:
                heartbeat()
            except Exception as exc:
                failures.append(type(exc).__name__)
                stop.set()
                return

    thread = threading.Thread(target=copy_context().run, args=(monitor,),
                              name="temporal-activity-heartbeat", daemon=True)
    heartbeat("starting")
    thread.start()
    try:
        with activity_scope(key=identity, stage=request.phase, stop_event=stop, attempt=info.attempt):
            if stop.is_set():
                raise CancelledError("Activity cancelled before domain work")
            yield stop, heartbeat
            if activity.is_cancelled():
                raise CancelledError("Activity cancellation observed")
            if failures:
                raise RuntimeError("Activity heartbeat failed: " + failures[0])
            heartbeat("completed")
    except ExecutionStopped as exc:
        if activity.is_cancelled():
            raise CancelledError("Domain operation cooperatively cancelled") from exc
        raise
    finally:
        finished.set()
        thread.join()
        close_connection()


class Activities:
    def __init__(self, role):
        self.role = role

    def _check(self, request):
        if PHASE_ROLES.get(request.phase) != self.role:
            raise ApplicationError("Activity was sent to an incompatible worker role",
                                   type="UnsupportedOperation", non_retryable=True)

    def _invoke(self, request, function):
        self._check(request)
        failure = None
        try:
            with execution(request) as (stop, heartbeat):
                # Cursor tokens can contain provider URLs or access tokens. Keep
                # their bodies in the same immutable reference store as inputs.
                from applypilot.database import get_connection
                from . import store
                conn = get_connection()
                if request.cursor:
                    token = store.get(conn, request.cursor)
                    if token.get("kind") != "cursor" or not isinstance(token.get("value"), str):
                        raise ApplicationError("Invalid cursor reference", type="InvalidWorkInput", non_retryable=True)
                    request = replace(request, cursor=token["value"])
                result = function(request, stop, heartbeat)
                if result.next_cursor is not None:
                    conn = get_connection()
                    reference = store.put(conn, {"kind": "cursor", "value": result.next_cursor})
                    conn.commit()  # Must be durable before returning to Temporal.
                    result = replace(result, next_cursor=reference)
                return result
        except (CancelledError, ApplicationError):
            raise
        except Exception as exc:
            log.exception("Temporal operation failed phase=%s", request.phase)
            failure = ApplicationError("Operation failed; inspect worker diagnostics",
                                       next_retry_delay=provider_retry_delay(exc))
        # Raise outside the except suite so SDK failure encoding cannot traverse
        # the original provider exception's potentially sensitive context.
        raise failure

    @activity.defn(name="applypilot.operation.v1")
    def operation(self, request: ActivityRequest) -> Observation:
        from .domain import execute
        return self._invoke(request, lambda decoded, stop, heartbeat: execute(decoded, stop, heartbeat))

    @activity.defn(name="applypilot.plan.v1")
    def plan(self, request: ActivityRequest) -> WorkPage:
        from .domain import plan
        return self._invoke(request, lambda decoded, stop, heartbeat: plan(decoded, stop))

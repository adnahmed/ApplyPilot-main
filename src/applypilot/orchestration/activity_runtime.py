"""Native SDK heartbeat/cancellation plumbing; no legacy execution-engine import."""
from contextlib import contextmanager
from contextvars import copy_context
from datetime import timedelta
import math
import threading

from temporalio import activity
from temporalio.exceptions import CancelledError


def provider_retry_delay(error):
    seconds = getattr(error, 'retry_after', None)
    if seconds is None:
        headers = getattr(getattr(error, 'response', None), 'headers', None)
        if headers is not None:
            from applypilot.integrations.search_providers import retry_after
            seconds = retry_after(headers.get('retry-after'))
    try:
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds <= 0:
            return None
        return timedelta(seconds=seconds)
    except OverflowError:
        return None


@contextmanager
def heartbeats(interval):
    from applypilot.execution import activity_scope, ExecutionStopped
    if type(interval) not in (int, float) or not 0 < interval <= 60 or not math.isfinite(interval):
        raise ValueError('Invalid heartbeat interval')
    finished = threading.Event()
    failed = threading.Event()
    cancelled = threading.Event()
    stopping = threading.Event()

    def heartbeat():
        activity.heartbeat()
        if activity.is_cancelled():
            cancelled.set()
            stopping.set()
        if activity.is_worker_shutdown():
            stopping.set()

    def monitor():
        # This reports an executing SDK Activity, not a business polling loop.
        while not finished.wait(interval):
            try:
                heartbeat()
            except Exception:
                failed.set()
                stopping.set()
                return

    def check():
        if cancelled.is_set() or activity.is_cancelled():
            raise CancelledError('Activity cancelled')
        if failed.is_set():
            raise RuntimeError('Activity heartbeat failed')
        if stopping.is_set():
            raise ExecutionStopped('Worker shutdown requested')

    heartbeat()
    check()
    thread = threading.Thread(target=copy_context().run, args=(monitor,),
                              name='temporal-activity-heartbeat', daemon=True)
    thread.start()
    try:
        info = activity.info()
        with activity_scope(key=f'{info.workflow_id}/{info.workflow_run_id}/{info.activity_id}',
                            stage=info.activity_type, stop_event=stopping, attempt=info.attempt):
            yield check
            check()
    except ExecutionStopped as exc:
        if activity.is_cancelled():
            raise CancelledError('Activity cancelled') from exc
        raise
    finally:
        finished.set()
        thread.join()

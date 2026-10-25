"""Finite target observations with native heartbeats and unclassified retries."""
from datetime import timedelta
import math
from temporalio import activity
from temporalio.exceptions import ApplicationError
from .activity_runtime import heartbeats
from .contracts import ActivityRequest


def _execute(request, phase):
    from applypilot.database import close_connection
    from .target_domain import execute
    if request.phase != phase:
        raise ValueError('Owner-target Activity phase mismatch')
    try:
        with heartbeats(request.options['heartbeat_interval_seconds']) as check:
            return execute(request, check)
    except Exception as exc:
        delay = getattr(exc, 'retry_after', None)
        if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
            raise ApplicationError('Provider requested a later attempt',
                                   next_retry_delay=timedelta(seconds=delay)) from None
        raise
    finally:
        close_connection()


@activity.defn(name='applypilot.target_prepare.v1')
def target_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'target_prepare')


@activity.defn(name='applypilot.target_search.v1')
def target_search(request: ActivityRequest) -> dict:
    return _execute(request, 'target_search')


@activity.defn(name='applypilot.target_read.v1')
def target_read(request: ActivityRequest) -> dict:
    return _execute(request, 'target_read')


@activity.defn(name='applypilot.target_synthesize.v1')
def target_synthesize(request: ActivityRequest) -> dict:
    return _execute(request, 'target_synthesize')


@activity.defn(name='applypilot.target_vacancy.v1')
def target_vacancy(request: ActivityRequest) -> dict:
    return _execute(request, 'target_vacancy')


@activity.defn(name='applypilot.target_publish.v1')
def target_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'target_publish')


@activity.defn(name='applypilot.target_complete.v1')
def target_complete(request: ActivityRequest) -> dict:
    return _execute(request, 'target_complete')


TARGET_ACTIVITIES_BY_ROLE = {'maintenance': [target_prepare, target_publish, target_complete],
                           'research': [target_search, target_read], 'models': [target_synthesize],
                           'discovery': [target_vacancy]}

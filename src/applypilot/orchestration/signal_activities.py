"""Explicit signal Activities; failure timing belongs to the official SDK."""
import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from applypilot.signals.contracts import SignalInput, SignalResult
from .activity_runtime import heartbeats, provider_retry_delay

log = logging.getLogger(__name__)


def _perform(request, function):
    try:
        request.validate()
    except (ValueError, TypeError) as exc:
        raise ApplicationError('Invalid signal request', non_retryable=True) from exc
    failure = None
    try:
        with heartbeats(request.heartbeat_interval_seconds) as check:
            return function(request, check_cancel=check)
    except (CancelledError, ApplicationError):
        raise
    except Exception as exc:
        log.exception('Signal operation failed')
        failure = ApplicationError('Signal operation failed; inspect worker diagnostics',
                                   next_retry_delay=provider_retry_delay(exc))
    if failure is not None:
        raise failure


@activity.defn(name='applypilot.extract_signal.v1')
def extract_signal(request: SignalInput) -> SignalResult:
    from applypilot.signals.service import extract
    return _perform(request, extract)


@activity.defn(name='applypilot.publish_signal.v1')
def publish_signal(request: SignalInput) -> SignalResult:
    from applypilot.signals.service import publish
    return _perform(request, publish)

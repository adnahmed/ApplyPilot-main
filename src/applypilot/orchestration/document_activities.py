"""Two explicit semantic Activities; no phase-to-callable registry or batch runner."""
import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from applypilot.documents.contracts import (PrepareDocument, PreparedDocument,
                                           RenderDocument, DocumentResult)
from .activity_runtime import heartbeats, provider_retry_delay

log = logging.getLogger(__name__)


def _failure(exc):
    log.exception('Document operation failed')
    return ApplicationError('Document operation failed; inspect worker diagnostics',
                            next_retry_delay=provider_retry_delay(exc))


@activity.defn(name='applypilot.prepare_document.v1')
def prepare_document(request: PrepareDocument) -> PreparedDocument:
    from applypilot.documents.service import prepare
    try:
        request.validate()
    except (ValueError, TypeError) as exc:
        raise ApplicationError('Invalid document request', non_retryable=True) from exc
    failure = None
    try:
        return prepare(request)
    except (CancelledError, ApplicationError):
        raise
    except Exception as exc:
        failure = _failure(exc)
    # Do not serialize provider errors, local paths or document text into history.
    raise failure


@activity.defn(name='applypilot.render_document.v1')
def render_document(request: RenderDocument) -> DocumentResult:
    from applypilot.documents.service import render
    try:
        request.validate()
    except (ValueError, TypeError) as exc:
        raise ApplicationError('Invalid document request', non_retryable=True) from exc
    failure = None
    try:
        with heartbeats(request.heartbeat_interval_seconds) as check:
            return render(request, check_cancel=check)
    except (CancelledError, ApplicationError):
        raise
    except Exception as exc:
        failure = _failure(exc)
    raise failure


DOCUMENT_ACTIVITIES = [prepare_document, render_document]

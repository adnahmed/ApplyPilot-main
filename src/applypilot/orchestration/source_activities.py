"""One provider-page Activity. No legacy execution scope or domain dispatcher."""
from dataclasses import replace
import logging

from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError

from .activity_runtime import heartbeats
from .contracts import ActivityRequest, Observation
from . import store

log = logging.getLogger(__name__)


@activity.defn(name='applypilot.fetch_source_page.v1')
def fetch_source_page(request: ActivityRequest) -> Observation:
    from applypilot.database import get_connection, close_connection
    from .source_page import execute_page
    failure = None
    try:
        if request.phase != 'discovery_page' or type(request.sequence) is not int or request.sequence < 0:
            raise ValueError('Invalid source page input')
        interval = request.options['heartbeat_interval_seconds']
        with heartbeats(interval) as check:
            try:
                conn = get_connection()
                value, root = store.work_payload(conn, request.reference)
                if value.get('kind') != 'discover':
                    raise ValueError('Input does not describe a source page')
                if request.cursor:
                    cursor = store.get(conn, request.cursor)
                    if cursor.get('kind') != 'cursor' or not isinstance(cursor.get('value'), str):
                        raise ValueError('Invalid provider cursor reference')
                    request = replace(request, cursor=cursor['value'])
            finally:
                close_connection()

            class Cancellation:
                def is_set(self):
                    check()
                    return False

            result = execute_page(request, value, root,
                {'context': store.context_from(root), 'stop_event': Cancellation()},
                lambda *_: check())
            # Provider cursor bodies may contain access tokens: never return them
            # in Temporal history. Only the durable content-addressed reference.
            if result.next_cursor is not None:
                conn = get_connection()
                reference = store.put(conn, {'kind': 'cursor', 'value': result.next_cursor})
                conn.commit()
                result = replace(result, next_cursor=reference)
            check()
            return result
    finally:
        close_connection()

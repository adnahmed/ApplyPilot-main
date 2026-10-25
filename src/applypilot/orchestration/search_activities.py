"""Semantic provider Activities. No calls to the legacy execution adapter.

Transactions end before provider I/O. Results are immutable business evidence;
Activity retries reuse the first committed observation instead of losing it.
"""
from __future__ import annotations

from datetime import timedelta
import logging
from temporalio import activity
from temporalio.exceptions import ApplicationError

from .search_contracts import SearchActivityInput, SearchEvidence

log = logging.getLogger(__name__)


def read_request(request):
    from applypilot.database import get_connection, close_connection
    from . import search_store
    try:
        conn = get_connection()
        result = search_store.read_request(conn, request)
        conn.commit()
        return result
    finally:
        close_connection()


def save_evidence(request, hits):
    from applypilot.database import get_connection, close_connection
    from . import search_store
    try:
        conn = get_connection()
        result = search_store.save_evidence(conn, request, hits)
        conn.commit()
        return result
    finally:
        close_connection()


def perform(request: SearchActivityInput, provider: str, function) -> SearchEvidence:
    try:
        request.validate()
        if request.provider != provider:
            raise ValueError('Provider Activity does not match its input')
        search, cached = read_request(request)
    except (ValueError, LookupError) as exc:
        log.error('search_input_invalid type=%s', type(exc).__name__)
        raise ApplicationError('Invalid search input reference or provider contract',
                               type='InvalidWorkInput', non_retryable=True) from None
    if cached is not None:
        return cached
    from applypilot.integrations.types import SourceError
    import math
    try:
        hits = function(search)
    except SourceError as exc:
        delay = exc.retry_after
        if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
            raise ApplicationError('Provider requested a later attempt',
                                   next_retry_delay=timedelta(seconds=delay)) from None
        raise
    return save_evidence(request, hits)


@activity.defn(name='applypilot.search_searxng.v1')
def search_searxng(request: SearchActivityInput) -> SearchEvidence:
    from applypilot.integrations.search_providers import search_searxng as fetch
    return perform(request, 'searxng', fetch)


@activity.defn(name='applypilot.search_ddgs.v1')
def search_ddgs(request: SearchActivityInput) -> SearchEvidence:
    from applypilot.integrations.search_providers import search_ddgs as fetch
    return perform(request, 'ddgs', fetch)


SEARCH_ACTIVITIES = [search_searxng, search_ddgs]

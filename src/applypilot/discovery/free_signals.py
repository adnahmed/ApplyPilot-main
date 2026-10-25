"""Fetch one configured search/social reader page and capture its source signals.

Each reader has its own Temporal child Workflow. There is no local reader
scheduler, fallback after platform denial, cooldown clock or extraction batch.
A page-size parameter limits a provider response, never the total useful work.
"""
from __future__ import annotations

from dataclasses import replace
import time
from applypilot import postgres

from applypilot.integrations.social import SocialService, available_readers
from applypilot.integrations.types import SourceError, stable_id
from .context import _thaw
from .models import Coverage, FailureKind
from .signal_cache import (
    cache_for, checkpoint_page, decode_cursor, digest, export_page,
    reserve_attempt, signal_records, source_inputs,
)
from .transport import TaskDeferred, task_params

_SOCIAL = {'twitter': {'twikit', 'xactions', 'x_use'},
           'linkedin_posts': {'linkedin_rs', 'linkedin_mcp', 'linkedin_scraper'}}


def reader_targets(target, context):
    """Lower declarative auto-selection into independent reader request identities.

    Configuration detection does not log in, fetch posts, rotate accounts, or
    grant access. Every selected reader must still enforce its own credentials.
    """
    provider = target.provider
    if provider not in {*_SOCIAL, 'web_search'} or target.kind != 'signal':
        return (target,)
    inputs = _thaw(source_inputs(context, provider))
    params = dict(target.request_params)
    mode = params.get('mode', inputs.get('mode', 'search' if provider == 'web_search' else 'auto'))
    if mode == 'export':
        return (target,)
    if provider == 'web_search':
        backend = inputs.get('backend', 'auto') if mode == 'search' else mode
        readers = ('searxng', 'ddgs') if backend == 'auto' else (backend,)
        if any(reader not in {'searxng', 'ddgs'} for reader in readers):
            raise TaskDeferred('Select a supported web search provider')
    elif mode == 'auto':
        if inputs.get('session_file') or inputs.get('session_env'):
            raise TaskDeferred('Select an explicit reader when supplying a custom session')
        readers = tuple(available_readers(provider, inputs))
        if not readers:
            raise TaskDeferred('No authorized signal reader is configured', FailureKind.AUTH)
    else:
        if mode not in _SOCIAL[provider]:
            raise TaskDeferred('Select an authorized signal reader')
        readers = (mode,)
    return tuple(replace(target, request_params=tuple(sorted({**params, 'mode': reader}.items())))
                 for reader in readers)


def _fetch(reader, provider, query, inputs, cursor, context):
    reserve_attempt(context, {'http': 1})
    if provider == 'web_search':
        # Supported, independent adapters. No SearchService auto-combination or
        # local futures: Temporal owns the two provider lifecycles.
        from applypilot.integrations.search_providers import search_ddgs, search_searxng
        from applypilot.orchestration.search_contracts import SearchRequest
        page = int(cursor) if cursor is not None else 1
        request = SearchRequest(query + ' hiring jobs', page=page, page_size=50).validate()
        hits = search_searxng(request) if reader == 'searxng' else search_ddgs(request)
        posts = [{'id': 'web:' + stable_id(hit.url), 'url': hit.url,
                  'text': hit.title + '\n' + hit.snippet, 'raw_text': hit.title + '\n' + hit.snippet,
                  'links': [hit.url], 'deleted': False, 'posted_at': hit.published_at,
                  'updated_at': None, 'source_revision': None} for hit in hits]
        return {'posts': posts, 'next_cursor': str(page + 1) if posts else None,
                'continuation_supported': True, 'acquisition_success': True,
                'fetched_posts': len(posts)}
    service = SocialService(reader, inputs=inputs)
    return service.read_page(provider, query=query, cursor=cursor, limit=50)


def execute(task, context, cursor=None):
    provider, state, records, leads = task.target.provider, {}, [], []
    try:
        params = task_params(task, ('twitter', 'linkedin_posts', 'web_search'))
        if set(params) - {'query', 'mode', 'query_version'}:
            raise TaskDeferred('Signal request contains unsupported parameters')
        version = params.get('query_version', 1)
        if type(version) is not int or version not in (1, 2):
            raise TaskDeferred('Unsupported signal query version')
        inputs = _thaw(source_inputs(context, provider))
        mode = params.get('mode', inputs.get('mode', 'search' if provider == 'web_search' else 'auto'))
        if mode == 'export' and provider in _SOCIAL:
            return export_page(task, context, cursor)
        # Fan-out belongs to the planning Activity / child Workflows, not here.
        allowed = {'searxng', 'ddgs'} if provider == 'web_search' else _SOCIAL[provider]
        if mode not in allowed:
            raise TaskDeferred('Signal requests must select one configured reader')
        query = params.get('query')
        if not isinstance(query, str) or not query.strip():
            raise TaskDeferred('Signal query must be nonempty')
        if provider != 'web_search' and version == 1:
            query += ' hiring'
        state = decode_cursor(task, cursor)
        if state and state.get('reader_contract') != 4:
            raise TaskDeferred('Retired signal cursor: finish its old Workflow deployment before cutover',
                               FailureKind.SCHEMA)
        state = state or {'reader_contract': 4, 'reader': mode, 'page': 0,
                          'window': digest((task.task_id, task.target.request_key()))}
        if state.get('reader') != mode or type(state.get('page')) is not int or state['page'] < 0:
            raise ValueError('Invalid signal provider cursor')
        cache = cache_for(context)
        page_id = digest((mode, state['page']))
        payload = cache.window_page(state['window'], page_id)
        if payload is None:
            provider_cursor = state.get('cursor')
            token = digest(provider_cursor)
            if cache.window_cursor_seen(state['window'], mode, token):
                raise TaskDeferred('Reader revisited a committed provider cursor', FailureKind.SCHEMA)
            payload = _fetch(mode, provider, query, inputs, provider_cursor, context)
            if not isinstance(payload, dict) or not isinstance(payload.get('posts'), list):
                raise TaskDeferred('Reader returned an invalid page', FailureKind.SCHEMA)
            if payload.get('acquisition_success', True) is not True:
                raise SourceError('Reader did not acquire its requested page', 'network', payload.get('retry_after'))
            next_cursor = payload.get('next_cursor')
            if next_cursor is not None and next_cursor == provider_cursor:
                raise TaskDeferred('Reader returned the same continuation cursor', FailureKind.SCHEMA)
            payload = cache.save_window_page(state['window'], page_id, mode, token,
                {**payload, 'acquired_at': time.time(), 'reader': mode})
        for post in payload['posts']:
            found, boards = signal_records(context, cache, provider, post, hiring_thread=provider == 'web_search')
            records.extend(found)
            leads.extend(boards)
        state.update(cursor=payload.get('next_cursor'), page=state['page'] + 1)
        # No resumable cursor is a provider limitation, not complete coverage.
        resumable = payload.get('continuation_supported') is True
        finished = state['cursor'] is None or not resumable
        page = checkpoint_page(task, context, state, records, leads, exhausted=finished)
        return replace(page, capped=not resumable, complete=False, partial=True,
                       coverage=Coverage.PARTIAL if resumable else Coverage.CAPPED,
                       request_metadata={**dict(page.request_metadata), 'reader': mode,
                           'reader_contract': 4, 'acquired_at': payload.get('acquired_at'),
                           'fetched_posts': payload.get('fetched_posts', len(payload['posts'])),
                           'pagination_finished': finished, 'coverage_scope': 'search_results',
                           'upstream_exhausted': False, 'data_api_cost': 0})
    except SourceError as exc:
        error = TaskDeferred('Signal provider failed',
            FailureKind(exc.kind) if exc.kind in FailureKind._value2member_map_ else FailureKind.UNKNOWN,
            retry_after=exc.retry_after)
        return checkpoint_page(task, context, state, records, leads, error)
    except (TaskDeferred, ValueError, TypeError, KeyError, OSError, postgres.Error) as exc:
        return checkpoint_page(task, context, state, records, leads, exc)

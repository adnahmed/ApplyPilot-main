"""JSON and HTML entry points share the same target services."""
import base64

from applypilot.targets import store, imports


def company_discovery(api, route, data=None):
    from applypilot.company_discovery import store as companies
    from .data import tables
    if data is None:
        with api.store.read() as conn:
            if conn is None or 'company_sources' not in tables(conn):
                return {'settings': {}, 'sources': [], 'signals': [], 'needs_initialization': True}
            from applypilot.company_discovery.feeds import capabilities
            return {**companies.overview(conn), 'feed_capabilities': capabilities(api.app_dir)}
    execution = None
    target_ids = None
    options = None
    with api.write() as conn:
        if 'company_sources' not in tables(conn):
            raise ValueError('Initialize business schemas with applypilot temporal init-data before editing sources')
        if route == 'company-discovery/settings':
            result = companies.save_settings(conn, data)
        elif route == 'company-discovery/source':
            result = companies.add_source(conn, data)
            options = {'company_source_ids': [result['id']]}
        elif route == 'company-discovery/source-action':
            result = companies.control_source(conn, data.get('id'), data.get('action'))
            if data.get('action') != 'pause':
                options = {'company_source_ids': [result['id']]}
        elif route == 'company-discovery/watch':
            result = companies.watch(conn, data.get('id'))
            target_ids = [result['target']['id']]
        elif route == 'company-discovery/predictive-action':
            from applypilot.company_discovery import predictive_store
            result = predictive_store.control(conn, data.get('website'), data.get('action'))
            if data.get('action') != 'pause':
                options = {'company_websites': [result['website']]}
        elif route == 'company-discovery/predictive-outcome':
            from applypilot.company_discovery import predictive_store
            result = predictive_store.record_outcome(conn, data.get('website'), data.get('outcome'),
                                                     data.get('revision'), notes=data.get('notes', ''))
        else:
            raise LookupError('Unknown company discovery action')
    import asyncio
    if target_ids:
        from applypilot.orchestration.target_schedules import sync_target_schedules
        execution = asyncio.run(sync_target_schedules(target_ids, root=api.app_dir, trigger=True))
    elif options:
        from applypilot.orchestration.client import run_work
        execution = asyncio.run(run_work('company_discovery', options, root=api.app_dir))
    return {**result, **({'execution': execution} if execution else {})}


def read(api, route, query):
    with api.store.read() as conn:
        if route == 'targets':
            return store.list_targets(conn, query)
        target = store.get(conn, route.split('/')[1])
        return {**target, **store.progress(conn, target)}


def write(api, route, data):
    target_ids = []
    trigger = False
    with api.write() as conn:
        from .data import tables
        if not {'company_targets', 'company_target_observations'} <= tables(conn):
            raise ValueError('Initialize business schemas with applypilot temporal init-data before editing targets')
        if route == 'targets/import-preview':
            if data.get('parsed') is not None:
                parsed = data['parsed']
            else:
                content = (base64.b64decode(data['content_base64'], validate=True) if data.get('content_base64')
                           else str(data.get('pasted', '')).encode('utf-8'))
                parsed = imports.parse(content, str(data.get('filename') or 'paste.tsv'))
            if not data.get('confirm_mapping'):
                return {'parsed': parsed, 'mapping': data.get('mapping') or parsed['mapping'], 'defaults': data.get('defaults', {})}
            return imports.preview(conn, parsed, data.get('mapping'), data.get('defaults'))
        if route == 'targets':
            result = store.save(conn, data)
            target_ids, trigger = [result['target']['id']], not result['duplicate']
        elif route == 'targets/import-commit':
            result = imports.commit(conn, data.get('id'), data.get('selected'))
            target_ids, trigger = result['ids'], True
        else:
            parts = route.split('/')
            if len(parts) == 2:
                result = store.save(conn, data, parts[1])
                target_ids, trigger = [parts[1]], True
            elif len(parts) == 3 and parts[2] == 'action':
                result = store.control(conn, parts[1], data.get('action'), data.get('revision'))
                target_ids = [parts[1]]
                trigger = data.get('action') in {'run', 'resume'}
            else:
                raise LookupError('Unknown target action')
    import asyncio
    from applypilot.orchestration.target_schedules import sync_target_schedules
    try:
        execution = asyncio.run(sync_target_schedules(target_ids, root=api.app_dir, trigger=trigger))
    except Exception as exc:
        raise ValueError('Target change saved, but Temporal acceptance was not confirmed; '
                         'check native visibility before retrying the action') from exc
    return {**result, 'execution': execution, 'completed': False}

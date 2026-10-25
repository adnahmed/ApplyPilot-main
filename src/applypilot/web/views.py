"""HTML pages and fragments over the same services as the JSON dashboard."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import forms, dashboard_data
from .presentation import task_view
from .work_activity import activity, activity_counts
from applypilot import __version__
from applypilot.runtime_info import runtime_info
from applypilot.targets import store as targets

TEMPLATES = Environment(loader=FileSystemLoader(Path(__file__).with_name('templates')),
                        autoescape=select_autoescape(['html']))
TEMPLATES.filters['pretty'] = lambda value: json.dumps(value, indent=2, ensure_ascii=False, default=str)
TEMPLATES.filters['label'] = lambda value: str(value or '').replace('_', ' ').capitalize()
TEMPLATES.filters['work_activity'] = activity
TEMPLATES.filters['dom_id'] = lambda value: 'record-' + str(value).encode('utf-8').hex()
TEMPLATES.filters['run_time'] = lambda value: datetime.fromtimestamp(float(value), timezone.utc).astimezone().strftime('%d %b %Y, %H:%M %Z')
TEMPLATES.globals.update(urlencode=urlencode, target_actions=targets.ACTIONS)

VIEWS = {
    'overview': ('Overview', [('Needs you', 'opportunities'), ('Your search', 'summary'), ('Current work', 'work')]),
    'outcomes': ('Results', [('Strategies and practice outcomes', 'opportunities/strategies'), ('Search results', 'summary')]),
    'opportunities': ('Next actions', [('Needs you', 'opportunities')]),
    'jobs': ('Opportunities', [('Your opportunities', 'jobs')]),
    'career': ('Career workbench', [('Reports & interview practice', 'career-ops')]),
    'sources': ('Targets & sources', [('Your targets', 'targets')]),
    'outreach': ('Outreach', [('Messages and conversations', 'outreach')]),
    'workers': ('Workers', [('Current tasks', 'workers')]),
    'profile': ('Profile', []),
    'evidence': ('Evidence & impact', [('Repository evidence', 'setup')]),
    'settings': ('Settings', [('Connection status', 'setup')]),
    'activity': ('Activity', [('Recent activity', 'activity')]),
}
FORM_READS = {
    'profile': ['setup/profile'], 'searches': ['setup/searches', 'setup/runtime-policy', 'autopilot/options'],
    'evidence': ['setup/profile'], 'connections': ['setup/connections'],
    'capacity': ['setup/connections', 'queue'], 'outreach': ['settings'],
    'settings': ['settings', 'learning/status', 'learning/export-policy'], 'backups': ['backups'],
    'providers': ['providers'], 'operations': ['operations', 'work/runs'], 'diary': [], 'rlm': ['rlm/status'],
    'company-discovery': ['company-discovery'],
    'career': ['career-ops'],
}
NAV = [('overview','Overview'), ('opportunities','Needs you'), ('workers','Workers'), ('jobs','Opportunities'), ('career','Career workbench'), ('sources','Targets & sources'), ('outreach','Outreach'),
       ('outcomes','Results'), ('profile','Profile'), ('evidence','Evidence & impact'),
       ('activity','Activity'), ('settings','Settings')]
ALIASES = {'searches': ('sources', {'tab': 'preferences'}), 'diary': ('evidence', {'tab': 'impact'}),
           'setup': ('profile', {}), 'operations': ('activity', {'severity': 'error'}),
           'system': ('activity', {'tab': 'diagnostics'}),
           **{key: ('settings', {'section': section}) for key, section in
              [('connections', 'connections'), ('backups', 'backups'), ('capacity', 'automation'), ('providers', 'providers')]}}


def configuration(view, query):
    title, sections = VIEWS[view]
    form_view, active = view, ''
    if view == 'settings':
        active = query.get('section', 'connections')
        choices = {'connections': ('connections', [('Connection status', 'setup')]),
                   'backups': ('backups', [('Recovery & backups', 'backups')]),
                   'automation': ('capacity', [('Current tasks', 'workers')]),
                   'providers': ('providers', [('AI providers', 'providers')]),
                   'learning': ('settings', [('Learning', 'learning/status')]),
                   'rlm': ('rlm', [('Recursive context analysis', 'rlm/status')])}
        if active not in choices:
            active = 'connections'
        form_view, sections = choices[active]
    elif view == 'sources':
        active = query.get('tab', 'targets')
        if active == 'preferences':
            form_view, sections = 'searches', []
        elif active == 'discovery':
            sections = [('Discovery sources', 'sources')]
        elif active == 'companies':
            form_view, sections = 'company-discovery', [('Company signals', 'company-discovery')]
    elif view == 'evidence':
        active = query.get('tab', 'all')
        if active == 'imports':
            sections = [('Supporting material', 'setup')]
        else:
            active, sections = 'all', [('Contributions & results', 'setup')]
    elif view == 'activity':
        active = query.get('tab', 'all')
        if active == 'issues':
            sections = [('Recent activity', 'activity')]
        elif active == 'diagnostics':
            form_view, sections = 'operations', [('Workspace storage', 'system')]
    return {'title': title, 'sections': sections, 'form_view': form_view, 'active_section': active}


def response(template, **context):
    return HTMLResponse(TEMPLATES.get_template(template).render(**context))


async def api_call(api, request, route, data=None, query=None):
    scope = dict(request.scope)
    scope['path_params'] = {'route': route}
    scope['method'] = 'POST' if data is not None else 'GET'
    scope['query_string'] = urlencode(query if query is not None else dict(request.query_params)).encode()
    body = json.dumps(data or {}).encode()
    scope['headers'] = [(k,v) for k,v in scope['headers'] if k not in {b'content-type', b'content-length'}] + [
        (b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]
    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}
    result = await api.dispatch(Request(scope, receive))
    if not hasattr(result, 'body'):
        raise ValueError('Use the download link for this resource')
    value = json.loads(result.body)
    if result.status_code >= 400:
        raise ValueError(value.get('error', 'The request could not be completed'))
    return value


async def form_values(api, request, view, *, provider=None, job_url=None):
    keys = FORM_READS.get(view, [])
    query = {'job_url': job_url if job_url is not None else request.query_params.get('job_url', '')} if view == 'career' else {}
    if view == 'career':
        query.update(report_id=request.query_params.get('report_id', ''), state=request.query_params.get('state', ''))
    values = await asyncio.gather(*(api_call(api, request, key, query=query) for key in keys), return_exceptions=True)
    data = {key: value for key, value in zip(keys, values) if not isinstance(value, BaseException)}
    if view == 'career' and 'career-ops' in data:
        data['career-ops']['mode'] = request.query_params.get('mode', 'evaluate')
    if view == 'providers' and 'providers' in data:
        providers = data['providers'].get('providers', [])
        selected = provider or request.query_params.get('provider')
        data['providers']['selected_provider'] = next((p['id'] for p in providers if p.get('id') == selected),
                                                      providers[0]['id'] if providers else '')
    return data, [str(x) for x in values if isinstance(x, BaseException)]


def page_response(request, **context):
    development = os.environ.get('APPLYPILOT_DEV')
    development = ((Path(__file__).resolve().parents[3] / '.git').exists()
                   if development is None else development.strip().lower() in {'1', 'true', 'yes', 'on'})
    dev_build = {'version': __version__, 'revision': runtime_info()['build_hash'][:12]} if development else None
    result = response('shell.html', dev_build=dev_build, **context)
    if request.headers.get('hx-request') == 'true':
        # Navigation can originate in a panel whose requests target itself.
        result.headers.update({'HX-Retarget': '#workspace', 'HX-Reselect': '#workspace',
                               'HX-Reswap': 'outerHTML'})
    return result


async def page(api, request):
    view = request.path_params.get('view', 'overview')
    if view == 'activity' and request.query_params.get('tab') == 'issues':
        return RedirectResponse('/ui/activity?' + urlencode({**dict(request.query_params), 'tab':'all', 'severity':'error'}), status_code=303)
    if view in ALIASES:
        destination, defaults = ALIASES[view]
        return RedirectResponse('/ui/' + destination + '?' + urlencode({**dict(request.query_params), **defaults}), status_code=303)
    if view not in VIEWS:
        return HTMLResponse('Page not found', status_code=404)
    if not api.security.session(request):
        return response('login.html')
    config = configuration(view, request.query_params)
    data, errors = await form_values(api, request, config['form_view'])
    specifications = forms.build(config['form_view'], data)
    if view == 'settings' and config['active_section'] == 'learning':
        specifications = [item for item in specifications if item['id'] != 'outreach-settings']
    if view == 'evidence':
        specifications = specifications if config['active_section'] == 'imports' else forms.build('diary', {})
        if config['active_section'] != 'imports':
            for specification in specifications:
                specification['view'] = 'diary'
    if view == 'activity' and config['active_section'] == 'diagnostics':
        specifications = [item for item in specifications if item['id'] == 'support']
    facets = await run_in_threadpool(api.store.facets) if view == 'jobs' else {}
    if view == 'outcomes':
        try:
            facets['runs'] = (await api_call(api, request, 'work/runs', query={})).get('items', [])
        except ValueError as exc:
            errors.append(str(exc))
    if view == 'outreach':
        facets = await run_in_threadpool(api.store.outreach_facets)
    if view == 'activity' and config['active_section'] != 'diagnostics':
        activity = await api_call(api, request, 'activity', query={})
        facets = {'sources': activity.get('sources', [])}
    return page_response(request, view=view, nav=NAV, **config,
                    forms=specifications, values=data, facets=facets,
                    q=dict(request.query_params), errors=errors, csrf=api.security.session(request),
                    read_only=api.read_only)


async def fragment(api, request):
    view = request.path_params['view']
    index = int(request.path_params['index'])
    if view not in VIEWS:
        return HTMLResponse('Panel not found', status_code=404)
    config = configuration(view, request.query_params)
    if not 0 <= index < len(config['sections']):
        return HTMLResponse('Panel not found', status_code=404)
    title, endpoint = config['sections'][index]
    if endpoint == 'setup':
        value = await run_in_threadpool(dashboard_data.evidence, api.app_dir, request.query_params) if view == 'evidence' else await run_in_threadpool(dashboard_data.connections, api.app_dir)
    else:
        query = {**dict(request.query_params), 'feed': 'outcomes'} if endpoint == 'work' else None
        value = await api_call(api, request, endpoint, query=query)
    if endpoint == 'workers':
        tasks = await run_in_threadpool(api.setup.jobs.status) if api.setup.jobs else []
        value = await run_in_threadpool(dashboard_data.worker_rows, api.app_dir, value, tasks)
    if endpoint == 'summary' and view == 'overview':
        from .temporal_status import snapshot
        value['automation'] = snapshot(api.app_dir)
    if endpoint == 'work' and not value.get('now', {}).get('automation'):
        tasks = await run_in_threadpool(api.setup.jobs.status) if api.setup.jobs else []
        standalone = await run_in_threadpool(dashboard_data.worker_rows, api.app_dir, {}, tasks)
        now = value.setdefault('now', {})
        now['active'] = list(now.get('active') or []) + standalone['rows']
        now['active_count'] = now.get('active_count', 0) + len(standalone['rows'])
        counts = now.setdefault('activity_counts', {})
        for category, count in activity_counts(standalone['rows']).items():
            counts[category] = counts.get(category, 0) + count
    if endpoint == 'activity':
        from collections import Counter
        value['chart'] = [{'label': key, 'count': count} for key, count in
                          Counter(row.get('severity', 'info') for row in value.get('items', [])).items()]
    if endpoint == 'operations':
        value = dashboard_data.issues(value)
    if endpoint == 'sources':
        connections = await run_in_threadpool(dashboard_data.connections, api.app_dir)
        value['connection_statuses'] = [row for row in connections['connection_statuses'] if row.get('component') == 'sources']
        from collections import Counter
        for source in value.get('health', []):
            source['issues'] = [{'message': message, 'count': count} for message, count in
                                Counter(part.strip() for part in str(source.get('error') or '').split(';') if part.strip()).items()]
    if endpoint == 'providers':
        from .monitoring import charts
        value['monitoring'] = await run_in_threadpool(charts)
        value['selected_provider'] = request.query_params.get('provider') or next((row['id'] for row in value.get('providers', [])), '')
    if endpoint == 'learning/status':
        value['policies'] = await api_call(api, request, 'learning/export-policy', query={})
        from .settings_sources import learning_overview
        value['learning_overview'] = learning_overview(value, value['policies'])
        from .data import safe_json
        for row in value.get('rounds', []):
            row['report'] = safe_json(row.get('report')) if isinstance(row.get('report'), str) else row.get('report') or {}
        for row in value.get('revisions', []):
            row['evaluation'] = safe_json(row.get('evaluation')) if isinstance(row.get('evaluation'), str) else row.get('evaluation') or {}
    return response('fragment.html', view=view, form_view=config['form_view'], index=index, title=title, endpoint=endpoint, value=value,
                    q=dict(request.query_params), read_only=api.read_only)


async def profile_fields(api, request):
    values, errors = await form_values(api, request, 'profile')
    return response('profile_fields.html', forms=forms.build('profile', values), view='profile',
                    form_view='profile', read_only=api.read_only, errors=errors,
                    completion=values.get('setup/profile', {}).get('completion', {}))


async def detail(api, request):
    kind, identifier = request.path_params['kind'], request.path_params.get('identifier')
    if kind == 'worker':
        if not re.fullmatch(r'\d{1,7}', str(identifier)) or int(identifier) > 1_000_000:
            raise ValueError('Invalid worker ID')
        trace = request.query_params.get('trace', '')
        if trace and not re.fullmatch(r'[a-f0-9]{32}', trace):
            raise ValueError('Invalid session trace')
        return response('worker_log.html', worker_id=int(identifier), trace_id=trace)
    if kind == 'task':
        value = await run_in_threadpool(task_result, api.app_dir, identifier)
        return response('action_result.html', result=value, progress_url='/ui/tasks/' + identifier)
    if kind in {'evidence', 'diary'}:
        if kind == 'evidence':
            from applypilot.onboarding.evidence_edit import detail as evidence_detail
            value = await run_in_threadpool(evidence_detail, api.app_dir, request.query_params.get('id',''))
        else:
            from applypilot.backup.diary_edit import detail as diary_detail
            value = await run_in_threadpool(diary_detail, api.app_dir, request.query_params.get('kind','record'), request.query_params.get('id',''))
        return response('evidence_detail.html', kind=kind, value=value, read_only=api.read_only)
    if kind == 'job':
        route, query = 'job', {'url': request.query_params.get('url', '')}
    elif kind == 'outreach':
        route, query = 'outreach/' + identifier, {}
    elif kind == 'diary':
        route, query = 'diary/item', dict(request.query_params)
    else:
        route, query = 'work/' + identifier, {}
    value = await api_call(api, request, route, query=query)
    return response('detail.html', value=value, kind=kind, identifier=identifier, read_only=api.read_only)


async def target_page(api, request):
    identifier = request.path_params.get('identifier')
    if not api.security.session(request):
        return response('login.html')
    target = await api_call(api, request, 'targets/' + identifier, query={}) if identifier else {
        'priority': 'high', 'cadence': 'daily', 'action': '', 'state': 'active'}
    return page_response(request, view='sources', title=target.get('company') or 'Add target', nav=NAV,
                    target=target, target_editor=True, csrf=api.security.session(request), read_only=api.read_only,
                    sections=[], forms=[], errors=[], q={})


async def import_page(api, request):
    return page_response(request, view='sources', title='Import company targets', nav=NAV,
                    importing=True, csrf=api.security.session(request), read_only=api.read_only,
                    sections=[], forms=[], errors=[], q={})


def assign(data, key, value):
    parts = key.split('.')
    target = data
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value


async def form_data(request):
    if int(request.headers.get('content-length') or 0) > 15_000_000:
        raise ValueError('Upload exceeds the dashboard limit')
    return await request.form(max_files=1, max_fields=2000, max_part_size=11_000_000)


async def action(api, request):
    route = request.path_params['route']
    raw = await form_data(request)
    view = str(raw.get('_view') or 'settings')
    submitted_payload = json.loads(str(raw.get('_payload') or '{}'))
    selected_provider = str(raw.get('provider') or submitted_payload.get('provider') or '') if view == 'providers' else None
    if route.startswith('targets'):
        return await target_action(api, request, route, raw)
    if route.startswith('opportunities/'):
        data = {key: str(raw.get(key) or '') for key in
                ('job_url', 'action_id', 'requirement_id', 'text', 'note', 'introduction', 'revision') if key in raw}
        if 'effort_minutes' in raw:
            data['effort_minutes'] = float(str(raw['effort_minutes'])) if str(raw.get('effort_minutes') or '').strip() else None
        for key in ('proof_links', 'evidence_ids'):
            if key in raw:
                data[key] = [line.strip() for line in str(raw[key]).splitlines() if line.strip()]
        result = await api_call(api, request, route, data)
        api.live.mark(data=True)
        rendered = response('action_result.html', result=result, progress_url='')
        rendered.headers['HX-Trigger'] = json.dumps({'ap:data': {}})
        return rendered
    if route == 'autopilot/control':
        data = {'action': str(raw.get('action') or '')}
    elif route == 'setup/evidence/edit':
        from .evidence_forms import evidence_payload
        data = evidence_payload(raw)
    elif route == 'diary/edit' and raw.get('_editor') == 'accomplishment':
        from .evidence_forms import diary_payload
        data = await run_in_threadpool(diary_payload, api.app_dir, raw)
    elif route == 'learning/install-preview':
        data = {**submitted_payload, 'trust': raw.get('trust') == 'true', 'replace': raw.get('replace') == 'true'}
    elif route == 'logout':
        data = {}
    else:
        values, errors = await form_values(api, request, view, provider=selected_provider)
        spec = next((f for f in forms.build(view, values) if f['id'] == raw.get('_form') and f['route'] == route), None)
        if spec is None:
            # Record-specific actions are bound to an existing backend guard.
            if re.fullmatch(r'outreach/[A-Za-z0-9_-]+/cancel', route):
                data = {}
            elif route in {'setup/draft', 'setup/remove-source'}:
                data = json.loads(str(raw.get('_payload') or '{}'))
                key = 'import_source' if route == 'setup/draft' else 'id'
                if set(data) != {key} or not data.get(key):
                    raise ValueError('Choose an existing material source')
                if route == 'setup/draft':
                    data['revision'] = values.get('setup', {}).get('draft', {}).get('revision')
            elif route in {'setup/task/cancel', 'work/exports', 'diary/extract', 'diary/edit', 'diary/undo', 'diary/revise'}:
                data = json.loads(str(raw.get('_payload') or '{}'))
                for key in ('note', 'instructions'):
                    if key in raw:
                        data[key] = str(raw[key])
            elif route == 'company-discovery/predictive-action':
                data = submitted_payload
                if set(data) != {'website', 'action'}:
                    raise ValueError('Choose an existing company research action')
            elif route == 'career-ops/run':
                data = dict(submitted_payload)
                if set(data) == {'mode', 'session_id', 'session_revision', 'job_url'}:
                    data['answer'] = str(raw.get('answer') or '')
                    if str(raw.get('effort_minutes') or '').strip():
                        data['effort_minutes'] = int(str(raw['effort_minutes']))
                elif set(data) != {'mode', 'retry_report_id'}:
                    raise ValueError('Choose a saved practice session or report')
            elif route == 'company-discovery/predictive-outcome':
                data = {**submitted_payload, 'outcome': str(raw.get('outcome') or ''),
                        'notes': str(raw.get('notes') or '')}
                if set(data) != {'website', 'outcome', 'revision', 'notes'}:
                    raise ValueError('Choose an existing company research outcome')
            elif route in {'company-discovery/source-action', 'company-discovery/watch'}:
                data = submitted_payload
                allowed = {'id', 'action'} if route.endswith('source-action') else {'id'}
                if set(data) != allowed or not re.fullmatch(r'[a-f0-9]{64}', str(data.get('id', ''))):
                    raise ValueError('Choose an existing company or source')
            else:
                raise ValueError('This form changed. Reload it before submitting')
        else:
            # Use the revision the owner saw, not a freshly read revision.
            data = json.loads(str(raw.get('_payload') or '{}'))
            if set(data) - set(spec['payload']):
                raise ValueError('Unknown form parameters')
            changed = None
            if route in {'setup/save-profile', 'setup/save-searches'} and '_changed_fields' in raw:
                changed = json.loads(str(raw['_changed_fields']))
                allowed = {item['name'] for item in spec['fields']}
                compound = [item['name'] + '.' for item in spec['fields'] if item['kind'] in {'rows', 'limit'}]
                if not isinstance(changed, list) or any(not isinstance(key, str) or (key not in allowed and not any(key.startswith(prefix) for prefix in compound)) for key in changed):
                    raise ValueError('Unknown changed field')
            for item in spec['fields']:
                key, kind = item['name'], item['kind']
                if changed is not None and key not in changed and not any(name.startswith(key + '.') for name in changed):
                    continue
                value = raw.get(key, '')
                if kind in {'rows', 'limit', 'multiselect'}:
                    assign(data, key, compound_value(item, raw))
                    continue
                if kind == 'file':
                    if not getattr(value, 'filename', ''):
                        if item.get('required'):
                            raise ValueError('Choose a file')
                        continue
                    content = await value.read(11_000_001)
                    if len(content) > 11_000_000:
                        raise ValueError('File exceeds the upload limit')
                    if route == 'setup/gmail-client':
                        data['client_json'] = content.decode('utf-8-sig')
                    elif route in {'learning/import-policy', 'learning/preview-policy'}:
                        data['bundle'] = json.loads(content)
                    elif route == 'setup/import-session':
                        data.update(content_base64=base64.b64encode(content).decode())
                    else:
                        data.update(name=value.filename, content_base64=base64.b64encode(content).decode())
                    continue
                if route == 'setup/task/intake' and key == 'text' and not value:
                    continue
                if kind == 'password' and not value:
                    continue
                if value == '' and item.get('nullable') and not item.get('required'):
                    value = None
                elif kind == 'bool':
                    value = value == 'true'
                elif kind == 'percent':
                    value = float(value) / 100
                elif kind == 'number':
                    value = None if value == '' else float(value) if '.' in str(value) else int(value)
                elif kind == 'list':
                    value = [x.strip() for x in re.split(r'[,\n]', str(value)) if x.strip()]
                elif kind == 'json' or item.get('decode') == 'json':
                    value = json.loads(str(value) or 'null')
                else:
                    value = str(value)
                assign(data, key, value)
    if route in {'setup/save-profile', 'setup/save-searches'} and not data:
        result = {'message': 'No changes to save.'}
    else:
        result = await api_call(api, request, route, data)
    api.live.mark(data=True)
    if route == 'learning/preview-policy':
        return response('policy_preview.html', result=result)
    if route == 'logout':
        return HTMLResponse('', headers={'HX-Redirect': '/'})
    if route == 'autopilot/control':
        rendered = response('status.html', value=result, read_only=api.read_only)
        rendered.headers.update({'HX-Retarget': '#runtime', 'HX-Reselect': '.runtime-strip',
                                 'HX-Reswap': 'innerHTML'})
        return rendered
    progress_url = ''
    if result.get('task_id'):
        identifier = str(result['task_id'])
        result = await run_in_threadpool(task_result, api.app_dir, identifier)
        progress_url = '/ui/tasks/' + identifier
    if route in {'operations/support-bundle', 'work/exports'} and result.get('id'):
        kind = result.get('export_kind') or ('support' if route == 'operations/support-bundle' else 'work')
        progress_url = '/ui/exports/' + kind + '/' + result['id']
    if route in {'providers/check', 'providers/reload'} and result.get('id'):
        progress_url = '/ui/provider-tasks/' + route.split('/')[-1] + '/' + result['id']
    values, errors = await form_values(api, request, view, provider=selected_provider,
                                      job_url=data.get('job_url') if route == 'career-ops/run' else None)
    if route == 'career-ops/run' and 'career-ops' in values:
        values['career-ops']['mode'] = data.get('mode', 'evaluate')
    current_forms = forms.build(view, values)
    spec = next((f for f in current_forms if f['id'] == raw.get('_form')), None)
    rendered = response('form.html', form=spec, view=view, read_only=api.read_only, result=result,
                        progress_url=progress_url) if spec else response('action_result.html', result=result, progress_url=progress_url)
    # Only mounted sibling forms need fresh revisions. Some connection forms
    # are intentionally absent, so unconditional OOB swaps cannot target them.
    triggers = {'ap:data': {}}
    if route in {'setup/save-profile', 'setup/work-authorization'}:
        triggers['ap:profile-saved'] = {}
    payloads = {item['id']: item['payload'] for item in current_forms
                if item is not spec and 'revision' in item['payload']
                and 'preferences_revision' not in item['payload']}
    if payloads:
        triggers['ap:form-payloads'] = {'view': view, 'payloads': payloads}
    if route == 'setup/save-searches' and data.get('preferences_revision') and spec:
        triggers['ap:search-saved'] = {
            'before': data['preferences_revision'], 'after': spec['payload'].get('preferences_revision'),
            'changed': list((data.get('preferences') or {}).keys())}
    rendered.headers['HX-Trigger'] = json.dumps(triggers)
    return rendered


def compound_value(item, raw):
    key, kind = item['name'], item['kind']
    if kind == 'multiselect':
        return raw.getlist(key)
    if kind == 'limit':
        mode = raw.get(key + '.__mode')
        if mode == 'uncapped':
            return -1
        if mode == 'paused':
            return 0
        if mode != 'limited':
            raise ValueError('Choose uncapped, paused, or a limit')
        amount = int(raw.get(key + '.__value') or 0)
        if amount < 1:
            raise ValueError('A limit must be at least 1')
        return amount
    indices = list(dict.fromkeys(name[len(key)+1:].split('.')[0] for name in raw
                                if name.startswith(key + '.') and not name.endswith('.__present')))
    if len(indices) > 100:
        raise ValueError('Use at most 100 items')
    rows = []
    for index in indices:
        prefix = key + '.' + index + '.'
        row = {'_index': int(raw.get(prefix + '_index', -1))}
        for column in item['columns']:
            name = prefix + column['name']
            value = raw.get(name, '')
            if column['kind'] == 'multiselect':
                value = raw.getlist(name)
            elif column['kind'] in {'list','tokens'}:
                value = [part.strip() for part in re.split(r'[,\n]', value) if part.strip()]
            elif column['name'] == 'tier':
                value = int(value or 1)
            if column.get('required') and not value:
                raise ValueError('Enter ' + column['label'].lower())
            row[column['name']] = value
        rows.append(row)
    return rows


async def target_action(api, request, route, raw):
    if route == 'targets/import-preview':
        upload = raw.get('upload')
        if getattr(upload, 'filename', ''):
            content = await upload.read(11_000_001)
            if len(content) > 11_000_000:
                raise ValueError('Import exceeds 11 MB')
            data = {'filename': upload.filename, 'content_base64': base64.b64encode(content).decode()}
        else:
            data = {'pasted': str(raw.get('pasted') or '')}
        if raw.get('parsed'):
            data['parsed'] = json.loads(str(raw['parsed']))
        data['defaults'] = {key: str(raw.get(key) or '') for key in ('action','priority','cadence','instructions')}
        data['confirm_mapping'] = raw.get('confirm_mapping') == 'true'
        if data['confirm_mapping']:
            data['mapping'] = {key.removeprefix('map.'): str(value) for key,value in raw.items() if key.startswith('map.')}
        value = await api_call(api, request, route, data)
        return response('import_preview.html', value=value, fields=targets.FIELDS, defaults=data['defaults'])
    if route == 'targets/import-commit':
        data = {'id': str(raw['id']), 'selected': [int(x) for x in raw.getlist('selected')]}
    elif route.endswith('/action'):
        data = {'action': str(raw['action']), 'revision': str(raw['revision'])}
    else:
        data = {key: str(raw.get(key) or '') for key in targets.FIELDS}
        if raw.get('revision'):
            data['revision'] = str(raw['revision'])
    value = await api_call(api, request, route, data)
    api.live.mark(data=True)
    if value.get('execution'):
        execution = value['execution']
        query = {key: execution[key] for key in ('workflow_id', 'run_id') if execution.get(key)}
        return response('target_request.html', execution=execution, target=value['target'],
                        status_url='/api/temporal/workflow?' + urlencode(query))
    destination = '/ui/sources'
    if 'target' in value:
        destination = '/ui/targets/' + value['target']['id']
    return HTMLResponse('', headers={'HX-Redirect': destination})


async def status(api, request):
    from .temporal_status import snapshot
    value = snapshot(api.app_dir)['control']
    return response('status.html', value=value, read_only=api.read_only)


async def export_status(api, request):
    kind, identifier = request.path_params['kind'], request.path_params['identifier']
    if kind not in {'support', 'work'} or not re.fullmatch(r'[a-f0-9]{48}', identifier):
        return HTMLResponse('Export not found', status_code=404)
    route = ('operations/support-bundle/' if kind == 'support' else 'work/exports/') + identifier
    value = await api_call(api, request, route, query={})
    return response('action_result.html', result=value, progress_url='/ui/exports/' + kind + '/' + identifier)


def task_result(root, identifier):
    from applypilot.onboarding.storage import read_json
    if not re.fullmatch(r'[a-f0-9]{24}', identifier):
        raise LookupError('Background task not found')
    raw = read_json(root / 'setup' / 'tasks' / (identifier + '.state.json'), {}, limit=1_000_000)
    if not raw:
        raise LookupError('Background task not found')
    result = task_view(root, raw, allow_auth=True)
    if isinstance(raw.get('result'), dict):
        from applypilot.operations.runtime import diagnostic_details
        saved = raw['result']
        result['profile_changed'] = saved.get('live_files_changed') is True
        details = diagnostic_details(saved)
        if isinstance(saved.get('exception_chain'), list):
            from applypilot.operations.bundle import Redactor
            details['exception_chain'] = Redactor(root).value(saved['exception_chain'])
        if saved.get('profile_unresolved'):
            details['unresolved_profile_fields'] = saved['profile_unresolved']
        if saved.get('grounding_diagnostics'):
            details['rejected_profile_items'] = saved['grounding_diagnostics'][:100]
        if details:
            result['details'] = details
    checks = (raw.get('result') or {}).get('checks') if isinstance(raw.get('result'), dict) else None
    if isinstance(checks, list):
        result['checks'] = [{key: row.get(key) for key in ('component', 'ok', 'detail', 'error_code', 'diagnostics', 'elapsed_seconds', 'live', 'skipped')}
                            for row in checks if isinstance(row, dict)]
    return result


async def task_status(api, request):
    identifier = request.path_params['identifier']
    value = await run_in_threadpool(task_result, api.app_dir, identifier)
    return response('action_result.html', result=value, progress_url='/ui/tasks/' + identifier)


async def provider_task_status(api, request):
    kind, identifier = request.path_params['kind'], request.path_params['identifier']
    if kind not in {'check', 'reload'} or not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise LookupError('Provider operation not found')
    value = await api_call(api, request, 'providers/' + kind + '/' + identifier, query={})
    return response('action_result.html', result=value, progress_url='/ui/provider-tasks/' + kind + '/' + identifier)

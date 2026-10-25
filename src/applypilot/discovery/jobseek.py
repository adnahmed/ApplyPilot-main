"""Published Jobseek configurations executed by its pinned, isolated crawler."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from applypilot.integrations.net import public_url
from applypilot.integrations.types import SourceError, stable_id

UPSTREAM_COMMIT = 'e65fb972e4fbb349d10ed66dc9418b1d271f52ca'
BOARD_FIELDS = ('company_slug', 'board_slug', 'board_url', 'monitor_type', 'monitor_config',
                'scraper_type', 'scraper_config')


def normalize_board(value):
    if not isinstance(value, dict):
        raise ValueError('Jobseek board must be an object')  # noqa: TRY004 -- public configuration validation
    board = {key: value.get(key) or '' for key in BOARD_FIELDS}
    for key in ('company_slug', 'board_slug', 'monitor_type', 'scraper_type'):
        if not isinstance(board[key], str) or (board[key] and not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', board[key])):
            raise ValueError('Invalid Jobseek ' + key)
    if not all(board[key] for key in ('company_slug', 'board_slug', 'monitor_type')):
        raise ValueError('Jobseek board requires company, board and monitor identities')
    board['board_url'] = public_url(board['board_url'], resolve=False)
    for key in ('monitor_config', 'scraper_config'):
        raw = board[key]
        try:
            board[key] = json.loads(raw) if isinstance(raw, str) and raw else raw or {}
        except ValueError as exc:
            raise ValueError(f'{board["board_slug"]}: malformed {key}') from exc
        if not isinstance(board[key], dict) or len(json.dumps(board[key])) > 200_000:
            raise ValueError('Jobseek configuration must be a bounded JSON object')
    return board


def _runtime_paths():
    from applypilot.config import APP_DIR
    directory = APP_DIR / 'tools' / 'jobseek'
    executable = Path(os.environ.get('APPLYPILOT_JOBSEEK_PYTHON') or
                      directory / 'runtime' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python'))
    return executable, directory / 'installation.json'


def runtime_status():
    """Inspect installation metadata without spawning Python, Chromium, or network IO."""
    executable, manifest = _runtime_paths()
    status = {'installed': False, 'commit': None, 'expected_commit': UPSTREAM_COMMIT,
              'monitor_count': None, 'scraper_count': None, 'status': 'not_installed',
              'error': 'Jobseek crawler runtime is missing. Run python scripts/jobseek/install.py.'}
    if not executable.is_file() or not manifest.is_file():
        return status
    try:
        metadata = json.loads(manifest.read_text(encoding='utf-8'))
        if not isinstance(metadata, dict):
            raise TypeError('Installation metadata must be an object')
    except (OSError, ValueError, TypeError) as exc:
        return {**status, 'status': 'invalid_manifest', 'error':
                'Jobseek installation manifest is invalid (' + type(exc).__name__ + '); reinstall the pinned runtime.'}
    status['commit'] = metadata.get('commit') if isinstance(metadata.get('commit'), str) else None
    if metadata.get('commit') != UPSTREAM_COMMIT:
        return {**status, 'status': 'revision_mismatch', 'error':
                'Jobseek runtime revision differs from this adapter; rerun scripts/jobseek/install.py.'}
    for field in ('monitor_count', 'scraper_count'):
        count = metadata.get(field)
        if type(count) is int and 1 <= count <= 10_000:
            status[field] = count
    return {**status, 'installed': True, 'status': 'installed', 'error': ''}


def _runtime():
    status = runtime_status()
    if not status['installed']:
        raise SourceError(status['error'], 'unavailable' if status['status'] == 'not_installed' else 'configuration')
    return _runtime_paths()[0]


def _policy(board, context):
    """Retain native provider deny rules and tenant restrictions for imported boards."""
    from .ats_common import board_target
    provider = board['monitor_type']
    if context.runtime.get('authorized_sources', {}).get(provider) is False:
        raise SourceError('Jobseek board provider is denied by discovery policy', 'access_denied')
    if provider not in {'greenhouse', 'lever', 'ashby'} and context.runtime.get(
            'authorized_sources', {}).get('ats_registry') is False:
        raise SourceError('Imported career boards are denied by discovery policy', 'access_denied')
    if provider in {'greenhouse', 'lever', 'ashby'}:
        config = board['monitor_config']
        metadata = {'url': board['board_url']}
        if config.get('token'):
            metadata = {'token': config['token']}
        if provider == 'lever':
            region = config.get('region') or ('eu' if (urlsplit(board['board_url']).hostname or '').endswith('.lever.eu')
                                               else 'global')
            metadata['api_region'] = region
        native = board_target(provider, str(config.get('token') or ''), metadata, 'jobseek')
        allow = context.runtime.get('native_targets', {}).get(provider)
        region = dict(native.request_params).get('api_region', 'global')
        if allow is not None and not any(key in allow for key in (native.scope, region, f'{native.scope}:{region}')):
            raise SourceError('Jobseek board is outside allowed native tenants', 'access_denied')


def execute_board(board, target, context, posting=None):
    """Observe one inventory or one explicitly selected posting."""
    from applypilot.execution import browser_operation
    from applypilot.integrations.worker import minimal_env, run_json
    from .careers.base import record
    from .transport import reserve_budget
    normalized = normalize_board(board)
    _policy(normalized, context)
    executable = _runtime()
    reserve_budget(context, 'http')
    operation = 'inventory' if posting is None else 'posting'
    payload = {'board': normalized, 'operation': operation, 'commit': UPSTREAM_COMMIT}
    if posting is not None:
        if not isinstance(posting, dict) or not posting.get('inventory_id') or not posting.get('url'):
            raise ValueError('Jobseek requires one posting URL and its observed inventory identity')
        payload.update(url=public_url(posting['url'], resolve=False), item=posting.get('item'))
    with browser_operation():
        data = run_json([str(executable), '-I', str(Path(__file__).with_name('jobseek_worker.py'))],
            payload, timeout=240, env=minimal_env(), marker='APPLYPILOT_RESULT:')
    if not isinstance(data, dict) or data.get('commit') != UPSTREAM_COMMIT:
        raise SourceError('Jobseek worker returned an invalid result envelope', 'schema')
    if operation == 'inventory':
        if not isinstance(data.get('urls'), list):
            raise ValueError('Jobseek inventory is missing posting URLs')
        from applypilot.targets.store import now
        urls = list(dict.fromkeys(public_url(url, resolve=False) for url in data['urls']))
        inventory = {'urls': urls, 'scope': 'jobseek:' + normalized['board_slug'],
                     'source_url': normalized['board_url'], 'observed_at': now(),
                     'authoritative': data.get('authoritative') is True, 'partial': data.get('partial') is True,
                     'intent_fingerprint': context.intent.fingerprint()}
        inventory['id'] = stable_id((normalized, inventory))
        observation = {'url': normalized['board_url'], 'provider': 'jobseek',
                       'board_slug': normalized['board_slug'], 'authoritative': inventory['authoritative'],
                       'partial': inventory['partial'], 'inventory_count': len(urls), 'jobseek_inventory': inventory}
        discovered = [{'url': url, 'item': data.get('rich', {}).get(url), 'inventory_id': inventory['id'],
                       'authoritative': inventory['authoritative'], 'partial': inventory['partial'],
                       'inventory_count': len(urls)} for url in urls]
        return [], observation, discovered
    if not isinstance(data.get('records'), list):
        raise ValueError('Jobseek posting response is missing its records')
    records = []
    for item in data['records']:
        if not isinstance(item, dict) or not item.get('title') or not item.get('url'):
            raise SourceError('Jobseek posting lacks title or source URL', 'schema')
        url = public_url(item['url'], resolve=False)
        metadata = item.get('metadata') or {}
        application = public_url(metadata.get('application_url') or url, resolve=False)
        row = record(item, provider='company_targets', tenant=normalized['board_slug'], company=target['company'],
            url=url, title=item['title'], locations=item.get('locations'), description=item.get('description'),
            posting_id=item.get('source_identity') or url, application_url=application,
            work_mode=item.get('job_location_type'), employment_type=item.get('employment_type'),
            salary=item.get('base_salary'))
        row['source_posting_id'] = normalized['board_slug'] + ':' + str(row['source_posting_id'])
        row['posted_at'] = item.get('date_posted')
        if isinstance(item.get('base_salary'), dict):
            row['salary_period'] = item['base_salary'].get('unit') or row.get('salary_period')
        row['source_identity'] = {'provider': 'jobseek', 'tenant': normalized['board_slug'],
                                  'posting_id': item.get('source_identity') or url}
        records.append(row)
    from .matching import _role
    from .models import JobCandidate, Relevance
    postings = [
        {'url': row['url'], 'title': row['title'], 'text': (row.get('description') or '')[:6000],
         'relevant': {Relevance.RELEVANT: True, Relevance.IRRELEVANT: False}.get(
             _role(JobCandidate(title=row['title']), context.intent, ())[0])} for row in records]
    observation = {'url': normalized['board_url'], 'provider': 'jobseek',
        'board_slug': normalized['board_slug'], 'monitor_type': normalized['monitor_type'],
        'scraper_type': normalized['scraper_type'], 'config_hash': stable_id(normalized),
        'upstream_commit': UPSTREAM_COMMIT, 'authoritative': posting['authoritative'],
        'partial': posting.get('partial', False), 'inventory_count': posting['inventory_count'],
        'requests_used': data.get('requests_used'),
        'jobseek_posting': {'inventory_id': posting['inventory_id'], 'url': posting['url'], 'postings': postings}}
    return records, observation, []


def assemble_hiring_observations(observations):
    """A complete snapshot requires evidence for every URL in its observed inventory."""
    result = [dict(observation) for observation in observations]
    postings = {}
    for observation in result:
        evidence = observation.get('jobseek_posting')
        if evidence:
            postings.setdefault(evidence['inventory_id'], {})[evidence['url']] = evidence['postings']
    for observation in result:
        inventory = observation.get('jobseek_inventory')
        if not inventory or inventory.get('partial'):
            continue
        completed = postings.get(inventory['id'], {})
        if not set(inventory['urls']).issubset(completed):
            continue
        records = {posting['url']: posting for url in inventory['urls'] for posting in completed[url]}
        observation['hiring_snapshot'] = {
            'scope': inventory['scope'], 'source_url': inventory['source_url'],
            'observed_at': inventory['observed_at'], 'complete': inventory['authoritative'],
            'intent_fingerprint': inventory['intent_fingerprint'], 'total': len(records),
            'relevant': sum(row['relevant'] is True for row in records.values()), 'postings': list(records.values())}
    return result

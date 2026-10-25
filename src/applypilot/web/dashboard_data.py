"""Focused dashboard projections over saved workspace records."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from applypilot.onboarding.storage import read_json
from .presentation import connection_rows, humanize_issue, task_view
from .work_activity import activity, activity_counts


def evidence(root, query):
    from applypilot.onboarding.portfolio import portfolio
    from applypilot.onboarding.workspace import material_sources
    profile = read_json(root / 'profile.json', {}, limit=32_000_000)
    catalog = portfolio(root, profile)
    if query.get('tab') != 'imports':
        search = str(query.get('q') or '').casefold().split()
        entries = [entry for entry in catalog.get('entries', [])
                   if all(word in json.dumps(entry, ensure_ascii=False).casefold() for word in search)
                   and (query.get('kind', 'all') not in {'repository', 'note', 'impact'}
                        or entry.get('kind') == query.get('kind'))]
        try:
            page = max(1, int(query.get('page') or 1))
        except (ValueError, TypeError):
            page = 1
        size = 20
        page = min(page, max(1, (len(entries) + size - 1) // size))
        return {'catalog': {key: value for key, value in catalog.items() if key != 'entries'},
                'entries': entries[(page-1)*size:page*size], 'total': len(entries), 'page': page, 'page_size': size}
    saved = read_json(root / 'setup' / 'github.json', [], limit=32_000_000)
    repositories = {str(row.get('ref') or row.get('url')).casefold(): row for row in saved if isinstance(row, dict)}
    for item in profile.get('public_evidence', []):
        if isinstance(item, dict) and item.get('provider') == 'github':
            key = str(item.get('ref') or item.get('url')).casefold()
            if key not in repositories:
                repositories[key] = {**item, 'extraction_status': 'collected'}
    rows = list(repositories.values())
    search = str(query.get('q') or '').casefold().split()
    rows = [row for row in rows if all(word in json.dumps(row, ensure_ascii=False).casefold() for word in search)]
    rows.sort(key=lambda row: (not bool(row.get('claims')), str(row.get('title') or row.get('ref')).casefold()))
    try:
        page = max(1, int(query.get('page') or 1))
        source_page = max(1, int(query.get('source_page') or 1))
    except (ValueError, TypeError):
        page = source_page = 1
    size = 12
    page = min(page, max(1, (len(rows) + size - 1) // size))
    result = {'portfolio': {key: catalog[key] for key in ('evidence_count', 'impact_count', 'claims_count')},
              'github': rows[(page-1)*size:page*size], 'total': len(rows), 'page': page, 'page_size': size}
    if query.get('tab') == 'imports':
        sources = material_sources(root)
        source_page = min(source_page, max(1, (len(sources) + 19) // 20))
        result.update(sources=[{k: v for k, v in row.items() if k not in {'text', 'payload'}}
                               for row in sources[(source_page-1)*20:source_page*20]],
                      sources_total=len(sources), sources_page=source_page, sources_page_size=20,
                      total=0)
    return result


def connections(root):
    return {'connection_statuses': connection_rows(root)}


def worker_rows(root, value, tasks):
    """Include actual standalone setup work even while Autopilot is stopped."""
    rows = [dict(row) for row in value.get('rows') or []]
    from applypilot.backup.common import STATE, folder, operation_busy
    if (operation_busy(root)
            and not any(row.get('stage') == 'backup' and activity(row)['in_progress'] for row in rows)
            and not any(str(task.get('operation', '')).startswith('backup_') and task.get('state') in {'queued', 'running'} for task in tasks)):
        backup = read_json(folder(root) / STATE, {})
        rows.append({'key': 'backup:workspace', 'worker': 'Workspace backup', 'stage': 'backup', 'operation': 'Back up workspace', 'state': 'running',
                     'waiting_reason': backup.get('progress') or 'Saving an encrypted recovery snapshot.'})
    for task in tasks:
        if task.get('state') in {'queued', 'running'}:
            item = task_view(root, task)
            rows.append({'key': 'setup:' + str(item['id']), 'worker': item['category'], 'category': item['category'], 'operation': item['title'], 'state': item['state'],
                         'waiting_reason': item['message'], 'started_at': task.get('started_at')})
    discovery = read_json(root / 'evidence' / 'discovery-state.json', {})
    if discovery.get('state') == 'running':
        import psutil
        try:
            process = psutil.Process(int(discovery.get('pid') or 0))
            expected = discovery.get('process_started')
            alive = expected is not None and abs(process.create_time() - float(expected)) < 1
        except (psutil.Error, ValueError, TypeError):
            alive = False
        if alive:
            for repo in discovery.get('active_repositories') or []:
                rows.append({'key': 'evidence:' + str(repo), 'worker': 'Repository evidence', 'stage': 'evidence', 'target': str(repo), 'state': 'running',
                             'waiting_reason': 'Packing source files and extracting supported claims.'})
    now = datetime.now(timezone.utc).timestamp()
    for row in rows:
        row['activity'] = activity(row)
        if not row['activity']['in_progress']:
            row.pop('elapsed_seconds', None)
        elif row.get('started_at') and row.get('elapsed_seconds') is None:
            try:
                raw = row['started_at']
                started = float(raw) if isinstance(raw, (int, float)) else datetime.fromisoformat(raw.replace('Z', '+00:00')).timestamp()
                row['elapsed_seconds'] = max(0, int(now - started))
            except (ValueError, TypeError):
                pass
    history = dict(value.get('history') or {})
    completed = list(history.get('items') or [])
    for task in tasks:
        if task.get('state') not in {'queued', 'running'}:
            item = task_view(root, task)
            completed.append({'operation': item['title'], 'outcome': item['state'], 'reason': item['message'],
                              'at': task.get('finished_at') or task.get('updated_at') or task.get('created_at'),
                              'source': item.get('category', 'Setup'), 'category': item['category'], 'task_id': task.get('id'),
                              'severity': 'error' if item['state'] in {'failed','timed_out','error'} else
                                          'warning' if item['state'] in {'interrupted','cancelled'} else 'info'})
    from .data import activity_sort_key
    completed.sort(key=activity_sort_key, reverse=True)
    history['items'] = completed[:100]
    from collections import Counter
    history['counts'] = dict(Counter(item.get('outcome') or 'recorded' for item in history['items']))
    groups = {key: [] for key in ('running', 'waiting', 'unverified', 'ended')}
    for row in rows:
        groups[row['activity']['bucket']].append(row)
    return {**value, 'rows': rows, 'worker_groups': groups,
            'activity_counts': activity_counts(rows), 'history': history}


def issues(value):
    result = dict(value)
    result['incidents'] = [{**row, 'title': ('Database' if 'long postgres transaction' in str(row.get('message') or '').casefold() else str(row.get('component') or row.get('stage') or 'Workspace').replace('_', ' ').capitalize()),
                            'message': humanize_issue(row.get('message') or row.get('reason') or row.get('error_kind'), str(row.get('component') or '')),
                            'state': 'historical_issue', 'at': row.get('last_at')}
                           for row in value.get('incidents', [])]
    return result

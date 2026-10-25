"""RLM configuration and bounded, payload-free trace observations."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re

from applypilot.reasoning.settings import RLM_FIELDS as FIELDS, RLMSettings, rlm_enabled
TRACE_FIELDS = ('at', 'trace_id', 'run_id', 'rlm_id', 'task', 'event', 'elapsed_seconds',
                'calls', 'leaf', 'error_type', 'reason_code', 'phase_seconds', 'max_calls',
                'max_iterations', 'parallel_calls', 'timeout_seconds', 'model', 'rlm_call_id',
                'max_tokens_per_call', 'shared_max_calls', 'shared_round_max_calls',
                'shared_round_max_output_tokens', 'reserved_output_tokens', 'turn', 'depth')


def trace(root, trace_id):
    if not isinstance(trace_id, str) or not re.fullmatch(r'[a-f0-9]{32}', trace_id):
        raise ValueError('Invalid RLM trace ID')
    path = Path(root) / 'logs' / 'agent-traces' / (trace_id + '-rlm.jsonl')
    if path.is_symlink() or not path.is_file():
        return {'trace_id': trace_id, 'events': [], 'available': False}
    with path.open('rb') as handle:
        size = path.stat().st_size
        offset = max(0, size - 256_000)
        handle.seek(offset)
        content = handle.read(256_000).decode('utf-8', errors='replace')
        if offset:
            content = content.partition('\n')[2]
    events = []
    for line in content.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append({key: row[key] for key in TRACE_FIELDS if key in row})
    return {'trace_id': trace_id, 'available': True, 'events': events[-300:],
            'truncated': bool(offset) or len(events) > 300}


def status(root):
    from applypilot.onboarding.resolution import application_values, stamp
    values, origins = application_values(root)
    configured = {'enabled': str(values.get('APPLYPILOT_RLM_ENABLED', 'true')).strip().lower() in {'true', '1', 'yes', 'on'}}
    provenance = {'enabled': origins.get('APPLYPILOT_RLM_ENABLED', {}).get('label', 'Default')}
    for name, (key, default, _, _) in FIELDS.items():
        configured[name] = int(values.get(key, default))
        provenance[name] = origins.get(key, {}).get('label', 'Default')
    effective = {**asdict(RLMSettings.read()), 'enabled': rlm_enabled()}
    recent = {}
    folder = Path(root) / 'logs' / 'agent-traces'
    paths = []
    for path in folder.glob('*-rlm.jsonl'):
        try:
            if not path.is_symlink() and path.is_file():
                paths.append((path.stat().st_mtime, path))
        except OSError:
            continue
    for _, path in sorted(paths, reverse=True)[:30]:
        trace_id = path.name.removesuffix('-rlm.jsonl')
        if not re.fullmatch(r'[a-f0-9]{32}', trace_id):
            continue
        try:
            events = trace(root, trace_id)['events']
        except OSError:
            continue
        for event in events:
            identifier = event.get('rlm_id')
            if not identifier:
                continue
            previous = recent.setdefault(identifier, {})
            previous.update(event)
            if event.get('event') in {'rlm.root_complete', 'rlm.root_failed', 'rlm.inference_failed'}:
                previous['terminal'] = event['event'].removeprefix('rlm.')
    return {'configured': configured, 'effective': effective, 'provenance': provenance,
            'revision': stamp(root),
            'restart_required': any(configured[k] != effective[k] for k in configured),
            'recent': sorted(recent.values(), key=lambda row: row.get('at', ''), reverse=True)[:30]}


def save(root, data):
    from applypilot.onboarding.environment import save as save_environment
    if set(data) != {'revision', 'enabled', *FIELDS} or type(data['enabled']) is not bool:
        raise ValueError('Supply the displayed RLM settings and revision')
    values = {'APPLYPILOT_RLM_ENABLED': str(data['enabled']).lower()}
    for name, (key, _, low, high) in FIELDS.items():
        value = data[name]
        if type(value) is not int or value < low or (high is not None and value > high):
            raise ValueError('Invalid RLM setting: ' + name)
        values[key] = str(value)
    save_environment(Path(root), {'revision': data['revision'], 'values': values})
    return {'saved': True, 'restart_required': True,
            'message': 'RLM settings saved. Restart workers to apply them to existing processes.'}

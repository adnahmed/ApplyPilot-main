"""Bounded, read-only windows over application worker output and session traces."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re

from applypilot.apply.agent_trace import SOURCES, _preview
from applypilot.operations.runtime import sanitize

WINDOW_BYTES = 64 * 1024


def _number(value, name):
    if not re.fullmatch(r'\d{1,16}', str(value)):
        raise ValueError('Invalid ' + name)
    return int(value)


def _tool_title(name):
    return str(name).removeprefix('mcp__').replace('__', ' · ').replace('_', ' ')


def _trace_entry(record, identity):
    event = str(record.get('event') or 'event')
    kind = ('message' if event == 'assistant_text' else 'tool' if event == 'tool_requested'
            else 'result' if event in {'tool_result', 'tool_returned', 'sdk_result'} else 'info')
    if (record.get('is_error') or record.get('isError')
            or event.endswith(('_failed', '_error', '.failed', '.error'))
            or str(record.get('level', '')).upper() in {'ERROR', 'CRITICAL'}):
        kind = 'error'
    title = (_tool_title(record.get('tool')) if record.get('tool') else
             'Claude' if kind == 'message' else event.replace('_', ' ').capitalize())
    text = record.get('text') or record.get('summary') or record.get('reason') or ''
    details = {key: value for key, value in record.items()
               if key not in {'at', 'event', 'text', 'trace_id', 'source', 'tool'}}
    return {'id': identity, 'kind': kind, 'title': title, 'text': str(text),
            'at': record.get('at'), 'details': details}


def _text_entries(lines, prefix):
    entries = []
    for offset, text in lines:
        text = sanitize(text, 16000).rstrip()
        if not text.strip() or re.fullmatch(r'=+', text.strip()):
            continue
        stripped = text.lstrip()
        stamp = re.match(r'^\[(\d{4}-\d{2}-\d{2} [\d:]+)\]\s*(.*)', text)
        if stamp:
            kind, title, content = 'session', stamp[2], stamp[1]
        elif stripped.startswith('>> '):
            kind, title, content = 'tool', _tool_title(stripped[3:].split(' ', 1)[0]), stripped[3:]
        elif stripped.startswith('<< '):
            kind = 'error' if '[ERROR]' in stripped else 'result'
            title, _, content = stripped[3:].partition(': ')
            title = _tool_title(title)
        elif stripped.startswith('SDK result:'):
            kind, title, content = 'result', 'Session result', stripped.removeprefix('SDK result:').strip()
        elif text.startswith('  ') or stripped.startswith(('URL:', 'Score:')):
            kind, title, content = 'info', 'Session details', stripped
        else:
            kind, title, content = 'message', 'Claude', text
        if kind == 'message' and entries and entries[-1]['kind'] == 'message':
            entries[-1]['text'] += '\n' + content
        else:
            entries.append({'id': prefix + str(offset), 'kind': kind, 'title': title,
                            'text': content, 'at': None, 'details': {}})
    return entries


def read(root, query):
    root = Path(root).resolve()
    worker = _number(query.get('worker'), 'worker ID')
    if worker > 1_000_000:
        raise ValueError('Invalid worker ID')
    source = query.get('source') or 'worker'
    if source not in {'worker', *SOURCES}:
        raise ValueError('Invalid log source')
    trace = str(query.get('trace') or '')
    if trace and not re.fullmatch(r'[a-f0-9]{32}', trace):
        raise ValueError('Invalid session trace')
    if source != 'worker' and not trace:
        raise ValueError('A session trace is required')
    after = _number(query['after'], 'log cursor') if query.get('after') is not None else None
    before = _number(query['before'], 'log cursor') if query.get('before') is not None else None
    if after is not None and before is not None:
        raise ValueError('Choose one log cursor')
    folder = root / 'logs'
    path = folder / f'worker-{worker}.log' if source == 'worker' else folder / 'agent-traces' / f'{trace}-{source}.jsonl'
    if any(parent.is_symlink() for parent in (folder, path.parent, path)):
        raise ValueError('Redirected worker log')
    if not path.resolve().is_relative_to(folder):
        raise ValueError('Redirected worker log')
    empty = {'available': False, 'worker': worker, 'source': source, 'items': [],
             'next_after': 0, 'next_before': None, 'generation': '', 'reset': False}
    try:
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            size = stat.st_size
            generation = f'{stat.st_dev}-{stat.st_ino}'
            cursor = after if after is not None else before
            reset = bool(cursor is not None and (cursor > size or query.get('generation') != generation))
            if reset:
                after = before = None
            end = min(size, before) if before is not None else size
            start = after if after is not None else max(0, end - WINDOW_BYTES)
            stream.seek(start)
            raw = stream.read(min(WINDOW_BYTES, max(0, end - start)))
        if start and after is None:
            split = raw.find(b'\n') + 1
            start += split
            raw = raw[split:]
        # A writer may be in the middle of a UTF-8/JSON record. Read it next time.
        complete = raw.rfind(b'\n') + 1
        if complete or len(raw) < WINDOW_BYTES:
            raw = raw[:complete]
        lines, offset = [], start
        for line in raw.splitlines(keepends=True):
            lines.append((offset, line.decode('utf-8', errors='replace').rstrip('\r\n')))
            offset += len(line)
        prefix = f'{source}-{generation}-'
        items = []
        if source == 'worker':
            items = _text_entries(lines, prefix)
        else:
            for position, line in lines:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    items.append(_trace_entry(_preview(record), prefix + str(position)))
        return {**empty, 'available': True, 'items': items, 'size': size,
                'generation': generation, 'reset': reset, 'next_after': offset,
                'next_before': start or None, 'more': offset < size,
                'updated_at': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()}
    except FileNotFoundError:
        return empty

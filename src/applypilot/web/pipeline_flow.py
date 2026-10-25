"""Explain producer watermarks and downstream readiness from saved telemetry."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import math
import time

from applypilot.operations.runtime import sanitize
from applypilot.preparation_buffers import LABELS

STAGES = {
    'discover': 'Job discovery', 'enrich': 'Job research', 'score': 'Scoring',
    'tailor': 'Résumé tailoring', 'cover': 'Cover letters', 'pdf': 'Documents',
    'applications': 'Applications',
}
CONSUMERS = {'score': 'score', 'qualified': 'tailor', 'cover': 'cover',
             'documents': 'pdf', 'apply': 'applications'}
PRODUCERS = {'score': ['enrich'], 'qualified': ['score'], 'cover': ['tailor'],
             'documents': ['tailor', 'cover'], 'apply': ['pdf']}
STATES = {
    'running': 'Working', 'queued': 'Queued', 'waiting': 'Waiting for eligible work',
    'waiting_provider': 'Waiting for AI', 'waiting_capacity': 'Waiting for AI capacity',
    'buffer_paused': 'Output buffer paused', 'buffer_wait': 'Output buffer paused',
    'error': 'Needs attention', 'blocked': 'Blocked', 'disabled': 'Disabled',
    'not_selected': 'Not selected', 'stopped': 'Stopped', 'checking': 'Checking readiness',
    'starting': 'Starting', 'scheduled': 'Waiting for next check', 'paused': 'Paused',
}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _stamp(value):
    if _number(value) is not None:
        return float(value)
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def _iso(value):
    stamp = _stamp(value)
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else ''
    except (ValueError, OverflowError, OSError):
        return ''


def _downstream(buffer, consumer, current):
    if not current:
        return 'Saved observation; current downstream activity and start time are unknown.'
    if consumer.get('active_tasks', 0):
        if (consumer.get('buffer') or {}).get('paused'):
            active = consumer['active_tasks']
            tasks = 'task is' if active == 1 else 'tasks are'
            activity = ('waiting for AI' if consumer.get('waiting_ai', 0) >= active else 'still running')
            return (f'New work is paused at this stage’s output buffer. {active} previously scheduled '
                    f'{tasks} {activity}; their output slots are already reserved.')
        if consumer.get('waiting_ai', 0) >= consumer['active_tasks']:
            return 'Tasks have started but are waiting for AI. Results must return before downstream output is ready.'
        return 'Already started. More eligible items can join as capacity becomes available.'
    if consumer.get('state') in {'error', 'blocked', 'disabled', 'not_selected', 'stopped', 'paused'}:
        return consumer.get('reason') or STATES[consumer['state']]
    if (consumer.get('buffer') or {}).get('paused'):
        return 'Waiting for this stage’s own output buffer to drain to its resume watermark.'
    if consumer.get('state') == 'waiting_capacity':
        return 'Waiting for AI capacity. A fuller input buffer will not release this wait.'
    if consumer.get('queued_tasks', 0):
        return 'Queued for execution; waiting for dependencies or capacity.'
    ready = buffer.get('ready')
    if ready is None:
        return 'Eligible input has not been reported; start time is unknown.'
    if ready > 0:
        return 'Eligible input is ready; waiting for admission and an execution slot.'
    if buffer.get('prepared', 0):
        return 'Waiting for upstream work to save an eligible result. Reserved outputs are not ready yet.'
    if buffer.get('stock', 0) or buffer.get('prepared_stock', 0):
        return 'Saved items are not runnable yet. Check the retry, admission, or document blockers below.'
    return 'Waiting for upstream to produce the first eligible item; the buffer does not need to fill.'


def snapshot(root, runtime, *, report=None, scheduler=None, workers=(), now=None):
    from applypilot.live_status import read_component

    now = time.time() if now is None else now
    report = report or read_component(root, 'preparation', latest=True) or {}
    stamp = _stamp(report.get('at'))
    current = bool(runtime.get('running') is True and runtime.get('stale') is False
                   and report.get('running') is True
                   and report.get('runtime_id') == runtime.get('runtime_id')
                   and stamp is not None and -5 <= now - stamp <= 20)
    stages = (scheduler or {}).get('stages', {}) if current else report.get('stages', {})
    stages = stages or report.get('stages') or {}
    buffers = report.get('buffers') or {}
    waiting = Counter(row.get('stage') for row in workers
                      if current and row.get('fresh') and row.get('active')
                      and row.get('state') in {'waiting_provider', 'waiting_capacity'})
    result = {'current': current, 'at': _iso(stamp), 'buffers': [], 'stages': [], 'issues': [],
              'note': ('Live preparation report.' if current else
                       'Saved report only. Buffer counts and reservations below may have changed.' if stamp else
                       'No preparation report yet. Occupancy and downstream start times are unknown.')}
    for name, label in LABELS.items():
        raw = buffers.get(name) or {}
        producers = [key for key, stage in stages.items()
                     if name in (stage.get('output_buffers') or {})] or PRODUCERS[name]
        consumer = {**(stages.get(CONSUMERS[name]) or {}), 'waiting_ai': waiting[CONSUMERS[name]]}
        available = raw.get('available') is True
        row = {key: _number(raw.get(key)) if available else None for key in
               ('stock', 'held_stock', 'prepared', 'occupancy', 'ready_now', 'consumable_stock', 'prepared_stock', 'deferred', 'route_blocked')}
        row.update(name=name, label=label, mode=raw.get('mode', 'watermarks'),
                   low=_number(raw.get('low')), high=_number(raw.get('high')),
                   available=available, paused=raw.get('paused') is True,
                   producer=', '.join(STAGES.get(key, key.replace('_', ' ')) for key in producers),
                   consumer=STAGES[CONSUMERS[name]], reason=sanitize(raw.get('reason'), 500))
        row['ready'] = row['consumable_stock'] if name == 'apply' else row['ready_now']
        row['status'] = ('Observation unavailable' if not available else
                         'Automatic capacity' if row['mode'] == 'automatic' else
                         'New work paused' if row['paused'] else 'Room for more output')
        row['headroom'] = max(0, row['high'] - row['occupancy']) if row['high'] is not None and row['occupancy'] is not None else None
        row['drain'] = max(0, row['occupancy'] - row['low']) if row['paused'] and row['occupancy'] is not None and row['low'] is not None else None
        row['fill_percent'] = min(100, 100 * row['occupancy'] / row['high']) if row['high'] and row['occupancy'] is not None else None
        row['resume_percent'] = min(100, 100 * row['low'] / row['high']) if row['high'] and row['low'] is not None else None
        row['next'] = sanitize(_downstream(row, consumer, current), 700)
        row['retry_at'] = _iso(consumer.get('retry_at')) if current else ''
        blocker_labels = {'provider_admission_wait': 'waiting for provider access',
                          'automatic_recovery_pending': 'waiting for recovery'}
        row['blockers'] = [{'label': blocker_labels.get(code, str(code).replace('_', ' ')), 'count': count}
                           for code, count in (raw.get('prepared_blockers') or {}).items()
                           if _number(count) is not None and count > 0]
        result['buffers'].append(row)

    for name, label in STAGES.items():
        raw = stages.get(name) or {}
        state = raw.get('state') or 'unknown'
        row = {'name': name, 'label': label, 'state': state,
               'status': STATES.get(state, 'Not reported'), 'waiting_ai': waiting[name] if current else None,
               'reason': sanitize(raw.get('reason'), 700),
               'last_result_at': _iso(raw.get('last_task_finished_at') or raw.get('last_result_at')),
               'retry_at': _iso(raw.get('retry_at')),
               'last_result': sanitize(raw.get('last_result_reason'), 500)}
        for key in ('active_tasks', 'queued_tasks', 'ready_backlog', 'deferred_backlog',
                    'oldest_queue_seconds', 'completed_tasks', 'failed_tasks', 'runtime_seconds_median'):
            row[key] = _number(raw.get(key))
        if current:
            if (raw.get('buffer') or {}).get('available') is False:
                row['status'] = 'Buffer inventory unavailable'
            elif state not in {'error', 'blocked'} and (raw.get('buffer') or {}).get('paused'):
                row['status'] = 'Finishing tasks; new work paused' if row['active_tasks'] else 'Output buffer paused'
            elif state not in {'error', 'blocked'} and row['waiting_ai']:
                row['status'] = 'Waiting for AI' if row['waiting_ai'] >= (row['active_tasks'] or 0) else 'Working; some tasks waiting for AI'
            if state in {'error', 'blocked'} and row['reason']:
                result['issues'].append(label + ': ' + row['reason'])
        result['stages'].append(row)
    if current:
        for worker in workers:
            if (worker.get('fresh') and worker.get('active') and worker.get('last_issue')
                    and not worker['last_issue'].get('resolved')):
                issue = worker['last_issue']
                text = sanitize(issue.get('message') or issue.get('summary'), 500)
                if text:
                    result['issues'].append(STAGES.get(worker.get('stage'), 'Worker') + ': ' + text)
    result['issues'] = list(dict.fromkeys(result['issues']))[:5]
    return result

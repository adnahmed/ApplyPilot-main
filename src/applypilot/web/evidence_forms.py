"""Typed payloads for editable accomplishments without showing storage JSON."""
from __future__ import annotations

import json
import math


def evidence_payload(raw):
    data = json.loads(str(raw.get('_payload') or '{}'))
    if set(data) != {'id', 'revision'}:
        raise ValueError('Reload this evidence before saving')
    try:
        count = int(raw.get('claim_count') or 0)
    except (ValueError, TypeError):
        raise ValueError('Invalid statement count') from None
    if not 0 <= count <= 100:
        raise ValueError('Too many statements in this evidence')
    data.update(title=str(raw.get('title') or ''), scope=str(raw.get('scope') or ''),
                resume_eligible=raw.get('resume_eligible') == 'true',
                claims=[{'id': str(raw.get(f'claim-{index}-id') or ''),
                         'text': str(raw.get(f'claim-{index}-text') or ''),
                         'resume_eligible': raw.get(f'claim-{index}-eligible') == 'true'}
                        for index in range(count)])
    return data


def diary_payload(root, raw):
    from applypilot.backup.diary_edit import detail
    data = json.loads(str(raw.get('_payload') or '{}'))
    if set(data) != {'kind', 'id', 'revision'}:
        raise ValueError('Reload this accomplishment before saving')
    current = detail(root, data['kind'], data['id'])
    if data['revision'] != current['revision']:
        raise ValueError('This accomplishment changed elsewhere. Reload before saving.')
    item = current['item']
    if data['kind'] == 'note':
        item['note'] = str(raw.get('note') or '')
    else:
        item['problem'] = str(raw.get('problem') or '')
        item['intervention'] = {**(item.get('intervention') or {}), 'summary': str(raw.get('contribution') or '')}
        item['scope'] = {**(item.get('scope') or {}), 'project': str(raw.get('project') or '')}
        for index, measurement in enumerate(item.get('measurements') or []):
            if not isinstance(measurement, dict):
                continue
            for key in ('claim', 'quantity_text', 'unit', 'baseline', 'result'):
                name = f'measurement-{index}-{key}'
                if name in raw:
                    text = str(raw[name]).strip()
                    if len(text) > 2000:
                        raise ValueError('Measurement descriptions must be under 2,000 characters')
                    measurement[key] = text or ('' if key == 'claim' else None)
            for key in ('value', 'delta_percent', 'value_min', 'value_max'):
                name = f'measurement-{index}-{key}'
                if name not in raw:
                    continue
                text = str(raw[name]).strip()
                try:
                    number = float(text) if text else None
                except ValueError:
                    raise ValueError('Enter a number for measurement values') from None
                if number is not None and not math.isfinite(number):
                    raise ValueError('Measurement values must be finite numbers')
                measurement[key] = number
        for index, outcome in enumerate(item.get('outcomes') or []):
            if isinstance(outcome, dict) and f'outcome-{index}' in raw:
                text = str(raw[f'outcome-{index}']).strip()
                if not 1 <= len(text) <= 2000:
                    raise ValueError('Describe the outcome in 1–2,000 characters')
                outcome['claim'] = text
    return {**data, 'item': item, 'operation': 'edit', 'confirm': True,
            'owner_reviewed': raw.get('owner_reviewed') == 'true'}

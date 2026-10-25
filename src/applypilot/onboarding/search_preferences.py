"""Structured search edits that retain undisplayed provider and market settings."""
from copy import deepcopy
import re


def market_entries(config):
    """Match the compiler's merged market order while retaining undisplayed fields."""
    from applypilot.discovery.intent import _translate_legacy_markets, _Warnings

    rows = {}
    for raw in [*config.get('markets', []), *config.get('locations', [])]:
        normalized = _translate_legacy_markets({**config, 'markets': [], 'locations': [raw]}, _Warnings())[0]
        label = str(normalized['label'])
        if label in rows:
            continue
        base = deepcopy(raw)
        for key, value in normalized.items():
            if isinstance(value, tuple):
                value = [getattr(item, 'value', item) for item in value]
            elif key == 'label':
                value = str(value)
            if value is not None:
                base[key] = value
        for legacy in ('country', 'remote'):
            base.pop(legacy, None)
        rows[label] = base
    return list(rows.values())


def _market_label(value, kind='market identifier'):
    if not isinstance(value, str):
        raise ValueError('Enter a ' + kind)
    label = re.sub(r'[^a-z0-9_-]+', '-', value.strip().lower()).strip('-_')
    if not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', label):
        raise ValueError('A ' + kind + ' must start with a letter and use at most 64 letters, numbers, hyphens, or underscores')
    return label


def _preference_rules(existing, value):
    if not isinstance(value, list) or len(value) > 100:
        raise ValueError('Use at most 100 preferences')
    original = existing.get('preferences') or []
    predicates = {'markets', 'employer_scope', 'engagements', 'pay_basis', 'employer_regions', 'eligible_scope'}
    optional = {'employer_scope', 'pay_basis', 'eligible_scope'}
    rows, seen = [], set()
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - predicates - {'id', '_index'}:
            raise ValueError('Unknown preference field')
        try:
            index = int(entry.get('_index', -1))
        except (ValueError, TypeError):
            raise ValueError('This preference changed. Reload before saving.') from None
        if index < -1 or index >= len(original) or (index >= 0 and index in seen):
            raise ValueError('These preferences changed. Reload before saving.')
        if index >= 0:
            seen.add(index)
        row = deepcopy(original[index]) if index >= 0 else {'match': {}}
        row['id'] = _market_label(entry.get('id', row.get('id')), 'preference name')
        match = row.setdefault('match', {})
        for key in predicates & entry.keys():
            if key in optional and entry[key] in (None, '', 'any'):
                match.pop(key, None)
            else:
                match[key] = deepcopy(entry[key])
        rows.append(row)
    if len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Give each preference a different name')
    return rows


def _market_references(config, renames, removed):
    def replace(label, where):
        if label in removed:
            raise ValueError(f'Cannot remove market "{label}": it is used by {where}. Remove that reference first, or rename the market to keep its settings.')
        return renames.get(label, label)

    if 'location_labels' in config:
        config['location_labels'] = [replace(label, 'Active markets in Discovery preferences') for label in config['location_labels']]
    for preference in config.get('preferences', []):
        match = preference.get('match') or {}
        if 'markets' in match:
            match['markets'] = [replace(label, 'preference "' + preference['id'] + '"') for label in match['markets']]
    for variant in config.get('variants', []):
        if 'market' in variant:
            variant['market'] = replace(variant['market'], 'salary variant "' + variant['name'] + '"')


def update_preferences(existing, patch):
    from applypilot.discovery.intent import compile_search_intent

    allowed = {'queries', 'markets', 'exclude_titles', 'defaults', 'sources', 'exploration',
               'probes', 'location_labels', 'preference_order', 'preference_rules', 'jobspy_sites'}
    if not isinstance(patch, dict) or set(patch) - allowed:
        raise ValueError('Unknown search preference')
    result = deepcopy(existing)
    renames, removed = {}, set()
    source_change = 'sources' in patch or 'jobspy_sites' in patch
    if source_change:
        from applypilot.discovery.intent import compile_source_selection
        enabled, boards = compile_source_selection(existing)
        result['sources'] = {**result.get('sources', {}), 'enabled': list(enabled)}
        result['sources'].pop('sites', None)
        result.pop('sites', None)
        result.pop('boards', None)
        result['jobspy'] = {**result.get('jobspy', {}), 'sites': list(boards)}
    for key, value in patch.items():
        if key in {'queries', 'markets'}:
            if not isinstance(value, list) or len(value) > 100:
                raise ValueError('Use at most 100 search queries or markets')
            original = result.get('queries', []) if key == 'queries' else market_entries(existing)
            rows, seen_indices = [], set()
            fields = {'query', 'tier', 'exclude'} if key == 'queries' else {'label', 'countries', 'regions', 'cities', 'work_modes'}
            for entry in value:
                if not isinstance(entry, dict) or set(entry) - fields - {'_index'}:
                    raise ValueError('Unknown search row field')
                index = int(entry.get('_index', -1))
                if index < -1 or index >= len(original) or (index >= 0 and index in seen_indices):
                    raise ValueError('These search rows changed. Reload the page before saving.')
                if index >= 0:
                    seen_indices.add(index)
                base = deepcopy(original[index]) if 0 <= index < len(original) else {}
                if isinstance(base, str):
                    base = {'query': base}
                geography_changed = key == 'markets' and any(
                    name in entry and entry[name] != base.get(name, [])
                    for name in ('countries', 'regions', 'cities', 'work_modes'))
                base.update({k: v for k, v in entry.items() if k != '_index'})
                if key == 'markets':
                    base['label'] = _market_label(base.get('label'))
                    if geography_changed:
                        base.pop('location', None)
                        base.pop('search_country', None)
                    if index >= 0:
                        renames[original[index]['label']] = base['label']
                rows.append(base)
            result['queries' if key == 'queries' else 'locations'] = rows
            if key == 'markets':
                result.pop('markets', None)
                if len({row['label'] for row in rows}) != len(rows):
                    raise ValueError('Use a different identifier for each market')
                removed = {row['label'] for row in original} - set(renames) - {row['label'] for row in rows}
        elif key == 'preference_rules':
            result['preferences'] = _preference_rules(existing, value)
        elif key in {'defaults', 'sources', 'exploration'}:
            permitted = {'defaults': {'hours_old', 'results_per_site'}, 'sources': {'enabled'},
                         'exploration': {'discovery_share', 'application_share'}}[key]
            if not isinstance(value, dict) or set(value) - permitted:
                raise ValueError('Unknown ' + key + ' setting')
            result[key] = {**result.get(key, {}), **value}
            if key == 'sources' and 'enabled' in value and 'freehire' in result:
                result['freehire'] = {**result['freehire'], 'enabled': 'freehire' in value['enabled']}
        elif key == 'jobspy_sites':
            result['jobspy']['sites'] = value
        elif key == 'preference_order':
            continue
        else:
            result[key] = value
    if 'markets' in patch:
        _market_references(result, renames, removed)
    if 'preference_order' in patch:
        from .portfolio import strategy
        result = strategy(result, {'preference_order': patch['preference_order']})
    compile_search_intent(result)
    return result

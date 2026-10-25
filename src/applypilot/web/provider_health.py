"""Provider/credential health from bounded private gateway evidence.

Only safe projections reach the dashboard. Reading never probes providers,
clears holds, changes credentials, or treats missing evidence as success.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .runtime import atomic_json
from applypilot.operations.runtime import sanitize

_lock = threading.Lock()
_cache = None
_probe_lock = threading.Lock()
_probes = {}
MAX_BYTES = 32 * 1024 * 1024
REMEDIES = {
    'credential_auth': 'Replace or reconnect this credential; verify it belongs to the expected account.',
    'credential_access': 'Check this model’s account permissions and provider access. A generic 403 does not prove the key is invalid.',
    'region_access': 'Check the provider’s supported regions and the location of this deployment.',
    'billing_required': 'Review this account’s billing requirements or choose another eligible provider.',
    'account_access': 'Review this account’s entitlement to the requested service.',
    'client_access': 'Use a provider-supported client or choose a compatible provider.',
    'rate_limit': 'Wait for the provider reset or select another provider with available capacity.',
    'daily_quota': 'Wait for this credential’s daily reset or select another provider.',
    'invalid_output': 'Inspect model/tool compatibility; incomplete output is not a successful response.',
    'output_budget_exhausted': 'Increase the bounded output allowance for this task or choose a suitable model.',
    'request_configuration': 'Review request fields and the model’s supported API contract.',
    'capability_mismatch': 'Choose a model supporting the requested tools, files, or structured output.',
    'input_limit': 'Reduce the input or select a model with a sufficient context window.',
    'provider_outage': 'Wait for provider recovery or select another provider.',
    'timeout': 'Check provider latency and host suspension; retry only after recovery.',
    'egress_transport_error': 'Check the gateway’s network and connection to this provider.',
}


def gateway_root():
    configured = os.getenv('APPLYPILOT_GATEWAY_ROOT') or os.getenv('APPLYPILOT_LITELLM_GUIDES')
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parents[3]
    return root / 'gateway'


def _read(path, default):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return default


def _stamp(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _controls(root):
    path = root / 'runtime' / 'provider-errors' / 'controls.json'
    try:
        with path.open('rb') as stream:
            raw = stream.read(256 * 1024 + 1)
    except FileNotFoundError:
        return {'schema': 1, 'revision': 'initial', 'disabled_providers': [],
                'disabled_credentials': {}, 'preferred_providers': []}
    except OSError:
        raise ValueError('Provider selections could not be read; repair controls.json before changing them') from None
    if len(raw) > 256 * 1024:
        raise ValueError('Provider selections exceed their size limit')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('Provider selections contain invalid JSON') from None
    fields = {'schema', 'revision', 'disabled_providers', 'disabled_credentials', 'preferred_providers'}
    def valid_name(name):
        return isinstance(name, str) and 0 < len(name) <= 128 and name.strip() == name

    def valid_list(values):
        return isinstance(values, list) and len(values) <= 4096 and all(valid_name(v) for v in values)
    if (not isinstance(value, dict) or set(value) != fields or type(value.get('schema')) is not int
            or value['schema'] != 1 or not valid_name(value.get('revision'))
            or not valid_list(value.get('disabled_providers')) or not valid_list(value.get('preferred_providers'))
            or not isinstance(value.get('disabled_credentials'), dict)
            or any(not valid_name(provider) or not valid_list(slots)
                   for provider, slots in value['disabled_credentials'].items())):
        raise ValueError('Unsupported provider selection format')
    return value


def _catalog(root):
    value = _read(root / 'provider_quotas.generated.json', {})
    profiles = value.get('lane_profiles', {}) if isinstance(value, dict) else {}
    return {lane: profile for lane, profile in profiles.items() if isinstance(profile, dict)} if isinstance(profiles, dict) else {}


def _epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (ValueError, TypeError, OverflowError):
        return 0


def diagnostic_secrets(root):
    """Cover saved and loaded credential values, including custom slot names."""
    from dotenv import dotenv_values
    from applypilot.config import APP_DIR
    from .provider_credentials import env_path
    sources = [dict(os.environ)]
    paths = [APP_DIR / '.env', root / '.env']
    try:
        paths.append(env_path(root))
    except (OSError, ValueError):
        pass
    for path in dict.fromkeys(paths):
        try:
            sources.append(dotenv_values(path, interpolate=False))
        except (OSError, ValueError):
            pass
    slots = {profile.get('credential_slot') or profile.get('quota_credential') for profile in _catalog(root).values()}
    return {f'CREDENTIAL_{index}_{name}': value for index, source in enumerate(sources)
            for name, value in source.items() if isinstance(value, str) and value and
            (name in slots or re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|MASTER_KEY|COOKIE|CREDENTIAL', name, re.I))}


def _safe_error(row, *, secrets=None):
    """Keep the recorded cause while removing credential and payload material."""
    return sanitize(row.get('error_message') or row.get('exception_type') or row.get('classification')
                    or 'No error message was recorded.', 4000, secrets=secrets)


def _error_signature(row, *, secrets=None):
    """Normalize only for a one-way grouping digest; never return the prose."""
    if row.get('classification') in REMEDIES:
        return _stamp([row.get('classification'), row.get('http_status'),
                       row.get('exception_type'), _safe_error(row, secrets=secrets)])[:20]
    raw = str(row.get('error_message') or row.get('exception_type') or row.get('classification') or 'Unclassified failure')
    for marker in ('Received Model Group=', 'Error doing the fallback:', '<!DOCTYPE', '<html'):
        raw = raw.split(marker, 1)[0]
    raw = re.split(r'(?i)[\"\']?(?:messages|prompt|resume|input|content)[\"\']?\s*:', raw, maxsplit=1)[0]
    raw = html.unescape(re.sub(r'<[^>]+>', ' ', raw))
    raw = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [redacted]', raw)
    raw = re.sub(r'(?i)(api.?key|token|password|authorization|cookie)\s*[=:]\s*[^\s,;}]+', r'\1=[redacted]', raw)
    raw = re.sub(r'https?://[^\s<>\"\']+', '[provider URL]', raw)
    raw = re.sub(r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b', '[email]', raw, flags=re.I)
    raw = re.sub(r'\b(?:[a-f0-9]{24,}|[A-Za-z0-9_-]{40,})\b', '[identifier]', raw)
    raw = re.sub(r'\b\d+(?:\.\d+)?\b', '#', raw)
    normalized = ' '.join(raw.split())[:2000]
    return _stamp([row.get('classification'), row.get('http_status'), row.get('exception_type'), normalized])[:20]


def _events(root):
    folder = root / 'runtime' / 'provider-errors'
    remaining, rows, seen, malformed = MAX_BYTES, [], set(), 0
    paths = []
    try:
        for path in folder.glob('provider-*.jsonl'):
            try:
                stat = path.stat()
                paths.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                malformed += 1
    except OSError:
        malformed += 1
    truncated = sum(size for _, size, _ in paths) > MAX_BYTES
    for _, _, path in sorted(paths, key=lambda item: item[0], reverse=True):
        if remaining <= 0:
            break
        try:
            with path.open('rb') as stream:
                size = os.fstat(stream.fileno()).st_size
                take = min(remaining, size)
                stream.seek(max(0, size - take))
                if size > take:
                    stream.readline()  # Drop the partial first record.
                payload = stream.read(take)
            remaining -= take
        except OSError:
            malformed += 1
            continue
        for line in payload.splitlines():
            try:
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError('Not an event')
                eid = row.get('evidence_id')
                if not isinstance(eid, str) or not re.fullmatch(r'[a-f0-9]{32}', eid) or eid in seen:
                    continue
                if (not isinstance(row.get('provider'), str) or not isinstance(row.get('credential_slot'), str)
                        or type(row.get('schema', 1)) is not int or not 0 < _epoch(row.get('at')) <= time.time() + 300
                        or any(row.get(key) is not None and not isinstance(row[key], str)
                               for key in ('classification', 'model', 'lane', 'exception_type', 'controls_revision'))):
                    raise ValueError('Invalid event metadata')
                seen.add(eid)
                rows.append(row)
            except (ValueError, TypeError):
                malformed += 1
    return rows, truncated, malformed


def snapshot(app_dir=None):
    global _cache
    root = gateway_root()
    with _lock:
        if _cache and _cache[0] == root and time.monotonic() - _cache[1] < 10:
            return _cache[2]
        value = _snapshot(root)
        _cache = root, time.monotonic(), value
        return value


def _snapshot(root):
    controls, profiles = _controls(root), _catalog(root)
    from .provider_credentials import state as credential_state
    credentials = credential_state(root)
    rows, truncated, malformed = _events(root)
    secrets = diagnostic_secrets(root)
    providers = {}
    def credential(provider, slot):
        p = providers.setdefault(provider, {'id': provider, 'credentials': {}, 'models': set(), 'tiers': set()})
        return p['credentials'].setdefault(slot, {'slot': slot, 'models': {}, 'errors': {},
            'credential_required': not slot.startswith('public:'),
            'successes': 0, 'failures': 0, 'last_success': None, 'last_failure': None,
            'current_last_success': None, 'current_last_failure': None,
            'hold_until': 0, 'hold_reason': None, 'route_holds': [],
            '_loaded_fingerprint': None, '_current_observed_at': 0})
    for lane, p in profiles.items():
        provider, slot = p.get('provider'), p.get('credential_slot') or p.get('quota_credential')
        if not provider or not slot:
            continue
        c = credential(provider, slot)
        model = p.get('inventory_model_id', lane)
        c['models'].setdefault(model, {'id': model, 'lane': lane, 'last_success': None, 'last_failure': None,
                                     'current_last_success': None, 'current_last_failure': None, '_holds': {},
                                     'capabilities': p.get('capabilities', {}), 'access_class': p.get('access_class')})
        providers[provider]['models'].add(model)
        providers[provider]['tiers'].add(p.get('public_tier') or 'unknown')
    observed_schema = 0
    last_observed = None
    applied_revision = None
    earliest = None
    for row in sorted(rows, key=lambda r: _epoch(r.get('at'))):
        provider, slot = row.get('provider'), row.get('credential_slot')
        if not isinstance(provider, str) or not isinstance(slot, str):
            continue
        c = credential(provider, slot)
        at = row.get('at')
        if not _epoch(at):
            continue
        earliest = earliest or at
        last_observed = at
        observed_schema = max(observed_schema, int(row.get('schema') or 1))
        if 'controls_revision' in row:
            applied_revision = row['controls_revision'] or 'initial'
        outcome = row.get('outcome', 'failed')
        model = c['models'].setdefault(str(row.get('model') or 'unknown'),
                                     {'id': str(row.get('model') or 'unknown'), 'lane': row.get('lane'),
                                      'last_success': None, 'last_failure': None, 'capabilities': {},
                                      'current_last_success': None, 'current_last_failure': None, '_holds': {}})
        expected = credentials['fingerprints'].get(slot)
        observed = row.get('credential_fingerprint')
        if not isinstance(observed, str) or not re.fullmatch(r'[a-f0-9]{64}', observed):
            observed = None
        if observed:
            c['_loaded_fingerprint'] = observed
        update = credentials['updates'].get(slot, {})
        saved_at = update.get('saved_at', 0) if isinstance(update, dict) and update.get('fingerprint') == expected else 0
        if type(saved_at) not in {int, float} or not 0 <= saved_at <= time.time() + 300:
            saved_at = 0
        current = not c['credential_required'] or bool(expected and observed == expected and _epoch(at) >= saved_at)
        if current:
            c['_current_observed_at'] = _epoch(at)
        if outcome == 'held':
            until = row.get('hold_until')
            scope = row.get('scope')
            if (current and type(until) in {int, float} and time.time() < until < time.time() + 366 * 86400
                    and isinstance(scope, str)):
                reason = row.get('classification') if row.get('classification') in REMEDIES else 'other'
                if scope == 'account':
                    if until > c['hold_until']:
                        c['hold_until'], c['hold_reason'] = until, reason
                else:
                    safe_scope = scope if re.fullmatch(r'[a-z_]+(?::budget:[a-zA-Z0-9_-]+)?', scope) else 'route'
                    key = (str(row.get('lane') or model['id']), safe_scope)
                    prior = model['_holds'].get(key, {})
                    if until > prior.get('until', 0):
                        model['_holds'][key] = {'model': model['id'], 'scope': safe_scope, 'until': until, 'reason': reason}
            continue
        if outcome not in {'success', 'failed'}:
            continue
        c['successes' if outcome == 'success' else 'failures'] += 1
        model['last_' + ('success' if outcome == 'success' else 'failure')] = at
        c['last_' + ('success' if outcome == 'success' else 'failure')] = at
        if current:
            field = 'current_last_' + ('success' if outcome == 'success' else 'failure')
            model[field] = c[field] = at
        if outcome == 'success':
            continue
        classification = str(row.get('classification') or 'other')
        summary = _safe_error(row, secrets=secrets)
        signature = _error_signature(row, secrets=secrets)
        error = c['errors'].setdefault(signature, {'id': signature, 'classification': classification,
            'http_status': row.get('http_status'), 'message': summary, 'count': 0,
            'first_at': at, 'last_at': at, 'models': set(), 'evidence_id': row['evidence_id'],
            'remedy': REMEDIES.get(classification, 'Inspect this provider/model’s request contract and original local evidence.')})
        error['count'] += 1
        error['last_at'] = at
        error['models'].add(model['id'])
        error['evidence_id'] = row['evidence_id']
    now = time.time()
    result = []
    for provider, p in sorted(providers.items()):
        p['enabled'] = provider not in controls['disabled_providers']
        p['preferred'] = provider in controls['preferred_providers']
        p['model_count'] = len(p.pop('models'))
        p['tiers'] = sorted(p['tiers'])
        p['credentials'] = list(p['credentials'].values())
        for c in p['credentials']:
            c['configured'] = not c['credential_required'] or credentials['present'].get(c['slot'], False)
            c['enabled'] = p['enabled'] and c['slot'] not in controls['disabled_credentials'].get(provider, [])
            c['selected'] = c['slot'] not in controls['disabled_credentials'].get(provider, [])
            expected = credentials['fingerprints'].get(c['slot'])
            loaded = c.pop('_loaded_fingerprint')
            observed_at = c.pop('_current_observed_at')
            update = credentials['updates'].get(c['slot'], {})
            pending = (isinstance(update, dict) and update.get('fingerprint') == expected
                       and type(update.get('saved_at')) in {int, float} and observed_at < update['saved_at'])
            c['credential_state'] = ('current' if not c['credential_required'] else 'unconfigured' if not c['configured'] else
                                     'restart_required' if pending or (loaded and loaded != expected) else
                                     'current' if observed_at else 'unverified')
            c['restart_required'] = c['credential_state'] == 'restart_required'
            c['models'] = list(c['models'].values())
            attention = 0
            for model in c['models']:
                model['active_holds'] = [hold for hold in model.pop('_holds').values() if hold['until'] > now]
                c['route_holds'].extend(model['active_holds'])
                last = max(_epoch(model['current_last_success']), _epoch(model['current_last_failure']))
                model['status'] = (c['credential_state'] if c['credential_state'] != 'current' else
                                   'held' if c['hold_until'] > now or any(h['scope'] == 'route' for h in model['active_holds']) else
                                   'limited' if model['active_holds'] else
                                   'unobserved' if not last else 'stale' if now - last > 21600 else
                                   'needs_attention' if _epoch(model['current_last_failure']) > _epoch(model['current_last_success']) else 'healthy')
                attention += model['status'] in {'needs_attention', 'limited', 'held'}
            last = max(_epoch(c['current_last_success']), _epoch(c['current_last_failure']))
            c['status'] = ('paused' if not c['enabled'] else
                           c['credential_state'] if c['credential_state'] != 'current' else
                           'held' if c['hold_until'] > now else
                           'unobserved' if not last else 'stale' if now - last > 21600 else
                           'needs_attention' if attention else 'healthy' if c['current_last_success'] else 'needs_attention')
            c['errors'] = sorted(c['errors'].values(), key=lambda e: e['last_at'], reverse=True)
            for error in c['errors']:
                error['models'] = sorted(error['models'])
            c['unique_errors'] = len(c['errors'])
        p['credentials'].sort(key=lambda c: (c['status'] not in {'needs_attention', 'held'}, c['slot']))
        p['counts'] = dict(Counter(c['status'] for c in p['credentials']))
        result.append(p)
    available = sorted(p.name for p in (root / 'providers').iterdir()
                        if p.is_dir() and p.name != 'common' and any(p.glob('update_*.py'))) if (root / 'providers').is_dir() else []
    available = sorted(set(available) | set(providers))
    return {'providers': result, 'catalog': available, 'revision': controls['revision'],
            'credential_revision': credentials['revision'],
            'generated_at': datetime.now(timezone.utc).isoformat(), 'window_start': earliest,
            'last_observed': last_observed, 'history_truncated': truncated, 'unreadable_records': malformed,
            'telemetry_version': observed_schema, 'applied_revision': applied_revision,
            'gateway_directory_available': root.is_dir(), 'configured_routes': len(profiles),
            'totals': {'providers': len(result), 'credentials': sum(len(p['credentials']) for p in result),
                       'failures': sum(c['failures'] for p in result for c in p['credentials']),
                       'unique_errors': sum(c['unique_errors'] for p in result for c in p['credentials'])}}


def save_selection(data):
    global _cache
    if not isinstance(data, dict) or set(data) - {'revision', 'provider', 'credential', 'enabled', 'preferred'}:
        raise ValueError('Unknown provider selection fields')
    root = gateway_root()
    from applypilot.processes import process_guard
    with _lock, process_guard(root / 'runtime' / 'provider-errors' / 'controls.guard'):
        current = _controls(root)
        if data.get('revision') != current['revision']:
            raise ValueError('Provider selections changed. Refresh before saving.')
        profiles = _catalog(root)
        selected = [p for p in profiles.values() if p.get('provider') == data.get('provider')]
        if not selected:
            raise ValueError('Select a provider in the generated gateway catalog')
        provider, slot = data['provider'], data.get('credential')
        if 'enabled' in data and type(data['enabled']) is not bool:
            raise ValueError('Enabled must be true or false')
        if 'preferred' in data and (slot or type(data['preferred']) is not bool):
            raise ValueError('Preferred applies to a provider and must be true or false')
        if 'enabled' not in data and 'preferred' not in data:
            raise ValueError('Supply an enabled or preferred selection')
        if slot and slot not in {p.get('credential_slot') or p.get('quota_credential') for p in selected}:
            raise ValueError('Select a credential belonging to this provider')
        if 'enabled' in data:
            values = set(current['disabled_credentials'].get(provider, []) if slot else current['disabled_providers'])
            values.discard(slot or provider) if data['enabled'] else values.add(slot or provider)
            if slot:
                current['disabled_credentials'][provider] = sorted(values)
            else:
                current['disabled_providers'] = sorted(values)
        if 'preferred' in data:
            preferred = set(current['preferred_providers'])
            preferred.add(provider) if data['preferred'] else preferred.discard(provider)
            current['preferred_providers'] = sorted(preferred)
        current['revision'] = uuid.uuid4().hex
        atomic_json(root / 'runtime' / 'provider-errors' / 'controls.json', current)
        _cache = None
        return {'ok': True, 'revision': current['revision']}


def start_probe(data):
    if not isinstance(data, dict) or set(data) != {'provider', 'credential', 'model'}:
        raise ValueError('Choose a provider, credential, and model to check')
    if not all(isinstance(value, str) and value for value in data.values()):
        raise ValueError('Choose a named provider, credential, and model')
    profiles = _catalog(gateway_root())
    candidates = [(lane, p) for lane, p in profiles.items() if p.get('provider') == data['provider']
                  and (p.get('credential_slot') or p.get('quota_credential')) == data['credential']
                  and p.get('inventory_model_id') == data['model']]
    if not candidates:
        raise ValueError('The selected model and credential are not configured')
    control = _controls(gateway_root())
    if data['provider'] in control['disabled_providers'] or data['credential'] in control['disabled_credentials'].get(data['provider'], []):
        raise ValueError('Enable this provider and credential before checking it')
    with _probe_lock:
        if any(v['state'] == 'running' for v in _probes.values()):
            raise ValueError('A provider check is already running')
        for key in list(_probes):
            if time.time() - _probes[key]['started'] > 3600:
                del _probes[key]
        identifier = uuid.uuid4().hex
        _probes[identifier] = {'id': identifier, 'state': 'running', 'started': time.time(), **data}
        lane = sorted(candidates, key=lambda pair: pair[1].get('public_tier') not in {'agent-fast', 'agent-general'})[0][0]
        try:
            threading.Thread(target=_probe, args=(identifier, lane), name='provider-check', daemon=True).start()
        except Exception as exc:
            _probes[identifier].update(state='failed', ok=False,
                message=sanitize(f'{type(exc).__name__}: {exc}', 4000, secrets=diagnostic_secrets(gateway_root())))
        return dict(_probes[identifier])


def save_credential(data):
    global _cache
    from .provider_credentials import save
    value = save(gateway_root(), _catalog(gateway_root()), data)
    with _lock:
        _cache = None
    return value


def probe_status(identifier):
    with _probe_lock:
        if identifier not in _probes:
            raise LookupError('Provider check was not found; refresh health before trying again')
        return dict(_probes[identifier])


def _probe(identifier, lane):
    global _cache
    secrets = {}
    try:
        import httpx
        from dotenv import dotenv_values
        from applypilot.config import APP_DIR
        from .provider_credentials import env_path, state as credential_state
        root = gateway_root()
        secrets = diagnostic_secrets(root)
        with _probe_lock:
            selected = dict(_probes[identifier])
        expected = credential_state(root)['fingerprints'].get(selected['credential'])
        env = {**dotenv_values(APP_DIR / '.env', interpolate=False),
               **dotenv_values(env_path(root), interpolate=False), **os.environ}
        url = str(env.get('LLM_URL') or 'http://127.0.0.1:4000/v1').rstrip('/')
        token = env.get('LITELLM_MASTER_KEY') or env.get('LLM_API_KEY')
        if not token:
            raise ValueError('Gateway authentication is not configured')
        metadata = {'logical_task_id': identifier}
        if expected:
            metadata['applypilot_expected_credential_fingerprint'] = expected
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            response = client.post(url + '/chat/completions', headers={'Authorization': 'Bearer ' + token}, json={
                'model': lane, 'messages': [{'role': 'user', 'content': 'Reply with the single word OK.'}],
                'max_tokens': 256, 'stream': False, 'metadata': metadata,
                'cache': {'no-cache': True, 'no-store': True}})
        try:
            body = response.json()
        except ValueError:
            body = None
        choices = (body.get('choices') or []) if isinstance(body, dict) else []
        choices = choices if isinstance(choices, list) else []
        complete = response.is_success and any(isinstance(c, dict) and c.get('finish_reason') == 'stop'
            and isinstance(c.get('message'), dict) and not c['message'].get('refusal')
            and isinstance(c['message'].get('content'), str) and c['message']['content'].strip() for c in choices)
        verified = False
        if complete and expected:
            # The gateway journal is asynchronous. A short bounded wait can
            # confirm the exact lane/value without trusting a generic HTTP 200.
            for attempt in range(3):
                rows, _, _ = _events(root)
                verified = any(row.get('logical_task_id') == identifier and row.get('lane') == lane
                               and row.get('credential_fingerprint') == expected and row.get('outcome') == 'success'
                               for row in rows)
                if verified:
                    break
                if attempt < 2:
                    time.sleep(.1)
            verified = verified and credential_state(root)['fingerprints'].get(selected['credential']) == expected
        message = ('Completed a request using the selected model and saved credential.' if verified else
                   'The route replied, but the saved credential could not be verified from matching gateway evidence. Check the gateway update and loaded credentials.')
        diagnostic = ''
        if not complete:
            failure = (body.get('error') or body.get('detail') or body.get('message') or body) if isinstance(body, dict) else body
            from applypilot.operations.bundle import Redactor
            redacted = Redactor(APP_DIR).value(failure) if failure is not None else response.text
            if isinstance(redacted, dict) and isinstance(redacted.get('message'), str):
                redacted = redacted['message']
            diagnostic = sanitize(redacted if isinstance(redacted, str) else json.dumps(redacted, ensure_ascii=False),
                                  4000, secrets=secrets)
            message = 'HTTP ' + str(response.status_code) + ': ' + (diagnostic or 'The gateway returned no response body.')
        value = {'state': 'completed', 'ok': verified, 'route_completed': complete,
                 'credential_verified': verified, 'http_status': response.status_code, 'message': message,
                 'error': diagnostic if not complete else None}
    except Exception as exc:
        from applypilot.operations.runtime import exception_details
        value = {'state': 'failed', 'ok': False,
                 'message': sanitize(f'{type(exc).__name__}: {exc}', 4000, secrets=secrets),
                 'error_details': exception_details(exc, secrets=secrets)}
    with _probe_lock:
        if identifier in _probes:
            _probes[identifier].update(value)
    with _lock:
        _cache = None

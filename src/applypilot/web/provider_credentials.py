"""Owner-only changes to a named credential in the existing gateway env file."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import threading
import time

from dotenv import dotenv_values, set_key

_lock = threading.Lock()


def env_path(root):
    base = dotenv_values(root / '.env', interpolate=False)
    configured = os.getenv('LITELLM_ENV_FILE') or base.get('LITELLM_ENV_FILE') or '.env'
    path = Path(configured)
    path = path if path.is_absolute() else root / path
    if path.is_symlink():
        raise ValueError('Gateway credential file must be a regular file')
    return path.resolve()


def fingerprint(slot, value):
    """Private comparison with the value loaded by the gateway; never an API field."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(('applypilot-provider-credential-v1\0' + slot + '\0' + value).encode('utf-8')).hexdigest()


def _material(path):
    from applypilot.onboarding.storage import read_bytes
    raw = read_bytes(path, 2_000_000)
    values = dotenv_values(stream=io.StringIO(raw.decode('utf-8-sig')), interpolate=False)
    return raw, values


def _updates(root):
    from applypilot.onboarding.storage import read_json
    value = read_json(root / 'runtime' / 'provider-errors' / 'credential-updates.json', {})
    if not isinstance(value, dict):
        raise ValueError('Credential replacement metadata is unreadable')
    return value


def revision(path):
    return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()


def state(root):
    path = env_path(root)
    raw, values = _material(path)
    return {'revision': hashlib.sha256(raw).hexdigest(),
            'present': {key: bool(value) for key, value in values.items()},
            'fingerprints': {key: fingerprint(key, value) for key, value in values.items()},
            'updates': _updates(root)}


def save(root, profiles, data):
    if not isinstance(data, dict) or set(data) != {'provider', 'credential', 'revision', 'value'}:
        raise ValueError('Supply a provider, credential slot, revision, and replacement value')
    allowed = {p.get('credential_slot') or p.get('quota_credential') for p in profiles.values()
               if p.get('provider') == data['provider']}
    key, value = data['credential'], data['value']
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', key) or key not in allowed:
        raise ValueError('Choose a credential slot belonging to this provider')
    if not isinstance(value, str) or not 8 <= len(value) <= 4096 or any(c in value for c in '\r\n\x00'):
        raise ValueError('Supply a credential of 8–4096 characters on one line')
    with _lock:
        path = env_path(root)
        from applypilot.processes import process_guard
        from applypilot.onboarding.storage import atomic, write_json
        import tempfile
        with process_guard(path.with_name(path.name + '.provider.guard')):
            raw, values = _material(path)
            if data['revision'] != hashlib.sha256(raw).hexdigest():
                raise ValueError('Gateway credentials changed. Refresh before replacing this credential.')
            if values.get(key) == value:
                return {'ok': True, 'credential': key, 'restart_required': False,
                        'message': 'This credential value is already saved; no replacement was made.'}
            updates = _updates(root)
            with tempfile.TemporaryDirectory(prefix='.provider-credential-', dir=path.parent) as directory:
                staged = Path(directory) / '.env'
                atomic(staged, raw.decode('utf-8-sig').encode('utf-8'))
                set_key(str(staged), key, value, quote_mode='always')
                if revision(path) != data['revision']:
                    raise ValueError('Gateway credentials changed while saving. Refresh before trying again.')
                atomic(path, staged.read_bytes())
            updates[key] = {'fingerprint': fingerprint(key, value), 'saved_at': time.time()}
            write_json(root / 'runtime' / 'provider-errors' / 'credential-updates.json', updates)
        return {'ok': True, 'credential': key, 'restart_required': True,
                'message': 'Credential saved. Recreate the LiteLLM service to load it; existing requests are unchanged.'}

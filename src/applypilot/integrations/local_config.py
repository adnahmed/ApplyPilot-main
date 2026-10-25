"""Discover existing owner-managed clients and sessions, without importing secrets into Git.

Explicit settings take precedence. Discovery reads only documented/conventional
locations; it never searches arbitrary disks or converts a browser's cookies.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import sys

SESSION_KEYS = {
    'twikit': 'APPLYPILOT_TWIKIT_COOKIES',
    'linkedin_scraper': 'APPLYPILOT_LINKEDIN_SESSION',
    'linkedin_rs': 'APPLYPILOT_LINKEDIN_RS_SESSION',
}


def freehire_credentials(env=None):
    env = os.environ if env is None else env
    path = Path(env.get('FREEHIRE_CREDENTIALS_PATH') or Path.home()/'.freehire'/'creds.json').expanduser()
    row = {}
    try:
        if path.is_file() and path.stat().st_size <= 65536:
            row = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        pass
    if not isinstance(row, dict): row = {}
    token = env.get('FREEHIRE_TOKEN') or env.get('FREEHIRE_API_KEY') or row.get('token') or ''
    base = env.get('FREEHIRE_API_URL') or row.get('api_url') or 'https://freehire.me'
    return {'token': token if isinstance(token, str) else '',
            'api_url': base if isinstance(base, str) else 'https://freehire.me',
            'path': str(path), 'from_cli': bool(not env.get('FREEHIRE_TOKEN') and not env.get('FREEHIRE_API_KEY') and row.get('token'))}


def session_file(provider, root=None, env=None):
    from applypilot.config import APP_DIR
    root = Path(root or APP_DIR); env = os.environ if env is None else env
    key = SESSION_KEYS[provider]
    explicit = env.get(key)
    if explicit:
        # An invalid explicit path is an error, not permission to use another identity.
        return Path(explicit).expanduser().resolve()
    home = Path.home()
    choices = {
        'twikit': [root/'sessions/twikit/cookies.json', root/'tools/twikit/cookies.json', home/'.twikit/cookies.json'],
        'linkedin_scraper': [root/'sessions/linkedin/session.json', root/'tools/linkedin_scraper/session.json'],
        'linkedin_rs': [root/'sessions/linkedin-rs/session.json',
                        linkedin_data_directory(env)/'linkedin/session.json',
                        home/'.config/linkedin-cli/session.json'],
    }
    # Refuse ambiguity rather than choosing an arbitrary signed-in account.
    found = list(dict.fromkeys(p.resolve() for p in choices[provider] if p.is_file()))
    return found[0] if len(found) == 1 else None


def linkedin_data_directory(env=None):
    """Match linkedin-rs's platform data directory, retaining legacy discovery."""
    env = os.environ if env is None else env
    home = Path.home()
    if os.name == 'nt':
        return Path(env.get('APPDATA') or home/'AppData/Roaming')
    if sys.platform == 'darwin':
        return home/'Library/Application Support'
    return Path(env.get('XDG_DATA_HOME') or home/'.local/share')


def linkedin_binary(root=None, env=None):
    from applypilot.config import APP_DIR
    root = Path(root or APP_DIR); env = os.environ if env is None else env
    explicit = env.get('APPLYPILOT_LINKEDIN_RS_BIN')
    if explicit:
        return str(Path(shutil.which(explicit) or explicit).expanduser())
    suffix = '.exe' if os.name == 'nt' else ''
    for candidate in (root/'tools/linkedin_rs'/('li'+suffix), root/'tools/linkedin-rs'/('li'+suffix),
                      root/'bin'/('li'+suffix), Path.home()/'.cargo/bin'/('li'+suffix)):
        if candidate.is_file(): return str(candidate)
    return shutil.which('li') or shutil.which('linkedin-cli')


def discovered_values(root, env):
    values = {}; origins = {}
    cred = freehire_credentials(env)
    if cred['from_cli']:
        values['FREEHIRE_TOKEN'] = cred['token']
        origins['FREEHIRE_TOKEN'] = {'kind':'cli', 'label':'Existing FreeHire CLI login', 'path':cred['path']}
    if not env.get('FREEHIRE_API_URL'):
        values['FREEHIRE_API_URL'] = cred['api_url'].rstrip('/') + ('' if cred['api_url'].rstrip('/').endswith('/api/v1') else '/api/v1')
        origins['FREEHIRE_API_URL'] = {'kind':'cli' if cred['from_cli'] else 'default', 'label':'FreeHire CLI / API default', 'path':cred['path'] if cred['from_cli'] else None}
    for provider, key in SESSION_KEYS.items():
        if not env.get(key):
            p = session_file(provider, root, env)
            if p and p.is_file():
                values[key] = str(p); origins[key] = {'kind':'session', 'label':'Existing '+provider+' session', 'path':str(p)}
    if not env.get('APPLYPILOT_LINKEDIN_RS_BIN'):
        p = linkedin_binary(root, env)
        if p:
            values['APPLYPILOT_LINKEDIN_RS_BIN'] = p
            origins['APPLYPILOT_LINKEDIN_RS_BIN'] = {'kind':'binary','label':'Existing local linkedin-rs binary','path':p}
    return values, origins

"""One resolution path for existing Gmail credentials in UI and the pinned transport."""
from __future__ import annotations
from pathlib import Path
from .storage import digest, now, read_json, write_json


def locations(root, env):
    explicit=env.get('MCP_CONFIG_DIR')
    folder=Path(explicit).expanduser() if explicit else Path.home()/'.gmail-mcp'
    if not explicit and not (folder/'credentials.json').is_file() and (root/'gmail'/'credentials.json').is_file():
        folder=root/'gmail'
    return (Path(env.get('GMAIL_OAUTH_PATH') or folder/'gcp-oauth.keys.json').expanduser(),
            Path(env.get('GMAIL_CREDENTIALS_PATH') or folder/'credentials.json').expanduser(),folder)


def file_connection(root, keys, tokens):
    """Only an explicit successful dashboard connection selects file-first auth."""
    selected = read_json(root / "setup" / "gmail-connection.json", {})
    return (isinstance(selected, dict) and selected.get("method") == "file"
            and selected.get("client_path") == str(keys.resolve())
            and selected.get("credentials_path") == str(tokens.resolve())
            and keys.is_file() and tokens.is_file())


def credential_fingerprint(root, env):
    """Identify the selected grant without persisting secrets or short-lived access tokens."""
    keys, tokens, _ = locations(root, env)
    client = read_json(keys, {}, limit=32000)
    token = read_json(tokens, {}, limit=32000)
    client = client if isinstance(client, dict) else {}
    token = token if isinstance(token, dict) else {}
    app = client.get('installed') or client.get('web') or client
    app = app if isinstance(app, dict) else {}
    effective = {} if file_connection(root, keys, tokens) else env
    return digest({'client_path': str(keys.resolve()), 'credentials_path': str(tokens.resolve()),
                   'client_id': effective.get('CLIENT_ID') or app.get('client_id'),
                   'client_secret': effective.get('CLIENT_SECRET') or app.get('client_secret'),
                   'refresh_token': effective.get('REFRESH_TOKEN') or token.get('refresh_token')})


def record_health(root, env, *, fingerprint=None, **observation):
    write_json(root / 'setup' / 'gmail-health.json',
               {**observation, 'checked_at': now(), 'credential_fingerprint': fingerprint or credential_fingerprint(root, env)})


def status(root, env):
    keys, tokens, folder=locations(root,env)
    warnings=[]
    def load(path):
        try: return read_json(path,{},limit=32000)
        except (ValueError, OSError):
            warnings.append(path.name+': unreadable or invalid JSON');return {}
    client=load(keys); token=load(tokens)
    client=client if isinstance(client,dict) else {}
    token=token if isinstance(token,dict) else {}
    app=client.get('installed') or client.get('web') or client
    app=app if isinstance(app,dict) else {}
    managed = file_connection(root, keys, tokens)
    effective = {} if managed else env
    client_ok=bool((effective.get('CLIENT_ID') and effective.get('CLIENT_SECRET')) or (app.get('client_id') and app.get('client_secret')))
    token_ok=bool(effective.get('REFRESH_TOKEN') or token.get('refresh_token'))
    health = read_json(root / "setup" / "gmail-health.json", {})
    try:
        health_current = bool(health.get('credential_fingerprint') and
                              health['credential_fingerprint'] == credential_fingerprint(root, env))
    except (ValueError, OSError):
        health_current = False
    return {'managed_file_connection': managed, 'reconnect_required': False, 'configured':client_ok and token_ok,'client_present':client_ok,'token_present':token_ok,
            'folder':str(folder),'client_path':str(keys),'credentials_path':str(tokens),
            'mailbox':token.get('email') or token.get('emailAddress') or '',
            'verified':health_current and health.get('state') == 'connected', 'health_current': health_current,
            'checked_at':health.get('checked_at') if health_current else None, 'warnings':warnings,
            'message':'Existing Gmail credentials detected; the Gmail SDK refreshes the selected token before requests.' if client_ok and token_ok else 'Some Gmail credential material is missing; connect your existing account.'}

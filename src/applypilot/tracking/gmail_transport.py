"""Host or fixed Docker stdio transport using the existing Gmail credentials."""
from __future__ import annotations
import asyncio
from contextlib import asynccontextmanager, contextmanager
import os
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

PASS_ENV=('PATH','HOME','USERPROFILE','SYSTEMROOT','SystemRoot','WINDIR','COMSPEC','PATHEXT',
          'APPDATA','LOCALAPPDATA','TEMP','TMP','TMPDIR','NPM_CONFIG_CACHE','npm_config_cache',
          'DOCKER_CONFIG','DOCKER_CONTEXT','DOCKER_HOST','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH','NODE_EXTRA_CA_CERTS','SSL_CERT_FILE','SSL_CERT_DIR','HTTP_PROXY','HTTPS_PROXY','NO_PROXY')
GMAIL_ENV=('CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN')

class GmailTransportError(RuntimeError):
    def __init__(self, details):
        self.details=details
        super().__init__(details['error'])


def parameters(root, env):
    from applypilot.onboarding.gmail_state import locations, file_connection
    from applypilot.integrations.container_runtime import runtime, base_run, file_mount
    keys,creds,folder=locations(root,env)
    child={k:env.get(k,os.environ.get(k)) for k in PASS_ENV if env.get(k,os.environ.get(k)) is not None}
    # A successful owner-clicked Connect explicitly selects these credential files.
    # Stale process tokens remain untouched, but cannot undo that reconnection.
    if not file_connection(root, keys, creds):
        child.update({k:env[k] for k in GMAIL_ENV if env.get(k)})
    child['TELEMETRY_ENABLED']='false'
    # The pinned server opens an HTTP listener even in stdio mode. Each child
    # must get its own ephemeral port instead of colliding on port 3000.
    child['PORT']='0'
    mode=env.get('APPLYPILOT_GMAIL_RUNTIME','auto').strip().lower()
    if mode not in {'auto','local','docker'}: raise ValueError('Gmail runtime must be auto, local, or docker')
    # Explicit local preserves a user-managed Node installation; auto prefers the
    # already-built MCP over npm downloads. Credentials and sender stay the same.
    rt=runtime() if mode!='local' else None
    if rt:
        name='applypilot-gmail-'+uuid.uuid4().hex[:16]
        argv=base_run(rt,name)
        for key in GMAIL_ENV:
            if key in child: argv+=['-e',key]  # values stay out of process arguments
        if keys.is_file(): argv+=file_mount(keys,'/gmail/gcp-oauth.keys.json')
        if creds.is_file(): argv+=file_mount(creds,'/gmail/credentials.json',writable=True)
        if not keys.is_file() and not (child.get('CLIENT_ID') and child.get('CLIENT_SECRET')):
            raise ValueError('No Gmail client configured. Restore the existing client file or connect Gmail.')
        if not creds.is_file() and not child.get('REFRESH_TOKEN'):
            raise ValueError('No Gmail refresh token configured. Connect your existing account first.')
        argv+=['-e','MCP_CONFIG_DIR=/gmail','-e','PORT=0','-e','TELEMETRY_ENABLED=false',
               '--entrypoint','node',rt['image'],'/opt/gmail/entry.js']
        return argv,child,(rt['docker'],name)
    if mode=='docker': raise ValueError('The source-tools container is not running. Start it with Docker Compose.')
    npx=(shutil.which('npx.cmd') if os.name=='nt' else shutil.which('npx'))
    if not npx: raise FileNotFoundError('npx not found on PATH; start source-tools or select an installed local runtime')
    child.update(GMAIL_OAUTH_PATH=str(keys),GMAIL_CREDENTIALS_PATH=str(creds),MCP_CONFIG_DIR=str(folder))
    return [npx,'-y','@shinzolabs/gmail-mcp@1.7.4'],child,None


@contextmanager
def prepared_credentials(root, env):
    """Adapt arbitrary configured paths to the pinned MCP's fixed basenames.

    Gmail MCP 1.7.4 derives paths from MCP_CONFIG_DIR; GMAIL_*_PATH env vars
    are not overrides in its config module. Keep a private per-process staging
    directory and only propagate a legitimate refresh back to the same source.
    """
    from applypilot.onboarding.gmail_state import locations, file_connection
    from applypilot.onboarding.storage import atomic, read_bytes, setup_lock
    keys, creds, _ = locations(root, env)
    original=read_bytes(creds) if creds.is_file() else b""
    if len(original)>32000 or (keys.is_file() and keys.stat().st_size>32000):
        raise ValueError("Gmail credential files must be smaller than 32 KB")
    managed=file_connection(root,keys,creds)
    selected=dict(env)
    if managed:
        for name in GMAIL_ENV: selected.pop(name,None)
    full_env=all(selected.get(k) for k in GMAIL_ENV)
    try:
        client=json.loads(read_bytes(keys).decode("utf-8-sig")) if keys.is_file() else {}
        token=json.loads(original.decode("utf-8-sig")) if original else {}
        app=(client.get("installed") or client.get("web") or client) if isinstance(client,dict) else {}
        if not full_env and (not isinstance(app,dict) or not app.get("client_id") or not app.get("client_secret")
                             or not isinstance(token,dict) or not token.get("refresh_token")):
            raise ValueError("The configured Gmail OAuth client or refresh token is missing")
    except (UnicodeError, json.JSONDecodeError):
        raise ValueError("Gmail credential files contain invalid JSON") from None
    with tempfile.TemporaryDirectory(prefix="applypilot-gmail-") as name:
        folder=Path(name);folder.chmod(0o700)
        staged_keys=folder/"gcp-oauth.keys.json";staged_creds=folder/"credentials.json"
        if isinstance(app,dict) and app:
            atomic(staged_keys,(json.dumps({"installed":app})+"\n").encode())
        # A file is also needed when env credentials refresh; never mount a
        # nonexistent file (Docker could create a directory in its place).
        atomic(staged_creds,(json.dumps(token if isinstance(token,dict) else {})+"\n").encode())
        selected.update(MCP_CONFIG_DIR=str(folder),GMAIL_OAUTH_PATH=str(staged_keys),
                        GMAIL_CREDENTIALS_PATH=str(staged_creds))
        try:
            yield selected
        finally:
            # Do not let a late verify overwrite a concurrent OAuth reconnection.
            # Env-selected identities never overwrite unrelated file credentials.
            if original and not full_env and staged_creds.is_file() and staged_creds.stat().st_size<=32000:
                try:
                    updated=json.loads(staged_creds.read_text(encoding="utf-8"))
                    if isinstance(updated,dict) and isinstance(updated.get("refresh_token"),str) and updated["refresh_token"]:
                        with setup_lock(root):
                            if creds.is_file() and read_bytes(creds)==original:
                                merged={**token,**{k:v for k,v in updated.items() if k in
                                    {"access_token","refresh_token","expiry_date","token_type"}}}
                                if merged!=token: atomic(creds,(json.dumps(merged)+"\n").encode())
                except (OSError,ValueError):
                    pass  # Existing token remains authoritative; never turn a failed verify into success.


@asynccontextmanager
async def open_transport(root, env):
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client
    from applypilot.onboarding.diagnostics import leaves
    from applypilot.operations.runtime import sanitize
    cleanup=None
    async def remove_owned_container():
        nonlocal cleanup
        if cleanup is None:
            return
        command, name = cleanup
        cleanup = None
        def remove():
            try: subprocess.run([command,'rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            except (OSError,subprocess.TimeoutExpired): pass
        await asyncio.to_thread(remove)
    # Anonymous local temporary file; raw subprocess stderr never goes to the UI.
    with tempfile.TemporaryFile(mode='w+b') as err:
        try:
            with prepared_credentials(root,env) as effective:
                argv,child,cleanup=parameters(root,effective)
                params=StdioServerParameters(command=argv[0],args=argv[1:],env=child)
                async with stdio_client(params,errlog=err) as streams:
                    try:
                        yield streams
                    finally:
                        await remove_owned_container()
        except Exception as exc:
            err.flush();err.seek(0,2);end=err.tell();err.seek(max(0,end-16384))
            stderr=err.read(16384).decode("utf-8", errors="replace")
            details=next((e.details for e in leaves(exc) if isinstance(getattr(e,'details',None),dict)),None)
            details = details or {'component': 'gmail', 'code': type(exc).__name__,
                                  'error': sanitize(str(exc), 4000), 'stderr': sanitize(stderr, 4000),
                                  'details_redacted': True}
            raise GmailTransportError(details) from exc
        finally:
            await remove_owned_container()

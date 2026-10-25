"""Settings provenance. Read existing files; never execute shell/MCP configuration.

The gateway is an external, read-only settings scope. It is NOT copied into the
application environment. Shell variables remain stronger than application files.
"""
from __future__ import annotations
import io
import json
import re
import os
from pathlib import Path
from dotenv import dotenv_values
from .storage import digest, read_bytes

GMAIL_KEYS = {'MCP_CONFIG_DIR','GMAIL_OAUTH_PATH','GMAIL_CREDENTIALS_PATH','CLIENT_ID','CLIENT_SECRET','REFRESH_TOKEN'}
# Local SearXNG matches the bundled Compose port; never infer social credentials.
DEFAULTS = {
    "APPLYPILOT_LEARNING_MODE": "full", "APPLYPILOT_OPEN_DASHBOARD": "true",
    "APPLYPILOT_RLM_ENABLED": "true", "APPLYPILOT_DSPY_ENABLED": "true",
    "APPLYPILOT_WEB_PORT": "8765", "APPLYPILOT_START_DASHBOARD_WAIT_SECONDS": "20",
    "SEARXNG_URL":"http://127.0.0.1:8888",'APPLYPILOT_BROWSER_RUNTIME':'auto', 'APPLYPILOT_SEARCH_BACKEND':'auto',
            'APPLYPILOT_CRAWLER':'auto', 'APPLYPILOT_SOURCE_RUNTIME':'auto', 'APPLYPILOT_GMAIL_RUNTIME':'auto', 'APPLYPILOT_RESEARCH_ON_ENRICH':'1',
            'APPLYPILOT_RESEARCH_COMPANIES':'-1', 'FREEHIRE_ENABLED':'true',
            'FREEHIRE_API_URL':'https://freehire.me/api/v1', 'AUTH_SERVER_PORT':'3000'}


def project_root():
    for base in (Path.cwd(), Path(__file__).resolve().parents[3]):
        for path in (base,*base.parents):
            if (path/'pyproject.toml').is_file() and (path/'src'/'applypilot').is_dir():
                return path
    return Path.cwd()


def file_values(path):
    raw=read_bytes(path,1_000_000)
    return {k:v or '' for k,v in dotenv_values(stream=io.StringIO(raw.decode('utf-8-sig')),interpolate=False).items()}


def application_values(root):
    """Return effective values + per-key provenance, including legacy project .env."""
    from applypilot import config
    project=project_root()/'.env'
    result={}; origins={}
    for label,path in (('Project .env',project),('Workspace .env',root/'.env')):
        for key,val in file_values(path).items():
            result[key]=val;origins[key]={'kind':'file','label':label,'path':str(path)}
    loaded=getattr(config,'_LOADED_ENV_VALUES',{})
    for key,val in os.environ.items():
        # Values config.load_env injected are not shell overrides; reread their disk source.
        if key in loaded and loaded[key]==val:
            continue
        result[key]=val;origins[key]={'kind':'process','label':'Shell / service environment','path':None}
    from applypilot.integrations.local_config import discovered_values
    detected, detected_origins = discovered_values(root, result)
    for key, val in detected.items():
        if not result.get(key):
            result[key] = val; origins[key] = detected_origins[key]
    # Reuse credentials already declared for a Gmail MCP in common CLI config files.
    # Only data from the allowlisted env keys is read; command/args are never executed.
    if any(result.get(key) for key in GMAIL_KEYS):
        return result,origins
    candidates=[root/'mcp.json', root/'.mcp.json', project_root()/'.mcp.json', Path.home()/'.claude.json']
    if result.get('APPLYPILOT_MCP_CONFIG'):
        candidates.insert(0,Path(result['APPLYPILOT_MCP_CONFIG']).expanduser())
    for path in dict.fromkeys(candidates):
        try:
            blob=json.loads(read_bytes(path,1_000_000) or b'{}')
        except (ValueError,OSError):
            continue
        servers=blob.get('mcpServers',{}) if isinstance(blob,dict) else {}
        if not isinstance(servers,dict): continue
        for name,server in servers.items():
            if not isinstance(server,dict): continue
            command=' '.join(str(x) for x in [server.get('command',''),*(server.get('args',[]) if isinstance(server.get('args'),list) else [])])
            if 'gmail' not in (str(name)+' '+command).casefold(): continue
            fields=server.get('env',{})
            if not isinstance(fields,dict): continue
            detected=False
            for key,value in fields.items():
                if key not in GMAIL_KEYS or result.get(key) or not isinstance(value,str) or not value:
                    continue
                ref=re.fullmatch(r'\$\{([A-Z0-9_]+)\}',value)
                if ref: value=result.get(ref[1],'')
                if not value: continue
                if key in {'MCP_CONFIG_DIR','GMAIL_OAUTH_PATH','GMAIL_CREDENTIALS_PATH'}:
                    value=str(Path(value).expanduser())
                    if not Path(value).is_absolute(): value=str(path.parent/value)
                result[key]=value;origins[key]={'kind':'mcp','label':'Existing Gmail MCP configuration','path':str(path)};detected=True
            if detected:
                # Never combine credentials belonging to two different Gmail MCPs.
                return result,origins
    return result,origins


def gateway_values():
    base=project_root()/'docs'/'guides'
    override=os.environ.get('LITELLM_ENV_FILE')
    path=Path(override).expanduser() if override else base/'.env'
    if not path.is_absolute(): path=base/path
    return file_values(path),str(path)


def display_defaults(root, values):
    from .gmail_state import locations
    defaults=dict(DEFAULTS)
    keys,credentials,folder=locations(root,values)
    defaults.update(MCP_CONFIG_DIR=str(folder), GMAIL_OAUTH_PATH=str(keys), GMAIL_CREDENTIALS_PATH=str(credentials))
    # These paths are the existing worker lookup locations, not a claim the SDK is installed.
    for provider in ('ddgs','playwright','twikit','linkedin_scraper'):
        defaults['APPLYPILOT_'+provider.upper()+'_PYTHON']=str(root/'tools'/provider/('Scripts/python.exe' if os.name=='nt' else 'bin/python'))
    return defaults


def registry(root=None):
    from .env_keys import ENV_HINTS
    result=dict(ENV_HINTS)
    gateway,_=gateway_values()
    families={key.rsplit('_',1)[0] for key in ENV_HINTS if re.fullmatch(r'.+_KEY_\d+',key)}
    for key in gateway:
        match=re.fullmatch(r'(.+_KEY)_(\d+)',key)
        if match and match[1] in families and 1 <= int(match[2]) <= 100:
            result.setdefault(key,'Existing provider credential slot in the Docker gateway. Read-only here.')
    return result


def stamp(root):
    vals,origins=application_values(root)
    gateway,path=gateway_values()
    ENV_HINTS=registry(root)
    return digest({'application':{k:vals.get(k) for k in ENV_HINTS},
                   'origins':{k:origins.get(k) for k in ENV_HINTS},
                   'gateway':{k:gateway.get(k) for k in ENV_HINTS},'gateway_path':path})

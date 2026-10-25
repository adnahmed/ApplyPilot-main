"""Fixed Docker transport for installed SDKs. No Docker socket is mounted into services.

The long-lived source-tools container serves public reads via exec. Session-backed
reads use a bounded one-shot container from the exact same image, with only their
chosen session file/directory mounted. No model or backup keys are forwarded.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid
from .types import SourceError

_LABEL = 'org.applypilot.runtime=source-tools-v1'
_cache = (0.0, None)
_lock = threading.Lock()

# Keep installed SDK dependencies in the image, but execute this checkout's
# worker so a cached image cannot retain obsolete search/connector behavior.
_WORKER_BOOTSTRAP = """import json, sys
raw = sys.stdin.buffer.read(4_000_001)
if len(raw) > 4_000_000:
    raise ValueError('Worker input exceeds 4 MB')
request = json.loads(raw)
source = request.pop('_worker_source')
exec(compile(source, '/opt/applypilot/worker_main.py', 'exec'), {
    '__name__': '__main__', '__file__': '/opt/applypilot/worker_main.py',
    '_INITIAL_REQUEST': request,
})
"""


def runtime(refresh=False):
    global _cache
    with _lock:
        if not refresh and time.monotonic() < _cache[0]: return _cache[1]
        found = None
        docker = shutil.which('docker')
        if docker:
            try:
                r = subprocess.run([docker,'ps','--filter','label='+_LABEL,'--format','{{.ID}}'],
                    capture_output=True,text=True,timeout=4,check=False)
                ids = r.stdout.split() if r.returncode == 0 else []
                if len(ids) == 1:
                    x = subprocess.run([docker,'inspect','--format','{{.Image}}',ids[0]],
                        capture_output=True,text=True,timeout=4,check=False)
                    if x.returncode == 0 and x.stdout.strip().startswith('sha256:'):
                        found = {'docker':docker,'container':ids[0],'image':x.stdout.strip()}
            except (OSError,subprocess.TimeoutExpired): pass
        _cache = (time.monotonic()+15,found)
        return found


def selected(provider):
    mode = os.environ.get('APPLYPILOT_SOURCE_RUNTIME','auto').strip().lower()
    if mode not in {'auto','local','docker'}:
        raise SourceError('Source runtime must be auto, local, or docker')
    if mode == 'local': return False
    if mode == 'auto':
        # Explicit interpreter overrides are authoritative, even when incorrect.
        if os.environ.get('APPLYPILOT_'+provider.upper()+'_PYTHON'): return False
        try:
            from .worker import python_for
            python_for(provider)
            return False
        except SourceError: pass
    return bool(runtime()) if mode == 'auto' else True


def file_mount(path, target, *, writable=False):
    path = Path(path).expanduser().resolve()
    if not path.is_file() or ',' in str(path):
        raise SourceError('Session file is missing or its path contains a Docker mount separator', 'auth')
    return ['--mount',f'type=bind,source={path},target={target}'+('' if writable else ',readonly')]


def directory_mount(path, target):
    path = Path(path).expanduser().resolve()
    from applypilot.config import APP_DIR
    sensitive_root=(path==APP_DIR.resolve() or (path/'pyproject.toml').exists() or (path/'.env').exists())
    if sensitive_root or not path.is_dir() or ',' in str(path) or path == Path(path.anchor) or path == Path.home().resolve():
        raise SourceError('Use a dedicated session directory, not a filesystem or home root', 'configuration')
    return ['--mount',f'type=bind,source={path},target={target}']


def base_run(rt, name, *, timeout=100):
    return [rt['docker'],'run','--rm','-i','--init','--name',name,'--label','org.applypilot.runtime=one-shot-v1','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','--pids-limit','256','--memory','2g','--shm-size','512m']


def call(provider, operation, arguments, *, timeout=None):
    from .worker import run_json
    rt = runtime()
    if not rt: raise SourceError('Local source-tools container is not running. Start searxng and source-tools with the provided Compose command.')
    args = dict(arguments); name = None
    binary = '/opt/tools/'+provider+'/bin/python'
    if provider not in {'ddgs','playwright','twikit','linkedin_scraper'}:
        raise SourceError('Unknown container SDK')
    inner = [binary,'-c',_WORKER_BOOTSTRAP]
    if timeout is not None:
        inner = ['timeout','--signal=TERM','--kill-after=5s',str(max(1,int(timeout)-2))+'s',*inner]
    if args.get('session_file') or timeout is None:
        # An uncapped worker needs an owned container: cancelling docker exec
        # alone cannot stop its remote process in the shared service.
        name = 'applypilot-reader-'+uuid.uuid4().hex[:16]
        argv = base_run(rt,name)
        if args.get('session_file'):
            session = Path(args['session_file']).resolve()
            argv += file_mount(session,'/session.json')
            args['session_file'] = '/session.json'
        if os.name != 'nt': argv += ['--user',f'{os.getuid()}:{os.getgid()}','-e','HOME=/tmp']
        argv += ['--entrypoint',inner[0],rt['image'],*inner[1:]]
    else:
        argv = [rt['docker'],'exec','-i',rt['container'],*inner]
    try:
        source = Path(__file__).with_name('worker_main.py').read_text(encoding='utf-8')
        # Shared docker exec must drain: stopping its CLI cannot stop the remote
        # worker. A named one-shot container is removed by the owning finally.
        return run_json(argv,{'provider':provider,'operation':operation,'arguments':args,'_worker_source':source},
                        timeout=timeout+8 if timeout is not None else None,
                        marker='APPLYPILOT_RESULT:',interruptible=name is not None)
    finally:
        if name:
            # An interrupted Docker CLI must not orphan a logged-in worker.
            try: subprocess.run([rt['docker'],'rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
            except (OSError,subprocess.TimeoutExpired): pass


def linkedin_read(argv, session, *, timeout=90):
    from .worker import run_json
    rt = runtime()
    if not rt:
        raise SourceError('Start source-tools for the optional LinkedIn reader; Windows sessions use its isolated container.'
                          if os.name == 'nt' else 'linkedin-rs is not installed and source-tools is not running')
    name = 'applypilot-li-'+uuid.uuid4().hex[:16]
    command = base_run(rt,name)+file_mount(session,'/root/.local/share/linkedin/session.json')
    command += file_mount(session,'/root/.config/linkedin-cli/session.json')
    command += ['--entrypoint','timeout',rt['image'],'--signal=TERM','--kill-after=5s',str(timeout-2)+'s','/usr/local/bin/li',*argv]
    try: return run_json(command,timeout=timeout+5)
    finally:
        try: subprocess.run([rt['docker'],'rm','-f',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5)
        except (OSError,subprocess.TimeoutExpired): pass

"""Explicit support-export context: local saved checks, never live account probes.

Only fixed diagnostic fields and redacted messages cross this boundary. Do not export task
payloads, arbitrary result dictionaries, OAuth/device links, emails or profiles.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import re

from applypilot.onboarding.storage import read_json

_ERRORS = {
    'page_id_missing': 'BrowserOS returned no identifiable owned page ID.',
    'run_result_missing': 'BrowserOS returned no usable script result.',
    'tool_contract': 'The BrowserOS/MCP tool contract does not match this integration.',
    'tool_execution': 'BrowserOS rejected the requested script.',
    'page_evaluation_failed': 'The owned BrowserOS page did not return the expected page data.',
    'browser_timeout': 'BrowserOS timed out while operating on its owned page.',
    'identity_not_ready': 'The signed-in owner profile did not become available before the deadline.',
    'account_blocked': 'Login, challenge or access restriction requires an owner action in BrowserOS.',
    'owner_mismatch': 'The connected account differs from the saved owner.',
    'authentication_required': 'The account requires reauthorization.',
    'verification_timeout': 'Account verification timed out.',
    'verification_failed': 'The account check failed before permission was saved.',
    'connection_failed': 'The local integration is unreachable.',
    'mcp_lifecycle': 'The MCP connection could not close cleanly.',
    'browser_unavailable': 'The BrowserOS account check failed.',
}
_COMPONENTS = {'browseros','browseros_tools','linkedin','x','email','gmail','gmail_tools',
               'gmail_owner','llm','gateway','models','github','sources','authorization'}
_GROUPS = {'browseros','gmail','llm','github','sources','authorization'}
_ACCOUNT_OPERATIONS = {'authorize','verify','gmail_connect','social_connect'}
_OPERATIONS = _ACCOUNT_OPERATIONS | {
    'apply_change', 'session_import', 'session_reuse', 'github_connect', 'github_backup_repo',
    'mcporter_scan', 'mcporter_auth', 'intake', 'material_url', 'suggest', 'github',
    'backup_sync', 'backup_check', 'backup_restore', 'backup_points', 'impact_extract', 'impact_revise',
    'career_ops',
}
_STATES = {'queued','running','completed','failed','deferred','cancelled','timed_out','interrupted'}


def _bool(value):
    return value if type(value) is bool else None


def _time(value):
    if not isinstance(value,str) or len(value)>64:
        return None
    try: return datetime.fromisoformat(value).isoformat()
    except ValueError: return None


def saved_diagnostics(value):
    """Project saved diagnostics without exporting arbitrary task result payloads."""
    from .runtime import diagnostic_details, sanitize
    if not isinstance(value, dict):
        return {}
    result = diagnostic_details(value)
    nested = diagnostic_details(value.get('diagnostics'))
    if nested:
        result['diagnostics'] = nested
    chain = value.get('exception_chain')
    if isinstance(chain, list):
        result['exception_chain'] = []
        for raw in chain[:12]:
            if not isinstance(raw, dict):
                continue
            item = {key: sanitize(raw[key], 1800 if key == 'message' else 200)
                    for key in ('type', 'message', 'relation', 'sqlstate', 'transaction_phase')
                    if isinstance(raw.get(key), str)}
            item.update({key: raw[key] for key in ('errno', 'winerror', 'return_code', 'http_status')
                         if type(raw.get(key)) is int})
            item.update({key: True for key in ('message_truncated', 'frames_truncated') if raw.get(key) is True})
            if type(raw.get('wait_seconds')) in {int, float} and math.isfinite(raw['wait_seconds']):
                item['wait_seconds'] = raw['wait_seconds']
            frames = raw.get('frames')
            if isinstance(frames, list):
                item['frames'] = [{key: sanitize(frame[key], 200) if key != 'line' else frame[key]
                                   for key in ('file', 'line', 'function')
                                   if (isinstance(frame.get(key), str) if key != 'line'
                                       else type(frame.get(key)) is int)}
                                  for frame in frames[-16:] if isinstance(frame, dict)]
            details = diagnostic_details(raw.get('diagnostics'))
            if details:
                item['diagnostics'] = details
            result['exception_chain'].append(item)
        if value.get('exception_chain_truncated') is True or len(chain) > 12:
            result['exception_chain_truncated'] = True
    return result


def _diagnostic(row):
    saved = saved_diagnostics(row)
    code=row.get('error_code') or row.get('code')
    # Recognize the old unclassified message without reflecting arbitrary text.
    if row.get('detail')=='No owned page ID' or row.get('error')=='No owned page ID':
        code='page_id_missing'
    if isinstance(code,str) and code in _ERRORS:
        return {**saved, 'error_code':code,'detail':_ERRORS[code]}
    if row.get('ok') is True:
        return {**saved, 'error_code':None,'detail':'Saved check passed; not rerun for this export.'}
    from applypilot.onboarding.diagnostics import explain
    text=row.get('detail') or row.get('error') or row.get('message') or ''
    # explain returns fixed messages. Raw strings are used only for classification.
    value=explain(RuntimeError(str(text)[:4000]))
    return {**saved, 'error_code':saved.get('code') or value['code'],
            'detail':saved.get('error') or value['error']}


def _checks(rows):
    if not isinstance(rows,list): return []
    return [{'component':r.get('component') if isinstance(r.get('component'),str) and r['component'] in _COMPONENTS else 'other',
             'ok':_bool(r.get('ok')),**_diagnostic(r)} for r in rows[:80] if isinstance(r,dict)]


def snapshot(root: Path) -> dict:
    """Bounded, read-only inspection. Does not import BrowserOS or start Jobs."""
    from .runtime import exception_details
    root=Path(root)
    unavailable=[];unavailable_details={}
    def read(path,section):
        try:
            # Do not follow a workspace's redirected setup/task directories.
            if any(p.is_symlink() for p in (path,path.parent,path.parent.parent) if p!=root.parent):
                raise ValueError('redirected support path')
            value=read_json(path,{},limit=2_000_000)
            if not isinstance(value,dict): raise ValueError('invalid support shape')
            return value
        except (OSError,ValueError,TypeError) as exc:
            if section not in unavailable: unavailable.append(section)
            unavailable_details[section]=exception_details(exc)
            return {}

    cfg=read(root/'unattended.json','outreach_settings')
    backup=read(root/'github_sync'/'account-state.json','backup_state')
    backup_diagnostic=saved_diagnostics(backup.get('error_details'))
    backup_diagnostic.update(saved_diagnostics({key: backup[key]
                                               for key in ('error', 'phase') if backup.get(key) is not None}))
    if backup.get('error_code') is not None:
        backup_diagnostic.update(saved_diagnostics({'code': backup['error_code']}))
    backup_state={'diagnostic':backup_diagnostic,
                  'status':backup.get('status') if isinstance(backup.get('status'), str) and backup.get('status') in
                           {'running','error','authentication_required','up_to_date','synced','restored','interrupted'} else None,
                  **{key: backup[key] for key in ('failed_at','retry_at','failures')
                     if type(backup.get(key)) in {int,float} and math.isfinite(backup[key])}}
    raw=read(root/'setup'/'checks.json','saved_checks')
    checks={}
    for group in sorted(_GROUPS):
        item=raw.get(group)
        if isinstance(item,dict):
            checks[group]={'checked_at':_time(item.get('checked_at')),'ok':_bool(item.get('ok')),
                           'checks':_checks(item.get('checks'))}
    tasks=[]
    folder=root/'setup'/'tasks'
    try:
        if folder.is_symlink() or folder.parent.is_symlink(): raise ValueError('redirected tasks path')
        paths=sorted(folder.glob('*.state.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]
    except (OSError,ValueError) as exc:
        paths=[];unavailable.append('recent_tasks')
        unavailable_details['recent_tasks']=exception_details(exc)
    for path in paths:
        identifier=path.name.removesuffix('.state.json')
        if not re.fullmatch(r'[0-9a-f]{24}',identifier): continue
        row=read(path,'recent_tasks')
        operation=row.get('operation')
        if not isinstance(operation,str) or operation not in _OPERATIONS: continue
        result=row.get('result') if isinstance(row.get('result'),dict) else {}
        state=row.get('state')
        task={'id':identifier,'operation':operation,'state':state if isinstance(state,str) and state in _STATES else 'unknown',
              'started_at':_time(row.get('started_at')),'finished_at':_time(row.get('finished_at')),
              'authorized':_bool(result.get('authorized')),'ok':_bool(result.get('ok')),
              'checks':_checks(result.get('checks'))}
        task.update({key: str(row[key])[:160] for key in ('operation_run_id', 'operation_span_id', 'runtime_build')
                     if isinstance(row.get(key), str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', row[key])})
        if task['state'] in {'failed','timed_out','interrupted'} or result.get('authorized') is False or result.get('ok') is False:
            task['diagnostic']=_diagnostic(result or row)
        tasks.append(task)
    channels=cfg.get('channels') if isinstance(cfg.get('channels'),list) else []
    gmail={}
    try:
        from applypilot.onboarding.environment import child_env
        from applypilot.onboarding.gmail_state import status as gmail_status
        saved=gmail_status(root,child_env(root))
        reconnect=saved.get('reconnect_required') is True
        client=saved.get('client_present') is True
        token=saved.get('token_present') is True
        if reconnect:
            action='Reconnect the same Gmail account from Connections; the saved OAuth client is retained.'
        elif not client:
            action='Restore or upload the Desktop OAuth client in Connections, then connect Gmail.'
        elif not token:
            action='Connect Gmail in Connections to save a refresh token for the intended account.'
        elif saved.get('warnings'):
            action='Open Gmail in Connections and reconnect because saved credential JSON is unreadable.'
        else:
            action='Run Verify Gmail connection from Connections before retrying mailbox work.'
        gmail={
            'source':'saved_workspace_state','live_check_performed':False,
            'configured':saved.get('configured') is True,
            'managed_file_connection':saved.get('managed_file_connection') is True,
            'reconnect_required':reconnect,'client_present':client,
            'saved_account_access_present':token,
            'warnings':[str(v)[:160] for v in (saved.get('warnings') or [])[:10]],
            'action':action,
        }
    except (OSError,ValueError,TypeError,KeyError) as exc:
        unavailable.append('gmail_authentication')
        unavailable_details['gmail_authentication']=exception_details(exc)
    from .bundle import Redactor
    return Redactor(root).value({'captured_at':datetime.now(timezone.utc).isoformat(),'saved_results_only':True,
            'note':'Historical local checks; configuration/accounts may have changed. No browser, login, model or send operation was invoked.',
            'outreach_config':{'enabled':_bool(cfg.get('enabled')),
                               'channels':[c for c in channels if isinstance(c,str) and c in {'email','linkedin','x'}]},
            'saved_checks':checks,'recent_setup_tasks':tasks,
            'recent_account_tasks':[task for task in tasks if task['operation'] in _ACCOUNT_OPERATIONS],
            'gmail_authentication':gmail,'backup_state':backup_state,
            'unavailable_sections':unavailable,'unavailable_details':unavailable_details,
            'raw_task_payloads_excluded':True})

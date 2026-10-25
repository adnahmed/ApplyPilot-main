"""Durable cleanup receipts for networking-owned BrowserOS sessions."""
from __future__ import annotations

import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4


def valid_page(page):
    return (type(page) is int and page >= 0) or (isinstance(page, str) and bool(page.strip()))


def directory():
    from applypilot.config import APP_DIR
    return Path(APP_DIR) / 'networking' / 'browser-sessions'


def _archive_completed(path, value):
    if value.get('cleanup') == 'complete' and value.get('active') is False:
        archive = path.parent / 'completed'
        archive.mkdir(exist_ok=True)
        _replace(path, archive / path.name)


def _replace(source, target):
    os.replace(source, target)


def write(path, value, *, _locked=False):
    from filelock import FileLock, Timeout
    path.parent.mkdir(parents=True, exist_ok=True)
    if not _locked:
        try:
            with FileLock(str(path)+'.lock', timeout=1):
                return write(path,value,_locked=True)
        except Timeout as exc:
            raise OSError('Networking cleanup receipt is busy') from exc
    temp = path.with_suffix('.' + uuid4().hex + '.tmp')
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        _replace(temp, path)
        _archive_completed(path, value)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except PermissionError:
            # A scanner can hold this private temporary file after replacement.
            # Its cleanup must not hide the durable receipt or primary error.
            pass


def checkpoint(browser, *, active=True, errors=()):
    import psutil
    if not getattr(browser, '_cleanup_path', None):
        browser._cleanup_path = directory() / (uuid4().hex + '.json')
    value = {'pid':os.getpid(), 'process_started_at':psutil.Process().create_time(), 'active':active,
             'endpoint_hash':sha256(browser.endpoint.encode()).hexdigest(),
             'session':browser.session, 'resumable':bool(browser.session and browser.session_parameter),
             'pages':list(browser.pages), 'ownership_uncertain':browser.ownership_uncertain,
             'cleanup':'pending' if browser.pages or browser.ownership_uncertain else 'complete',
             'errors':list(errors), 'prior_session_failures':getattr(browser, 'prior_cleanup_failures', [])}
    write(browser._cleanup_path, value)


async def reconcile(browser):
    """Try journaled inactive-session cleanup; return failures for advisory receipts."""
    from filelock import FileLock, Timeout

    root = directory()
    if not root.exists():
        return []
    lock = FileLock(str(root / (sha256(browser.endpoint.encode()).hexdigest() + '.reconcile.lock')), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        return []
    try:
        return await _reconcile(browser, root)
    finally:
        lock.release()


async def _reconcile(browser, root):
    from filelock import FileLock, Timeout
    from applypilot.processes import process_alive
    import psutil
    from .browser import BrowserUnavailable
    from .failures import describe

    unresolved = []
    visible_ids = None
    observation_attempted = False
    deadline = asyncio.get_running_loop().time() + 5
    attempted = 0

    async def known_pages_absent(value):
        nonlocal visible_ids, observation_attempted
        pages = value.get('pages')
        if value.get('ownership_uncertain') or not isinstance(pages, list) or not all(valid_page(p) for p in pages):
            return False
        if not pages:
            return True
        if visible_ids is None:
            if observation_attempted:
                return False
            observation_attempted = True
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError('Prior BrowserOS cleanup deadline reached')
            async with asyncio.timeout_at(deadline):
                observed = await browser.run('return {pages:await browser.pages.list()};', timeout=5)
            listed = observed.get('pages')
            if not isinstance(listed, list) or any(not isinstance(p, dict) or not valid_page(p.get('pageId'))
                    or p.get('ownership') not in {'mine','user','other-agent'} for p in listed):
                return False
            visible_ids = {str(p['pageId']) for p in listed}
        return all(str(page) not in visible_ids for page in pages)

    def owner_alive(value):
        if not process_alive(value.get('pid')):
            return False
        started = value.get('process_started_at')
        if started is None:
            return True  # Legacy records cannot establish that a live PID was reused.
        try:
            return psutil.Process(int(value['pid'])).create_time() == started
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True

    def last_modified(path):
        try:
            return path.stat().st_mtime
        except OSError:
            return 0

    for path in sorted(root.glob('*.json'), key=last_modified):
        if attempted >= 4 or asyncio.get_running_loop().time() >= deadline:
            unresolved.append({'reason_code':'cleanup_reconciliation_deferred', 'retryable':True})
            break
        if path.is_symlink() or path == getattr(browser, '_cleanup_path', None):
            continue
        try:
            with FileLock(str(path) + '.lock', timeout=0):
                value = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(value, dict):
                    unresolved.append({'session_record':path.name, 'reason_code':'invalid_cleanup_record'})
                    continue
                if value.get('cleanup') == 'complete':
                    if value.get('active') and not owner_alive(value):
                        value['active'] = False
                        write(path, value, _locked=True)
                    else:
                        _archive_completed(path, value)
                    continue
                if value.get('endpoint_hash') != sha256(browser.endpoint.encode()).hexdigest():
                    continue
                if value.get('active') and owner_alive(value):
                    continue
                attempted += 1
                handle = value.get('session')
                if not handle or not value.get('resumable') or not browser.session_parameter:
                    try:
                        absent = await known_pages_absent(value)
                    except Exception:
                        absent = False
                    if absent:
                        value.update(active=False, pages=[], cleanup='complete', errors=[])
                        write(path, value, _locked=True)
                    else:
                        failure = {'session_record':path.name, 'reason_code':'session_resume_unavailable',
                                   'reason':'Recorded pages remain present or ownership cannot be established.',
                                   'retryable':True}
                        value.update(active=False, errors=[failure])
                        write(path, value, _locked=True)
                        unresolved.append(failure)
                    continue
                async def run(code):
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError('Prior BrowserOS cleanup deadline reached')
                    args = {browser.session_parameter:handle, 'code':code}
                    if browser.run_properties is None or 'timeout' in browser.run_properties:
                        args['timeout'] = max(1, int((deadline - asyncio.get_running_loop().time()) * 1000))
                    async with asyncio.timeout_at(deadline):
                        response = await browser.client.call_tool('run', args)
                    raw = response.model_dump(by_alias=True)
                    returned = (raw.get('_meta') or {}).get('com.browseros.neo/session', handle)
                    if returned != handle:
                        raise BrowserUnavailable('BrowserOS cannot resume the recorded cleanup session.', code='browser_session_changed')
                    data = browser.decode(response)
                    if isinstance(data, dict) and isinstance(data.get('ok'), bool) and any(k in data for k in ('value','logs','error')):
                        if data['ok'] is not True:
                            browser._tool_failure(data.get('error',''))
                        data = data.get('value')
                    if isinstance(data, str):
                        data = json.loads(browser._data_text(data))
                    return data
                errors = []
                try:
                    observed = await run('return {pages:await browser.pages.list()};')
                    pages = observed.get('pages') if isinstance(observed, dict) else None
                    if not isinstance(pages, list) or any(not isinstance(p, dict) or p.get('ownership') not in {'mine','user','other-agent'} for p in pages):
                        raise BrowserUnavailable('BrowserOS cleanup could not establish page ownership.', code='cleanup_ownership_unknown')
                    owned = [p.get('pageId') for p in pages if p['ownership'] == 'mine']
                    if not all(valid_page(p) for p in owned):
                        raise BrowserUnavailable('BrowserOS cleanup returned an invalid page identifier.', code='page_id_missing')
                    remaining = list(owned)
                    value.update(pages=remaining, ownership_uncertain=False)
                    write(path, value, _locked=True)
                    for page in owned:
                        try:
                            closed = await run('await browser.pages.close('+json.dumps(page)+');return {closed:true};')
                            if closed != {'closed':True}:
                                raise BrowserUnavailable('BrowserOS did not confirm page cleanup.', code='cleanup_unconfirmed')
                            remaining.remove(page)
                        except Exception as exc:
                            errors.append(describe(exc, channel='browseros', operation='cleanup'))
                            if asyncio.get_running_loop().time() >= deadline:
                                break
                except Exception as exc:
                    errors.append(describe(exc, channel='browseros', operation='cleanup_reconcile'))
                if errors:
                    try:
                        if await known_pages_absent(value):
                            value.update(pages=[], ownership_uncertain=False)
                            errors = []
                    except Exception:
                        pass
                value.update(active=False, errors=errors,
                             cleanup='pending' if errors else 'complete')
                write(path, value, _locked=True)
                if errors:
                    unresolved.append({'session_record':path.name, 'failures':errors})
        except Timeout:
            unresolved.append({'session_record':path.name, 'reason_code':'cleanup_reconciliation_busy', 'retryable':True})
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as exc:
            unresolved.append({'session_record':path.name, 'reason_code':type(exc).__name__})
    return unresolved

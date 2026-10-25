"""Bounded, database-independent failure journal with no raw exception text.

One append-only file per process avoids cross-process rotation races. Diagnostics
must never replace a business exception, even when the filesystem is unavailable.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import threading

_lock=threading.Lock()
log=logging.getLogger(__name__)


def emit(component: str, *, reason: str='', error: BaseException | None=None, root=None) -> bool:
    from applypilot.config import APP_DIR
    payload={'at':datetime.now(timezone.utc).isoformat(), 'pid':os.getpid(),
             'component':re.sub(r'[^a-zA-Z0-9_.:-]','_',str(component))[:80],
             'reason_sha256':hashlib.sha256(str(reason).encode()).hexdigest(),
             'exception_type':type(error).__name__ if error else None,
             'sqlstate':getattr(error,'sqlstate',None), 'database_independent':True}
    data=(json.dumps(payload,sort_keys=True,separators=(',',':'))+'\n').encode()
    try:
        with _lock:
            directory=Path(root or APP_DIR)/'operations'/'emergency'
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            path=directory/(str(os.getpid())+'.jsonl')
            if path.is_symlink():raise OSError('Refusing redirected incident journal')
            if path.exists() and path.stat().st_size>1024*1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            fd=os.open(path,os.O_APPEND|os.O_CREAT|os.O_WRONLY,0o600)
            try:
                os.write(fd,data)
                os.fsync(fd)
            finally:os.close(fd)
        return True
    except Exception:
        # Standard logging can also be misconfigured; the primary failure wins.
        try:log.error('Emergency journal unavailable; component=%s type=%s state=%s',
                      payload['component'],payload['exception_type'],payload['sqlstate'])
        except Exception:pass
        return False

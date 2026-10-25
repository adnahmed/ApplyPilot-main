"""Best-effort OS priority for independent maintenance workers."""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)




def lower_priority(pid: int) -> bool:
    """Best effort; never terminate a process or request elevated privileges."""
    import psutil
    try:
        process = psutil.Process(pid)
        if os.name == "nt":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            process.nice(max(10, process.nice()))
        return True
    except (psutil.Error, OSError):
        log.debug("Could not lower maintenance worker priority", exc_info=True)
        return False

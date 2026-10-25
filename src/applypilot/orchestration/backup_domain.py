"""One finite backup operation; timing and retries belong to its Activity."""
from .contracts import Observation
from . import store


def capture(options):
    from applypilot.database import get_connection, close_connection
    try:
        conn = get_connection()
        reference = store.put(conn, {"kind": "backup_request", "options": dict(options)})
        conn.commit()
        return reference
    finally:
        close_connection()


def execute(request, stop, heartbeat):
    from applypilot.config import APP_DIR
    from applypilot.database import get_connection, close_connection
    from applypilot.backup import service
    from applypilot.discovery.repository import transaction
    try:
        value, root = store.work_payload(get_connection(), request.reference)
        options = root["options"]
    finally:
        close_connection()
    if stop.is_set():
        from applypilot.execution import ExecutionStopped
        raise ExecutionStopped("Backup cancelled")
    progress = lambda **kw: heartbeat("backup")
    if request.phase in {"backup", "backup_full"}:
        result = service.sync(APP_DIR, full=request.phase == "backup_full",
                              automatic=options.get("automatic", True), progress=progress)
    elif request.phase == "backup_check":
        result = service.check(APP_DIR, progress=progress)
    elif request.phase == "backup_points":
        result = service.list_points(APP_DIR, progress=progress)
    else:
        raise ValueError("Unknown backup operation")
    # Result bodies stay outside Temporal history (repository names, point IDs).
    conn = get_connection()
    with transaction(conn):
        reference = store.put(conn, {"kind": "backup_result", "result": result})
    return Observation(str(result.get("status", "complete")), {"result_reference": reference})

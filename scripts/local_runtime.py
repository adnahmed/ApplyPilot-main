"""Run a host CLI process with the existing shared PostgreSQL lifecycle."""
import os
from contextlib import ExitStack
import signal
import sys
import threading

from applypilot import config, postgres

config.load_env()
with ExitStack() as lifetime:
    role = os.environ.pop('APPLYPILOT_LOCAL_ROLE', '')
    if role:
        if role not in {'dashboard', 'activities', 'applications'}:
            raise ValueError('Unknown local runtime role')
        from applypilot.processes import process_guard
        lifetime.enter_context(process_guard(config.APP_DIR / 'local-runtime' / (role + '.guard')))

    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)
    try:
        if not os.environ.get('APPLYPILOT_DATABASE_URL', '').strip():
            from pgembed._commands import POSTGRES_BIN_PATH
            os.environ['PATH'] = str(POSTGRES_BIN_PATH) + os.pathsep + os.environ.get('PATH', '')
        os.environ['APPLYPILOT_DATABASE_URL'] = postgres.server_uri()
    finally:
        if os.name == 'nt':
            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)

    from applypilot.cli import app

    finished = threading.Event()
    if role:
        def watch_launcher():
            if os.name == 'nt':
                import msvcrt
                from ctypes import wintypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                peek = kernel.PeekNamedPipe
                peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                 ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
                peek.restype = wintypes.BOOL
                handle = msvcrt.get_osfhandle(sys.stdin.fileno())
                while not finished.wait(.2):
                    if not peek(handle, None, 0, None, None, None):
                        if ctypes.get_last_error() not in {109, 232, 233}:
                            return
                        break
                else:
                    return
            else:
                sys.stdin.buffer.read()
            if not finished.wait(.25):
                signal.raise_signal(signal.SIGINT)

        threading.Thread(target=watch_launcher, name='local-runtime-owner', daemon=True).start()
    try:
        app()
    finally:
        finished.set()

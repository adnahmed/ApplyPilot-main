"""Best-effort telemetry. The emergency submodule has no database dependency."""
from importlib import import_module

__all__ = ["capture", "correlation", "observe", "record", "scope", "submit", "thread_target", "worker_phase", "worker_recovered"]

def __getattr__(name):
    if name in __all__:
        value = getattr(import_module(".runtime", __name__), name)
        globals()[name] = value
        return value
    raise AttributeError(name)

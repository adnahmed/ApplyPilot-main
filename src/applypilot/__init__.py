"""ApplyPilot — AI-powered end-to-end job application pipeline."""

__version__ = "0.2.0"

# DSPy 3.3.1 constructs a diskcache at import, before configure_cache is callable.
# An existing device file forces its documented memory-only fallback BEFORE any
# cache database is created. Do not allow an environment override to reintroduce
# application-owned file databases. The explicit cache configuration below is
# applied when our DSPy adapter loads. Browser-internal cookies/history are not
# application-owned relational stores.
import os as _os
_os.environ["DSPY_CACHEDIR"] = _os.devnull

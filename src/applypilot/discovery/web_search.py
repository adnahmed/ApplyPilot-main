"""Free DDGS/self-hosted SearXNG discovery, preserving weak-signal provenance."""
from .free_signals import execute as execute_task
from .signal_cache import run_source


def run_web_search_discovery(*, context=None, workers=1):
    return run_source("web_search", context=context, workers=workers)

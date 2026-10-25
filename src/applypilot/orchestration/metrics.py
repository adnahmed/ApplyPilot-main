"""Structured duration observations; telemetry never imports an execution engine."""
from contextlib import contextmanager
import logging
import time

log = logging.getLogger(__name__)


@contextmanager
def meter(name):
    started = time.perf_counter()
    outcome = "completed"
    try:
        yield
    except BaseException:
        outcome = "failed"
        raise
    finally:
        log.info("operation_duration", extra={"operation": name,
            "outcome": outcome, "duration_seconds": time.perf_counter() - started})

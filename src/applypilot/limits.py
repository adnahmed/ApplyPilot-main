"""Work-volume limits are not request sizes or executor sizes.

-1 is the only uncapped workload sentinel. Zero still disables a workload;
positive integers are real caps. Never pass -1 to an SDK, range(), a semaphore,
or a Python slice. Paid budgets and outbound-send policy are deliberately not
converted to this convention.
"""
from __future__ import annotations

from itertools import count
import os

UNLIMITED = -1


def work_limit(value: int, name: str = "limit", *, allow_zero: bool = True) -> int:
    if type(value) is not int or (value != UNLIMITED and value < (0 if allow_zero else 1)):
        qualifier = "nonnegative" if allow_zero else "positive"
        raise ValueError(f"{name} must be -1 (uncapped) or a {qualifier} integer")
    return value


def has_capacity(limit: int | float, used: int | float, amount: int | float = 1) -> bool:
    """Apply only to already validated limits; accounting continues when uncapped."""
    return limit == UNLIMITED or used + amount <= limit


def page_size(limit: int, maximum: int = 100, *, used: int = 0) -> int:
    """Lower a workload target to a finite, nonnegative request size."""
    work_limit(limit)
    if type(maximum) is not int or maximum < 1 or type(used) is not int or used < 0:
        raise ValueError("Page maximum must be positive and used must be nonnegative")
    return maximum if limit == UNLIMITED else max(0, min(maximum, limit - used))


def page_indices(limit: int):
    """Every caller must still stop on exhaustion, nonprogress, errors or cancellation."""
    work_limit(limit)
    return count() if limit == UNLIMITED else iter(range(limit))


def default_workers(kind: str = "research") -> int:
    """A finite CPU-aware starting pool, not a cap on the amount of work.

    Explicit dashboard/CLI worker counts may exceed these starting values.
    Browser/account governors remain independent.
    """
    cpu = os.cpu_count() or 1
    return cpu * 2 if kind == "prepare" else cpu


def configured_workers() -> int:
    """Resolve the shared execution worker count independently of source budgets."""
    from applypilot.runtime_policy import load_policy
    return load_policy().capacity.workers

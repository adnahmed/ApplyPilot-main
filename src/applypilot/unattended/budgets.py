"""Owner workload quotas. -1 is uncapped; 0 pauses; positive integers are caps.

Finite per-tick batches, provider rate limits, exact-recipient validation and
uncertain-delivery reconciliation are independent of a daily workload quota.
"""
from __future__ import annotations

MAX_EXACT_INTEGER = 2**53 - 1  # JSON/browser integer representation, not a product quota.


def validate_limit(value: int, name: str = "limit") -> int:
    if type(value) is not int or not -1 <= value <= MAX_EXACT_INTEGER:
        raise ValueError(f"{name} must be -1 (uncapped), 0 (paused), or a positive whole number")
    return value


def exhausted(used: int, *limits: int) -> bool:
    # Check EACH finite policy: min(-1, positive) would incorrectly pause all sends.
    return any(validate_limit(limit) >= 0 and used >= limit for limit in limits)


def remaining(used: int, *limits: int) -> int:
    finite = [validate_limit(limit) for limit in limits if limit != -1]
    return max(0, min(finite) - used) if finite else -1


def batch_size(limit: int, maximum: int = 100) -> int:
    value = validate_limit(limit)
    return maximum if value == -1 else min(value, maximum)

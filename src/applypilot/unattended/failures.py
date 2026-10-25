"""Concrete diagnostics for finite provider reads; never a retry policy."""
from __future__ import annotations
import asyncio


def describe(exc, *, channel='networking', operation='research'):
    return {'channel': channel, 'operation': operation,
            'error_type': type(exc).__name__,
            'reason_code': getattr(exc, 'code', None) or getattr(exc, 'kind', None) or type(exc).__name__,
            'reason': str(exc)[:500]}


def record(failures, exc, *, channel='networking', operation='research'):
    if isinstance(exc, asyncio.CancelledError):
        raise exc
    value = describe(exc, channel=channel, operation=operation)
    if value not in failures:
        failures.append(value)
    return value

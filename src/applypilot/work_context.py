"""Immutable-scope operation metadata, not a stage pool or work selector."""
from __future__ import annotations
from contextlib import contextmanager
from contextvars import ContextVar

_context: ContextVar[dict | None] = ContextVar("applypilot_work_context", default=None)


def stage_context() -> dict:
    return _context.get() or {}


@contextmanager
def stage_scope(**values):
    current = {**stage_context(), **values}
    token = _context.set(current)
    try:
        yield current
    finally:
        _context.reset(token)

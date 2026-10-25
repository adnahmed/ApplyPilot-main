"""Inference cancellation and diagnostics; LiteLLM owns transport recovery."""
from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field


class InferenceCancelled(RuntimeError):
    """The owning application or HTTP caller stopped this computation."""

    error_kind = "cancelled"


async def cancellable(awaitable, cancel_event=None, timeout=None, *, cancel_events=()):
    """Cancel the actual network coroutine, not merely its waiting host thread."""
    task = asyncio.ensure_future(awaitable)
    started = time.monotonic()
    try:
        while True:
            if task.done():
                return task.result()
            if ((cancel_event is not None and cancel_event.is_set())
                    or any(event.is_set() for event in cancel_events)):
                raise InferenceCancelled("Inference cancelled by its owner")
            remaining = None if timeout is None else timeout - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                raise InferenceBudgetExceeded("Explicit inference time budget exhausted",
                                              reason_code="inference_deadline", error_kind="timeout")
            poll = min(0.2, remaining) if remaining is not None else 0.2
            done, _ = await asyncio.wait({task}, timeout=poll)
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class InferenceBudgetExceeded(RuntimeError):
    """No further gateway requests may be made for this inference run."""

    def __init__(self, message, *, reason_code=None, error_kind="policy"):
        super().__init__(message)
        self.reason_code = reason_code
        self.error_kind = error_kind


@dataclass
class InferenceContext:
    """Activity cancellation and failure propagation; never execution admission.

    DSPy may catch evaluator exceptions internally. Keep the original failure so
    the Activity fails instead of publishing a candidate with fabricated scores.
    Counters are diagnostics only; LiteLLM owns actual token usage and cost.
    """
    cancel_event: threading.Event | None = None
    calls: int = field(default=0, init=False)
    reserved_output_tokens: int = field(default=0, init=False)
    failure_reason: str | None = field(default=None, init=False)
    failure_error_type: str | None = field(default=None, init=False)
    _error: BaseException | None = field(default=None, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __deepcopy__(self, memo):
        return self

    def fail(self, error=None, *, reason_code=None):
        with self._lock:
            if self._error is None:
                self._error = error or RuntimeError("Inference failed")
                self.failure_reason = reason_code or type(self._error).__name__
                self.failure_error_type = type(self._error).__name__

    def remaining(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InferenceCancelled("Activity inference cancelled")
        if self._error is not None:
            raise self._error
        return None

    def reserve(self, max_tokens):
        self.remaining()
        if max_tokens < 1:
            raise ValueError("Per-call output tokens must be positive")
        with self._lock:
            self.calls += 1
            self.reserved_output_tokens += max_tokens


def gateway_chat(client, messages, *, max_tokens, temperature, response_format=None, budget=None, cancel_event=None):
    """Keep ApplyPilot's transport, workload headers, diagnostics and proxy policy.

    The context counts gateway requests without admitting or scheduling work.
    Provider output limits remain per request; LiteLLM reports actual usage.
    """
    if not client._is_proxy:
        raise RuntimeError("DSPy/RLM require the existing LLM_URL gateway; direct provider fallback is disabled")
    if cancel_event is None and budget is not None:
        cancel_event = budget.cancel_event
    if budget is None and cancel_event is None:
        return client.chat(messages, max_tokens=max_tokens, temperature=temperature, response_format=response_format)
    if budget is not None:
        budget.reserve(max_tokens)
    from applypilot import operations as ops
    from applypilot.llm import _run_async

    try:
        timeout = budget.remaining() if budget is not None else None
        with ops.worker_phase("waiting_provider", task=client.task.value):
            return _run_async(client._achat(messages, temperature, max_tokens, response_format),
                              cancel_event=cancel_event, timeout=timeout)
    except Exception as exc:
        from applypilot.llm import EmptyModelOutput, OutputBudgetExceeded
        # chat_json owns one correction for truncated or empty output. Other
        # transport failures must not be hidden by GEPA's evaluator as a score.
        if budget is not None and not isinstance(exc, (OutputBudgetExceeded, EmptyModelOutput)):
            budget.fail(exc)
        raise

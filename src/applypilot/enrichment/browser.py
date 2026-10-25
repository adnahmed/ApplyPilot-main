"""Bounded owned-browser calls for synchronous detail extractors."""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
import inspect
import logging
import threading
import time


log = logging.getLogger(__name__)


def check_running():
    from applypilot.execution import ExecutionStopped, current_task
    from applypilot.work_context import stage_context
    events = (current_task().get('stop_event'),
              stage_context().get('stop_event'))
    if any(event is not None and event.is_set() for event in events):
        raise ExecutionStopped('Enrichment stopped by its owner')


class _ReadStopped(BaseException):
    pass


class _ReadTimeout(BaseException):
    pass


class _Object:
    def __init__(self, owner, target):
        self.owner, self.target = owner, target

    def __getattr__(self, name):
        if name in {'url', 'status'}:
            return self.owner.invoke(self.target, name, property_only=True)
        return lambda *args, **kwargs: self.owner.invoke(self.target, name, *args, **kwargs)


class _Browser:
    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.manager = self.browser = None
        self.deadline = time.monotonic() + 90
        self.thread = threading.Thread(target=self._serve, name='enrichment-browser', daemon=True)
        self.thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self.loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self.loop.run_until_complete(asyncio.wait(tasks, timeout=1))
            self.loop.close()

    def run(self, operation, *, timeout=30, cleanup=False):
        async def bounded():
            return await asyncio.wait_for(operation(), timeout)

        future = asyncio.run_coroutine_threadsafe(bounded(), self.loop)
        deadline = time.monotonic() + timeout
        if not cleanup:
            deadline = min(deadline, self.deadline)
        try:
            while True:
                if not cleanup:
                    from applypilot.execution import ExecutionStopped
                    try:
                        check_running()
                    except ExecutionStopped as exc:
                        raise _ReadStopped(str(exc)) from exc
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _ReadTimeout('Owned enrichment browser read timed out')
                try:
                    return future.result(timeout=min(.2, remaining))
                except FutureTimeout as exc:
                    if future.done():
                        raise _ReadTimeout('Owned enrichment browser operation timed out') from exc
        finally:
            if not future.done():
                future.cancel()

    def invoke(self, target, name, *args, property_only=False, **kwargs):
        async def operation():
            value = getattr(target, name)
            if not property_only:
                value = value(*args, **kwargs)
            return await value if inspect.isawaitable(value) else value

        def wrap(value):
            if isinstance(value, list):
                return [wrap(item) for item in value]
            if type(value).__module__.startswith('patchright.async_api.'):
                return _Object(self, value)
            return value

        return wrap(self.run(operation))

    async def open(self, options, user_agent, init_script):
        from patchright.async_api import async_playwright
        self.manager = async_playwright()
        playwright = await self.manager.start()
        self.browser = await playwright.chromium.launch(**options)
        context = await self.browser.new_context(user_agent=user_agent)
        await context.add_init_script(init_script)
        return await context.new_page()

    async def close(self):
        for target in (self.browser, self.manager):
            if target is None:
                continue
            try:
                await asyncio.wait_for(target.close() if target is self.browser else target.__aexit__(None, None, None), 4)
            except Exception as exc:
                log.warning('Owned enrichment browser cleanup: %s', type(exc).__name__)


@contextmanager
def owned_page(*, options, user_agent, init_script):
    from applypilot.execution import ExecutionStopped
    check_running()
    owner = _Browser()
    try:
        page = owner.run(lambda: owner.open(options, user_agent, init_script), timeout=45)
        yield _Object(owner, page)
    except _ReadStopped as exc:
        raise ExecutionStopped(str(exc)) from exc
    except _ReadTimeout as exc:
        raise TimeoutError(str(exc)) from exc
    finally:
        try:
            owner.run(owner.close, timeout=10, cleanup=True)
        except (_ReadTimeout, Exception) as exc:
            log.warning('Owned enrichment browser cleanup did not settle: %s', type(exc).__name__)
        finally:
            if not owner.loop.is_closed():
                owner.loop.call_soon_threadsafe(owner.loop.stop)
            owner.thread.join(timeout=3)
            if owner.thread.is_alive():
                log.warning('Owned enrichment browser loop did not stop within its cleanup deadline')

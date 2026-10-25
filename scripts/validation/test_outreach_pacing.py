"""Deterministic account pacing checks with no services, accounts or outbound actions."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from uuid import uuid4
from unittest.mock import AsyncMock, patch

if len(Path(__file__).resolve().parents) > 2:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from applypilot.orchestration import integration_workflows as module
from applypilot.orchestration.contracts import Observation
from applypilot.orchestration.integration_contracts import AccountCommand
from applypilot.orchestration.contracts import ActivityRequest, task_queue
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Replayer, Worker


class EndRun(BaseException):
    pass


class AccountPacingChecks(unittest.TestCase):
    def run_account(self, instance, command, wait, operation, clock=None, continuation=None):
        handle = SimpleNamespace(signal=AsyncMock())
        with patch.object(module, "operation", operation), \
                patch.object(module.workflow, "wait_condition", wait), \
                patch.object(module.workflow, "now", lambda: datetime.fromtimestamp((clock or [200000])[0], UTC)), \
                patch.object(module.workflow, "get_external_workflow_handle", return_value=handle), \
                patch.object(module.workflow, "continue_as_new", continuation or (lambda command: None)):
            with self.assertRaises(EndRun):
                asyncio.run(instance.run(command))
        return handle

    def test_update_validation_and_query(self):
        instance = module.AccountWorkflow()
        bad = [None, {}, {"spacing_seconds": 1, "daily_limit": -2},
               {"spacing_seconds": 0, "daily_limit": 1}, {"spacing_seconds": True, "daily_limit": 1},
               {"spacing_seconds": math.inf, "daily_limit": 1}, {"spacing_seconds": math.nan, "daily_limit": 1},
               {"spacing_seconds": 1, "daily_limit": 1.5}, {"spacing_seconds": 1, "daily_limit": False}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                instance.set_pacing(value)
        self.assertEqual(instance.status()["pacing_revision"], 0)
        for revision, daily in enumerate((-1, 0, 5), 1):
            value = {"spacing_seconds": 600, "daily_limit": daily}
            self.assertEqual(instance.set_pacing(value), value)
            value["daily_limit"] = 999
            self.assertEqual(instance.status()["pacing"]["daily_limit"], daily)
            self.assertEqual(instance.status()["pacing_revision"], revision)

    def test_update_wakes_existing_timer_and_preserves_dispatch_history(self):
        instance = module.AccountWorkflow()
        clock, delays, phases = [200000.0], [], []
        item = {"reference": "message-1", "reply_workflow": "caller", "channel": "email"}
        command = AccountCommand({}, "account", [item], [clock[0]], True)

        async def operation(work, phase, **kwargs):
            phases.append(phase)
            if phase == "account_send_policy":
                return Observation("ready", {"not_before": 0, "spacing_seconds": 600, "daily_limit": -1})
            return Observation("sent", {"receipt_id": "receipt-1"})

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if timeout is None:
                raise EndRun()
            delays.append(timeout.total_seconds())
            if len(delays) == 1:
                instance.set_pacing({"spacing_seconds": 10, "daily_limit": -1})
                self.assertTrue(predicate())
            else:
                clock[0] += timeout.total_seconds()
                raise TimeoutError()

        handle = self.run_account(instance, command, wait, operation, clock)
        self.assertEqual(delays, [600, 10])
        self.assertEqual(clock[0], 200010)
        self.assertEqual(phases.count("account_send_policy"), 3)
        self.assertEqual(phases.count("send_email"), 1)
        self.assertEqual(handle.signal.await_args.args[1].detail, {"receipt_id": "receipt-1"})

    def test_update_zero_waits_durably_and_resume_rechecks_permission(self):
        instance = module.AccountWorkflow()
        instance.set_pacing({"spacing_seconds": 10, "daily_limit": 0})
        item = {"reference": "message-1", "reply_workflow": "caller", "channel": "email"}
        policies = []

        async def operation(work, phase, **kwargs):
            if phase == "account_send_policy":
                policies.append(phase)
                return Observation("ready", {"not_before": 0, "spacing_seconds": 600, "daily_limit": 5})
            return Observation("sent")

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if not instance.pending:
                raise EndRun()
            self.assertIsNone(timeout)
            instance.set_pacing({"spacing_seconds": 10, "daily_limit": 2})
            self.assertTrue(predicate())

        handle = self.run_account(instance, AccountCommand({}, "account", [item], initialized=True), wait, operation)
        self.assertEqual(len(policies), 2)
        self.assertEqual(handle.signal.await_args.args[1].state, "sent")
        self.assertEqual(instance.status()["effective_pacing"]["daily_limit"], 2)

    def test_update_cannot_remove_authorized_daily_ceiling(self):
        instance = module.AccountWorkflow()
        instance.set_pacing({"spacing_seconds": 1, "daily_limit": -1})
        item = {"reference": "message-1", "reply_workflow": "caller", "channel": "email"}
        command = AccountCommand({}, "account", [item], [199950, 199980], True)
        operation = AsyncMock(return_value=Observation("ready", {
            "not_before": 0, "spacing_seconds": 600, "daily_limit": 2}))
        delays = []

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if not instance.pending:
                raise EndRun()
            delays.append(timeout.total_seconds())
            instance.cancel("message-1")
            self.assertTrue(predicate())

        handle = self.run_account(instance, command, wait, operation)
        self.assertEqual(delays, [259200 - 200000])
        self.assertEqual(instance.status()["effective_pacing"]["daily_limit"], 2)
        self.assertEqual(operation.await_count, 1)
        self.assertEqual(handle.signal.await_args.args[1].state, "cancelled")

    def test_update_preserves_provider_not_before(self):
        instance = module.AccountWorkflow()
        instance.set_pacing({"spacing_seconds": 1, "daily_limit": -1})
        item = {"reference": "message-1", "reply_workflow": "caller", "channel": "email"}
        operation = AsyncMock(return_value=Observation("ready", {
            "not_before": 200300, "spacing_seconds": 600, "daily_limit": -1}))
        delays = []

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if not instance.pending:
                raise EndRun()
            delays.append(timeout.total_seconds())
            instance.cancel("message-1")

        self.run_account(instance, AccountCommand({}, "account", [item], initialized=True), wait, operation)
        self.assertEqual(delays, [300])

    def test_permission_or_zero_policy_cannot_be_overridden(self):
        for state in ("paused", "needs_authorization"):
            with self.subTest(state=state):
                instance = module.AccountWorkflow()
                instance.set_pacing({"spacing_seconds": 1, "daily_limit": 999})
                item = {"reference": "message-1", "reply_workflow": "caller", "channel": "email"}
                operation = AsyncMock(return_value=Observation(state))

                async def wait(predicate, timeout=None):
                    if not predicate():
                        raise EndRun()

                handle = self.run_account(instance, AccountCommand({}, "account", [item]), wait, operation)
                self.assertEqual(operation.await_count, 1)
                self.assertEqual(handle.signal.await_args.args[1].state, state)

    def test_pacing_survives_continue_as_new_and_serialization(self):
        instance = module.AccountWorkflow()
        pending = [{"reference": str(index), "reply_workflow": "caller", "channel": "email"} for index in range(101)]
        pacing = {"spacing_seconds": 300, "daily_limit": 2}
        command = AccountCommand({}, "account", pending, [199900], True,
                                 [str(index) for index in range(100)], pacing, 7)
        continued = []

        async def wait(predicate, timeout=None):
            self.assertTrue(predicate())

        def continuation(value):
            continued.append(value)
            raise EndRun()

        operation = AsyncMock()
        self.run_account(instance, command, wait, operation, continuation=continuation)
        self.assertEqual(operation.await_count, 0)
        restored = AccountCommand(**json.loads(json.dumps(asdict(continued[0]))))
        self.assertEqual(restored.pacing, pacing)
        self.assertEqual(restored.pacing_revision, 7)
        self.assertEqual(restored.dispatched_at, [199900])
        self.assertEqual(restored.pending, [pending[-1]])
        self.assertTrue(restored.initialized)
        self.assertEqual(AccountCommand({}, "old-history").pacing, {})


class FakeOutreach:
    def __init__(self):
        self.sent = []

    @activity.defn(name="applypilot.integration.account_send_policy.v1")
    async def policy(self, request: ActivityRequest) -> Observation:
        return Observation("ready", {"not_before": 0, "spacing_seconds": 4, "daily_limit": 100})

    @activity.defn(name="applypilot.integration.send_email.v1")
    async def send(self, request: ActivityRequest) -> Observation:
        self.sent.append(request.reference)
        return Observation("sent", {"receipt_id": request.reference})


@unittest.skipUnless(os.getenv("APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS"), "An isolated Temporal server is required")
class NativeOutreachPacingChecks(unittest.IsolatedAsyncioTestCase):
    async def test_live_updates_pause_resume_daily_wait_and_replay(self):
        client = await Client.connect(os.environ["APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS"])
        settings = {"workspace": uuid4().hex, "queue_prefix": "isolated-outreach",
                    "activity_retry_initial_seconds": 0.1, "activity_retry_maximum_seconds": 1,
                    "activities": {role: {"heartbeat_interval_seconds": 0.1,
                        "start_to_close_seconds": 30, "heartbeat_seconds": 2} for role in ("network", "email")}}
        queue = lambda role: task_queue(settings["queue_prefix"], settings["workspace"], role)
        item = lambda value: {"reference": value, "reply_workflow": "isolated-missing-requester", "channel": "email"}
        fake = FakeOutreach()
        command = AccountCommand(settings, "isolated-account", [item("one")], [time.time()], True)
        async with Worker(client, task_queue=queue("workflow"), workflows=[module.AccountWorkflow]), \
                Worker(client, task_queue=queue("network"), activities=[fake.policy]), \
                Worker(client, task_queue=queue("email"), activities=[fake.send]):
            handle = await client.start_workflow(module.AccountWorkflow.run, command,
                id="isolated-outreach-" + uuid4().hex, task_queue=queue("workflow"))

            async def until(predicate):
                while True:
                    observed = await handle.query(module.AccountWorkflow.status)
                    if predicate(observed):
                        return observed
                    await asyncio.sleep(0.02)

            try:
                await asyncio.wait_for(until(lambda value: value["effective_pacing"].get("spacing_seconds") == 4), 10)
                paused = {"spacing_seconds": 0.01, "daily_limit": 0}
                self.assertEqual(await handle.execute_update(module.AccountWorkflow.set_pacing, paused), paused)
                await asyncio.wait_for(until(lambda value: value["effective_pacing"].get("daily_limit") == 0), 10)
                self.assertEqual(fake.sent, [])
                # The historical attempt still counts after the override. Allow
                # that attempt plus the first fixture message, then exhaust two.
                await handle.execute_update(module.AccountWorkflow.set_pacing, {**paused, "daily_limit": 2})
                await asyncio.wait_for(until(lambda value: value["pending"] == 0), 10)
                self.assertEqual(fake.sent, ["one"])
                await handle.signal(module.AccountWorkflow.enqueue, item("two"))
                await asyncio.wait_for(until(lambda value: value["pending"] == 1), 10)
                self.assertEqual(fake.sent, ["one"])
                await handle.execute_update(module.AccountWorkflow.set_pacing, {**paused, "daily_limit": 3})
                await asyncio.wait_for(until(lambda value: value["pending"] == 0), 10)
                self.assertEqual(fake.sent, ["one", "two"])
                await handle.execute_update(module.AccountWorkflow.set_pacing, paused)
                await handle.signal(module.AccountWorkflow.enqueue, item("cancelled"))
                await asyncio.wait_for(until(lambda value: value["pending"] == 1), 10)
                await handle.signal(module.AccountWorkflow.cancel, "cancelled")
                await asyncio.wait_for(until(lambda value: value["pending"] == 0), 10)
                self.assertEqual(fake.sent, ["one", "two"])
            finally:
                await handle.cancel()
                with self.assertRaises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 10)
        await Replayer(workflows=[module.AccountWorkflow]).replay_workflow(await handle.fetch_history())


if __name__ == "__main__":
    unittest.main()

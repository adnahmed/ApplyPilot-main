"""Provider observation recovery and durable account pacing without provider actions."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import math
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from applypilot.apply import provider_observations as evidence
from applypilot.orchestration import account_workflows as module, workflows
from applypilot.orchestration.contracts import (
    AccountSubmission, ActivityRequest, ApplicationAccountCommand, ApplicationCommand, Observation, task_queue)
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import ActivityError, CancelledError
from temporalio.worker import Replayer, Worker


def policy():
    return {'linkedin_applications': {'account_id': 'account', 'spacing_seconds': 600,
             'hourly_limit': 4, 'daily_limit': 20, 'restriction_retry_seconds': 86400}}


def fact(workflow_id='account-workflow', delay=1200, stamp=200000.0):
    return {'kind': evidence.KIND, 'workflow_id': workflow_id, 'account_id': 'account',
            'attempt_id': 'first', 'observed_at': stamp, 'retry_after_seconds': delay,
            'evidence_reference': 'a' * 64}


class EndRun(BaseException):
    pass


class ProviderPacingChecks(unittest.TestCase):
    def actor(self):
        actor = module.ApplicationAccountWorkflow()
        actor.pacing = dict(policy()['linkedin_applications'])
        actor.pacing.pop('account_id')
        actor.configured = True
        return actor

    def test_duplicate_delayed_and_other_account_signals(self):
        actor, other = self.actor(), self.actor()
        with patch.object(module.workflow, 'info', return_value=SimpleNamespace(workflow_id='account-workflow')):
            actor.provider_restriction(fact())
            actor.provider_restriction(fact())
            self.assertEqual(actor.provider_available_at, 201200)
            self.assertEqual(actor.pacing_revision, 1)
            actor.provider_restriction(fact(delay=None))
            self.assertEqual(actor.provider_available_at, 286400)
        with patch.object(module.workflow, 'info', return_value=SimpleNamespace(workflow_id='other-workflow')), \
                patch.object(module.workflow.logger, 'warning'):
            other.provider_restriction(fact())
            self.assertEqual(other.provider_available_at, 0)

    def test_old_pacing_update_preserves_configured_restriction_delay(self):
        actor = self.actor()
        actor.set_pacing({'spacing_seconds': 600, 'hourly_limit': 4, 'daily_limit': 20,
                          'restriction_retry_seconds': 172800})
        actor.set_pacing({'spacing_seconds': 900, 'hourly_limit': 3, 'daily_limit': 15})
        self.assertEqual(actor.pacing['restriction_retry_seconds'], 172800)
        for invalid in (0, -1, True, math.inf, math.nan):
            with self.assertRaises(ValueError):
                actor.set_pacing({'spacing_seconds': 600, 'hourly_limit': 4, 'daily_limit': 20,
                                  'restriction_retry_seconds': invalid})

    def run_actor(self, actor, command, operation, wait, continuation=None):
        info = SimpleNamespace(workflow_id='account-workflow',
                               is_continue_as_new_suggested=lambda: continuation is not None)
        with patch.object(workflows, 'operation', operation), \
                patch.object(module.workflow, 'wait_condition', wait), \
                patch.object(module.workflow, 'time', return_value=200000.0), \
                patch.object(module.workflow, 'cancellation_reason', return_value=None), \
                patch.object(module.workflow, 'info', return_value=info), \
                patch.object(module.workflow, 'get_external_workflow_handle',
                             return_value=SimpleNamespace(signal=AsyncMock())), \
                patch.object(module.workflow, 'continue_as_new', continuation):
            with self.assertRaises(EndRun):
                asyncio.run(actor.run(command))

    def item(self, attempt):
        return AccountSubmission(ApplicationCommand('r', policy(), 'application', attempt_id=attempt), 'caller')

    def test_signal_wakes_existing_timer(self):
        actor, delays = self.actor(), []
        command = ApplicationAccountCommand(policy(), [self.item('first')], [200000.0], initialized=True)

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if timeout is None:
                raise EndRun()
            delays.append(timeout.total_seconds())
            if len(delays) == 1:
                actor.provider_restriction(fact())
                self.assertTrue(predicate())
            else:
                actor.cancel_attempt('first')

        operation = AsyncMock()
        self.run_actor(actor, command, operation, wait)
        self.assertEqual(delays, [600, 1200])
        operation.assert_not_awaited()

    def test_lost_signal_returns_receipt_before_next_account_dispatch(self):
        actor, delays, sent = self.actor(), [], []
        command = ApplicationAccountCommand(policy(), [self.item('first'), self.item('second')], initialized=True)

        async def operation(command, phase):
            sent.append(command.attempt_id)
            return Observation('confirmed_not_submitted', {'provider_restriction': fact()})

        async def wait(predicate, timeout=None):
            if predicate():
                return
            if timeout is None:
                raise EndRun()
            delays.append(timeout.total_seconds())
            actor.cancel_attempt('second')

        self.run_actor(actor, command, operation, wait)
        self.assertEqual(sent, ['first'])
        self.assertEqual(delays, [1200])

    def test_restriction_survives_continue_as_new(self):
        actor = self.actor()
        command = ApplicationAccountCommand(policy(), [self.item('first')], initialized=True)
        continued = []

        async def wait(predicate, timeout=None):
            self.assertTrue(predicate())

        def continuation(value):
            continued.append(value)
            raise EndRun()

        self.run_actor(actor, command, AsyncMock(return_value=Observation('incomplete', {
            'provider_restriction': fact()})), wait, continuation)
        restored = ApplicationAccountCommand(**json.loads(json.dumps(asdict(continued[0]))))
        self.assertEqual(restored.provider_available_at, 201200)
        self.assertEqual(restored.policy['linkedin_applications']['restriction_retry_seconds'], 86400)

    def test_workflow_cancellation_is_not_swallowed_as_an_attempt_failure(self):
        actor = self.actor()
        command = ApplicationAccountCommand(policy(), [self.item('first')], initialized=True)
        error = ActivityError('cancelled', scheduled_event_id=1, started_event_id=2,
            identity='fixture', activity_type='applypilot.operation.v1', activity_id='fixture', retry_state=None)
        error.__cause__ = CancelledError('Workflow cancelled')

        async def wait(predicate, timeout=None):
            self.assertTrue(predicate())

        with patch.object(workflows, 'operation', AsyncMock(side_effect=error)), \
                patch.object(module.workflow, 'wait_condition', wait), \
                patch.object(module.workflow, 'cancellation_reason', return_value=None), \
                patch.object(module.workflow, 'time', return_value=200000.0), self.assertRaises(ActivityError):
            asyncio.run(actor.run(command))


class ObservationBoundaryChecks(unittest.TestCase):
    def test_quote_must_be_on_current_provider_page(self):
        page = {'url': 'https://www.linkedin.com/jobs/view/123', 'text': 'Please try again tomorrow.'}
        for snapshot, quote, delay in ((page, 'An invented provider message', None),
                ({**page, 'url': 'https://other.example'}, page['text'], None),
                (page, page['text'], math.nan), (page, page['text'], -1)):
            with self.assertRaises(ValueError):
                evidence.capture_restriction('attempt', 1, 'invocation', snapshot, quote, delay)

    def test_persisted_observation_replays_original_timestamp(self):
        page = {'url': 'https://www.linkedin.com/jobs/view/123', 'text': 'Please try again tomorrow.'}
        conn, receipts = MagicMock(), {}
        conn.__enter__.return_value = conn
        binding = {'workflow_id': 'account-workflow', 'account_id': 'account'}
        receipts[evidence.digest(['application-account', 'attempt'])] = binding
        attempt = {'attempt_id': 'attempt', 'channel': 'linkedin-easy-apply', 'target': page['url']}
        with patch('applypilot.database.get_connection', return_value=conn), \
                patch('applypilot.database.close_connection'), \
                patch('applypilot.apply.guard.attempt_status', return_value=attempt), \
                patch('applypilot.apply.browseros_mcp.read_state', return_value={
                    'invocation': 'invocation', 'bound_pages': {'1': page['url']}}), \
                patch.object(evidence.store, 'receipt', side_effect=lambda conn, key: receipts.get(key)), \
                patch.object(evidence.store, 'save_receipt', side_effect=lambda conn, key, value: receipts.update({key: value})), \
                patch.object(evidence.store, 'put', return_value='a' * 64) as persist:
            first = evidence.capture_restriction('attempt', 1, 'invocation', page, page['text'], 3600)
            second = evidence.capture_restriction('attempt', 1, 'invocation', page, page['text'], 3600)
            self.assertEqual(first, second)
            persist.assert_called_once()
            self.assertEqual(persist.call_args.args[1]['observation'], page)
            self.assertEqual(first['retry_after_seconds'], 3600)

    def test_reconciliation_result_replays_undelivered_provider_fact(self):
        from applypilot.apply.service import _result
        with patch.object(evidence, 'restriction_fact', return_value=fact()):
            result = _result('outcome_unknown', {'attempt_id': 'first', 'channel': 'linkedin-easy-apply'})
        self.assertEqual(result['provider_restriction'], fact())


class FakeApplications:
    def __init__(self):
        self.attempts = []

    @activity.defn(name='applypilot.operation.v1')
    async def operation(self, request: ActivityRequest) -> Observation:
        self.attempts.append((request.options['attempt_id'], time.monotonic()))
        return Observation('confirmed_not_submitted')


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeProviderPacingChecks(unittest.IsolatedAsyncioTestCase):
    async def test_account_signal_timer_isolation_and_replay(self):
        client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        settings = {**policy(), 'workspace': uuid4().hex, 'queue_prefix': 'isolated-provider',
                    'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
                    'activities': {'applications': {'heartbeat_interval_seconds': 0.1,
                        'start_to_close_seconds': 30, 'heartbeat_seconds': 2}}}
        queue = lambda role: task_queue(settings['queue_prefix'], settings['workspace'], role)
        fake, handles = FakeApplications(), []
        async with Worker(client, task_queue=queue('workflow'), workflows=[module.ApplicationAccountWorkflow]), \
                Worker(client, task_queue=queue('applications'), activities=[fake.operation]):
            for account in ('first', 'second'):
                handle = await client.start_workflow(module.ApplicationAccountWorkflow.run,
                    ApplicationAccountCommand(settings, initialized=True),
                    id='isolated-provider-' + account + '-' + uuid4().hex, task_queue=queue('workflow'))
                handles.append(handle)
            try:
                observation = fact(workflow_id=handles[0].id, stamp=time.time(), delay=2)
                await handles[0].signal(module.ApplicationAccountWorkflow.provider_restriction, observation)
                await handles[1].signal(module.ApplicationAccountWorkflow.provider_restriction, observation)
                for account, handle in zip(('first', 'second'), handles):
                    await handle.signal(module.ApplicationAccountWorkflow.enqueue, AccountSubmission(
                        ApplicationCommand('fixture', settings, 'job', attempt_id=account), 'missing-fixture-caller'))

                async def until_done():
                    while len(fake.attempts) < 2:
                        await asyncio.sleep(0.02)

                await asyncio.wait_for(until_done(), 10)
                self.assertEqual([item[0] for item in fake.attempts], ['second', 'first'])
                self.assertGreater(fake.attempts[1][1] - fake.attempts[0][1], 0.4)
            finally:
                for handle in handles:
                    await handle.cancel()
                    with self.assertRaises(WorkflowFailureError):
                        await asyncio.wait_for(handle.result(), 10)
        for handle in handles:
            await Replayer(workflows=[module.ApplicationAccountWorkflow]).replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main()

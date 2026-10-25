"""Account timing checks use fake submissions and an isolated Temporal server."""
import asyncio
from copy import deepcopy
from datetime import timedelta
import os
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.converter import DataConverter
from temporalio.worker import Replayer, Worker

from applypilot.orchestration import account_workflows, workflows
from applypilot.orchestration.account_workflows import ApplicationAccountWorkflow
from applypilot.orchestration.contracts import (AccountSubmission, ActivityRequest,
    ApplicationAccountCommand, ApplicationCommand, Observation, digest, task_queue)


def policy():
    return {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-pacing',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {'applications': {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}},
            'linkedin_applications': {'spacing_seconds': 0.4, 'hourly_limit': 100, 'daily_limit': 100,
                                      'restriction_retry_seconds': 86400}}


def submission(settings, attempt):
    return AccountSubmission(ApplicationCommand(digest('options'), settings,
        digest(('application', attempt)), attempt_id=attempt), 'isolated-missing-requester')


class Continued(BaseException):
    def __init__(self, command):
        self.command = command


class Waiting(BaseException):
    def __init__(self, timeout):
        self.timeout = timeout


class AccountContinuationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pacing_usage_and_cancellations_survive_continue_as_new(self):
        settings = policy()
        command = ApplicationAccountCommand(settings, pending=[submission(settings, 'one'), submission(settings, 'two')],
            submissions=[50], initialized=True, cancelled=['cancelled-future'], provider_available_at=95)
        instance = ApplicationAccountWorkflow()
        pacing = {'spacing_seconds': 17, 'hourly_limit': 5, 'daily_limit': 15,
                  'restriction_retry_seconds': 86400}
        instance.set_pacing(pacing)
        async def wait(predicate, **options):
            if options.get('timeout'):
                raise Waiting(options['timeout'])
            self.assertTrue(predicate())
        with patch.object(account_workflows.workflow, 'wait_condition', side_effect=wait), \
                patch.object(account_workflows.workflow, 'cancellation_reason', return_value=None), \
                patch.object(account_workflows.workflow, 'time', return_value=100), \
                patch.object(account_workflows.workflow, 'info',
                    return_value=SimpleNamespace(is_continue_as_new_suggested=lambda: True)), \
                patch.object(account_workflows.workflow, 'get_external_workflow_handle',
                    return_value=SimpleNamespace(signal=AsyncMock())), \
                patch.object(account_workflows.workflow, 'continue_as_new',
                    side_effect=lambda cmd: (_ for _ in ()).throw(Continued(cmd))), \
                patch.object(workflows, 'operation', new=AsyncMock(return_value=Observation('observed'))):
            with self.assertRaises(Continued) as result:
                await instance.run(command)
        encoded = await DataConverter.default.encode([result.exception.command])
        resumed = (await DataConverter.default.decode(encoded, [ApplicationAccountCommand]))[0]
        self.assertEqual(resumed.policy['linkedin_applications'], pacing)
        self.assertEqual(resumed.submissions, [50, 100])
        self.assertEqual(resumed.handled, {'one': 100})
        self.assertEqual(resumed.cancelled, ['cancelled-future'])
        self.assertEqual(resumed.pending[0].command.attempt_id, 'two')
        self.assertEqual(resumed.provider_available_at, 95)
        with patch.object(account_workflows.workflow, 'wait_condition', side_effect=wait), \
                patch.object(account_workflows.workflow, 'cancellation_reason', return_value=None), \
                patch.object(account_workflows.workflow, 'time', return_value=105), \
                patch.object(workflows, 'operation', new=AsyncMock()) as dispatch:
            with self.assertRaises(Waiting) as waiting:
                await ApplicationAccountWorkflow().run(resumed)
            self.assertEqual(waiting.exception.timeout, timedelta(seconds=12))
            dispatch.assert_not_called()


class FakeApplications:
    def __init__(self):
        self.attempts = []

    @activity.defn(name='applypilot.operation.v1')
    async def operation(self, request: ActivityRequest) -> Observation:
        if request.phase != 'application_attempt':
            raise AssertionError('Unexpected phase in isolated account fixture')
        self.attempts.append(request.options['attempt_id'])
        return Observation('observed')


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeAccountTests(unittest.IsolatedAsyncioTestCase):
    async def test_updates_recalculate_waits_and_cancelled_queue_items_consume_no_submission(self):
        client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        settings = policy()
        settings['linkedin_applications']['spacing_seconds'] = 2
        queue = lambda role: task_queue(settings['queue_prefix'], settings['workspace'], role)
        fake = FakeApplications()
        command = ApplicationAccountCommand(settings, pending=[submission(settings, 'one')],
                                             submissions=[time.time()], initialized=True)
        async with Worker(client, task_queue=queue('workflow'), workflows=[ApplicationAccountWorkflow]), \
                Worker(client, task_queue=queue('applications'), activities=[fake.operation]):
            handle = await client.start_workflow(ApplicationAccountWorkflow.run, command,
                id='isolated-account-' + uuid.uuid4().hex, task_queue=queue('workflow'))
            async def status_where(predicate):
                while True:
                    observed = await handle.query(ApplicationAccountWorkflow.status)
                    if predicate(observed):
                        return observed
                    await asyncio.sleep(0.02)
            try:
                await asyncio.wait_for(status_where(lambda state: state['pending'] == 1), 10)
                tightened = {'spacing_seconds': 4, 'hourly_limit': 100, 'daily_limit': 100,
                             'restriction_retry_seconds': 86400}
                self.assertEqual(await handle.execute_update(ApplicationAccountWorkflow.set_pacing, tightened), tightened)
                await asyncio.sleep(2.2)
                self.assertEqual(fake.attempts, [])
                loosened = {**tightened, 'spacing_seconds': 0.01}
                await handle.execute_update(ApplicationAccountWorkflow.set_pacing, loosened)
                await asyncio.wait_for(status_where(lambda state: state['pending'] == 0 and not state['active_attempt']), 10)
                self.assertEqual(fake.attempts, ['one'])
                await handle.execute_update(ApplicationAccountWorkflow.set_pacing,
                    {**loosened, 'hourly_limit': 1})
                await handle.signal(ApplicationAccountWorkflow.enqueue, submission(settings, 'cancelled'))
                await asyncio.wait_for(status_where(lambda state: state['pending'] == 1), 10)
                await handle.signal(ApplicationAccountWorkflow.cancel_attempt, 'cancelled')
                await asyncio.wait_for(status_where(lambda state: state['pending'] == 0), 10)
                self.assertEqual(fake.attempts, ['one'])
                await handle.execute_update(ApplicationAccountWorkflow.set_pacing, loosened)
                await handle.signal(ApplicationAccountWorkflow.enqueue, submission(settings, 'two'))
                await asyncio.wait_for(status_where(lambda state: fake.attempts == ['one', 'two']
                    and not state['active_attempt']), 10)
                self.assertEqual((await handle.query(ApplicationAccountWorkflow.status))['pacing'], loosened)
            finally:
                await handle.cancel()
                with self.assertRaises(WorkflowFailureError):
                    await asyncio.wait_for(handle.result(), 10)
        await Replayer(workflows=[ApplicationAccountWorkflow]).replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main()

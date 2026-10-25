"""Native prediction retry/continuation/replay; explicitly isolated Temporal only."""
import asyncio
from contextlib import AsyncExitStack
import os
import unittest
import uuid

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from applypilot.orchestration.contracts import ActivityRequest, digest, task_queue
from applypilot.orchestration.prediction_contracts import PredictionCommand
from applypilot.orchestration.prediction_workflows import PredictiveCompanyWorkflow


class PredictionFixture:
    def __init__(self):
        self.preparations = 0
        self.reads = []
        self.searches = []
        self.vacancies = []
        self.models = []

    @activity.defn(name='applypilot.prediction_prepare.v1')
    async def prepare(self, request: ActivityRequest) -> dict:
        self.preparations += 1
        return {'reference': digest('business'), 'homepage': digest('homepage'), 'vacancies': [
            {'reference': digest(('page', i)), 'identity': str(i), 'depth': 0} for i in range(80)]}

    @activity.defn(name='applypilot.prediction_read.v1')
    async def read(self, request: ActivityRequest) -> dict:
        self.reads.append((request.options['page_reference'], request.options['backend'], request.options['request_reference']))
        if request.options['backend'] == 'http':
            return {'challenge_reference': digest('actual-challenge')}
        if activity.info().attempt == 1:
            raise RuntimeError('Unclassified transient solver failure')
        page = request.options['page_reference']
        return {'identities': [page, page], 'pages': [page], 'queries': [digest('private-query')], 'document': True}

    @activity.defn(name='applypilot.prediction_search.v1')
    async def search(self, request: ActivityRequest) -> dict:
        self.searches.append(request.options['provider'])
        return {'pages': [digest('homepage')]}

    @activity.defn(name='applypilot.prediction_vacancy.v1')
    async def vacancy(self, request: ActivityRequest) -> dict:
        self.vacancies.append(request.options['page_reference'])
        return {'reference': request.reference, 'pages': []}

    @activity.defn(name='applypilot.prediction_analyze.v1')
    async def analyze(self, request: ActivityRequest) -> dict:
        self.models.append('analyze')
        return {'reference': request.reference}

    @activity.defn(name='applypilot.prediction_audit.v1')
    async def audit(self, request: ActivityRequest) -> dict:
        self.models.append('audit')
        return {'reference': request.reference}

    @activity.defn(name='applypilot.prediction_publish.v1')
    async def publish(self, request: ActivityRequest) -> dict:
        return {'state': 'audited'}


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativePredictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_observed_challenge_native_retry_continuation_and_history_replay(self):
        client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        policy = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-check',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('maintenance', 'research', 'discovery', 'models')}}
        queue = lambda role: task_queue(policy['queue_prefix'], policy['workspace'], role)
        fixture = PredictionFixture()
        workflow_id = 'isolated-prediction-' + uuid.uuid4().hex
        async with AsyncExitStack() as stack:
            for role, activities in [('maintenance', [fixture.prepare, fixture.publish]),
                    ('research', [fixture.read, fixture.search]), ('discovery', [fixture.vacancy]),
                    ('models', [fixture.analyze, fixture.audit])]:
                await stack.enter_async_context(Worker(client, task_queue=queue(role), activities=activities))
            await stack.enter_async_context(Worker(client, task_queue=queue('workflow'),
                workflows=[PredictiveCompanyWorkflow]))
            handle = await client.start_workflow(PredictiveCompanyWorkflow.run,
                PredictionCommand(digest('root'), policy), id=workflow_id, task_queue=queue('workflow'))
            self.assertEqual(await asyncio.wait_for(handle.result(), 60), {'state': 'audited'})
        self.assertEqual(fixture.preparations, 1)
        self.assertEqual(fixture.reads, [(digest('homepage'), 'http', ''),
            (digest('homepage'), 'flaresolverr', digest('actual-challenge')),
            (digest('homepage'), 'flaresolverr', digest('actual-challenge'))])
        self.assertEqual(fixture.searches, ['searxng', 'ddgs'])
        self.assertEqual(len(fixture.vacancies), 80)
        self.assertEqual(len(set(fixture.vacancies)), 80)
        self.assertEqual(fixture.models, ['analyze', 'audit'])
        first = await client.get_workflow_handle(workflow_id, run_id=handle.first_execution_run_id).fetch_history()
        self.assertTrue(any(event.HasField('workflow_execution_continued_as_new_event_attributes') for event in first.events))
        replayer = Replayer(workflows=[PredictiveCompanyWorkflow])
        await replayer.replay_workflow(first)
        await replayer.replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main()

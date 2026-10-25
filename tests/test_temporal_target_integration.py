"""Target traversal runs on an explicitly isolated server using fake Activities."""
import asyncio
from contextlib import AsyncExitStack
import os
import unittest
import uuid
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker
from applypilot.orchestration.contracts import ActivityRequest, digest, task_queue
from applypilot.orchestration.target_contracts import TargetCommand
from applypilot.orchestration.target_workflows import TargetWorkflow


class TargetObservations:
    def __init__(self):
        self.pages = [digest(index) for index in range(40)]
        self.reads, self.vacancies, self.publications = [], [], []
        self.preparations = self.search_attempts = self.completions = 0

    @activity.defn(name='applypilot.target_prepare.v1')
    async def prepare(self, request: ActivityRequest) -> dict:
        self.preparations += 1
        return {'reference': digest('prepared'), 'query_reference': digest('query'),
                'read_references': [digest('home')], 'needs_research': True, 'pages': []}

    @activity.defn(name='applypilot.target_search.v1')
    async def search(self, request: ActivityRequest) -> dict:
        self.search_attempts += 1
        if request.options['provider'] == 'searxng' and activity.info().attempt == 1:
            raise RuntimeError('Previously unseen target search failure')
        return {'sources': [digest(request.options['provider'])], 'pages': [digest('home')]}

    @activity.defn(name='applypilot.target_read.v1')
    async def read(self, request: ActivityRequest) -> dict:
        self.reads.append(request.options['backend'])
        if request.options['backend'] == 'http':
            return {'challenge': True}
        return {'source_reference': digest('document'), 'final_reference': digest('home')}

    @activity.defn(name='applypilot.target_synthesize.v1')
    async def synthesize(self, request: ActivityRequest) -> dict:
        return {'reference': digest('evidence'), 'outreach_only': False,
                'pages': [{'reference': page, 'identity': page} for page in self.pages]}

    @activity.defn(name='applypilot.target_vacancy.v1')
    async def vacancy(self, request: ActivityRequest) -> dict:
        reference = request.options['page_reference']
        self.vacancies.append(reference)
        return {'reference': reference, 'pages': [], 'found': 1, 'coverage_complete': True}

    @activity.defn(name='applypilot.target_publish.v1')
    async def publish(self, request: ActivityRequest) -> dict:
        self.publications.append(request.options['reference'])
        return {'new': 1}

    @activity.defn(name='applypilot.target_complete.v1')
    async def complete(self, request: ActivityRequest) -> dict:
        self.completions += 1
        return {'state': 'exhausted'}


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeTargetTests(unittest.IsolatedAsyncioTestCase):
    async def test_target_retry_solver_continuation_and_replay(self):
        client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        policy = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-target',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('maintenance', 'research', 'models', 'discovery')}}
        queue = lambda role: task_queue(policy['queue_prefix'], policy['workspace'], role)
        fake = TargetObservations()
        async with AsyncExitStack() as stack:
            for role, functions in {'maintenance': [fake.prepare, fake.publish, fake.complete],
                    'research': [fake.search, fake.read], 'models': [fake.synthesize], 'discovery': [fake.vacancy]}.items():
                await stack.enter_async_context(Worker(client, task_queue=queue(role), activities=functions))
            await stack.enter_async_context(Worker(client, task_queue=queue('workflow'), workflows=[TargetWorkflow]))
            handle = await client.start_workflow(TargetWorkflow.run, TargetCommand(digest('input'), policy),
                id='isolated-target-' + uuid.uuid4().hex, task_queue=queue('workflow'))
            result = await asyncio.wait_for(handle.result(), 45)
        self.assertEqual(result.state, 'exhausted')
        self.assertEqual(result.detail['found'], 40)
        self.assertEqual(result.detail['new'], 40)
        self.assertEqual(fake.reads, ['http', 'flaresolverr'])
        self.assertEqual(fake.vacancies, fake.pages)
        self.assertEqual(fake.publications, fake.pages)
        self.assertEqual((fake.preparations, fake.search_attempts, fake.completions), (1, 3, 1))
        first = await client.get_workflow_handle(handle.id, run_id=handle.first_execution_run_id).fetch_history()
        self.assertTrue(any(event.HasField('workflow_execution_continued_as_new_event_attributes') for event in first.events))
        replayer = Replayer(workflows=[TargetWorkflow])
        await replayer.replay_workflow(first)
        await replayer.replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main()

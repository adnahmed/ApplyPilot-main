"""Production registrations and Jobseek traversal on a disposable Temporal server."""
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import AsyncExitStack
import os
import sys
from types import ModuleType
import unittest
from unittest.mock import patch
import uuid

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from applypilot.orchestration.company_contracts import CompanyEvidenceCommand
from applypilot.orchestration.company_workflows import CompanyEvidenceWorkflow
from applypilot.orchestration.contracts import ActivityRequest, digest, task_queue
from applypilot.orchestration.worker import ROLES, activities_for, workflow_types


class JobseekObservations:
    def __init__(self):
        self.board = digest('board')
        self.urls = ['https://example.test/jobs/' + str(index) for index in range(70)]
        self.pages = {digest(url): url for url in self.urls}
        self.observations = []
        self.visits = Counter()
        self.inventory_calls = self.publications = 0

    @activity.defn(name='applypilot.company_read.v1')
    async def read(self, request: ActivityRequest) -> dict:
        return {'reference': request.reference}

    @activity.defn(name='applypilot.company_assess.v1')
    async def assess(self, request: ActivityRequest) -> dict:
        return {'reference': request.reference,
                'pages': [{'reference': self.board, 'identity': self.board}]}

    @activity.defn(name='applypilot.company_vacancy.v1')
    async def vacancy(self, request: ActivityRequest) -> dict:
        reference = request.options['page_reference']
        self.visits[reference] += 1
        if reference == self.board:
            self.inventory_calls += 1
            self.observations.append({'jobseek_inventory': {
                'id': 'inventory-1', 'urls': self.urls, 'scope': 'jobseek:engineering',
                'source_url': 'https://example.test/jobs', 'observed_at': '2026-10-01T00:00:00Z',
                'authoritative': True, 'partial': False, 'intent_fingerprint': 'fixture'}})
            pages = [{'reference': page, 'identity': page} for page in self.pages]
            return {'reference': request.reference, 'pages': pages}
        if reference == digest(self.urls[25]) and activity.info().attempt == 1:
            raise RuntimeError('An unseen Jobseek posting response')
        self.observations.append({'jobseek_posting': {'inventory_id': 'inventory-1',
            'url': self.pages[reference], 'postings': [{'url': self.pages[reference],
                'title': 'Engineer', 'text': 'Engineering vacancy', 'relevant': True}]}})
        return {'reference': request.reference, 'pages': []}

    @activity.defn(name='applypilot.company_publish.v1')
    async def publish(self, request: ActivityRequest) -> dict:
        self.publications += 1
        transport = ModuleType('applypilot.integrations.net')
        transport.public_url = lambda *args, **kwargs: self.fail_unexpected_network()
        with patch.dict(sys.modules, {'applypilot.integrations.net': transport}):
            from applypilot.discovery.jobseek import assemble_hiring_observations
        return assemble_hiring_observations(self.observations)[0]['hiring_snapshot']

    def fail_unexpected_network(self):
        raise AssertionError('Snapshot assembly must not call the network transport')


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'),
                     'An isolated Temporal server is required')
class NativeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        self.policy = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-runtime',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('research', 'models', 'discovery', 'maintenance')}}

    def queue(self, role):
        return task_queue(self.policy['queue_prefix'], self.policy['workspace'], role)

    async def test_every_production_capability_starts_with_its_real_registration(self):
        async with AsyncExitStack() as stack:
            for role in ROLES:
                options = {'task_queue': self.queue(role)}
                if role == 'workflow':
                    options['workflows'] = workflow_types()
                else:
                    executor = stack.enter_context(ThreadPoolExecutor(max_workers=1))
                    options.update(activities=activities_for(role), activity_executor=executor,
                                   max_concurrent_activities=1)
                worker = await stack.enter_async_context(Worker(self.client, **options))
                async def started():
                    while not worker.is_running:
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(started(), 10)
                self.assertTrue(worker.is_running)

    async def test_jobseek_inventory_and_independent_postings_retry_continue_and_replay(self):
        fake = JobseekObservations()
        async with AsyncExitStack() as stack:
            for role, functions in {'research': [fake.read], 'models': [fake.assess],
                    'discovery': [fake.vacancy], 'maintenance': [fake.publish]}.items():
                await stack.enter_async_context(Worker(self.client, task_queue=self.queue(role), activities=functions))
            await stack.enter_async_context(Worker(self.client, task_queue=self.queue('workflow'),
                workflows=workflow_types()))
            handle = await self.client.start_workflow(CompanyEvidenceWorkflow.run,
                CompanyEvidenceCommand(digest('company'), self.policy),
                id='isolated-jobseek-' + uuid.uuid4().hex, task_queue=self.queue('workflow'))
            result = await asyncio.wait_for(handle.result(), 45)
        self.assertTrue(result['complete'])
        self.assertEqual(result['total'], 70)
        self.assertEqual(result['relevant'], 70)
        self.assertEqual({row['url'] for row in result['postings']}, set(fake.urls))
        self.assertEqual((fake.inventory_calls, fake.publications), (1, 1))
        self.assertEqual(fake.visits.pop(digest(fake.urls[25])), 2)
        self.assertEqual(set(fake.visits.values()), {1})
        first = await self.client.get_workflow_handle(handle.id, run_id=handle.first_execution_run_id).fetch_history()
        self.assertTrue(any(event.HasField('workflow_execution_continued_as_new_event_attributes')
                            for event in first.events))
        replayer = Replayer(workflows=workflow_types())
        await replayer.replay_workflow(first)
        await replayer.replay_workflow(await handle.fetch_history())


if __name__ == '__main__':
    unittest.main()

"""Real server source-to-evidence traversal with only fixture Activities."""
import asyncio
from contextlib import AsyncExitStack
import os
import unittest
import uuid

from temporalio import activity
from temporalio.client import Client
from temporalio.worker import Replayer, Worker

from applypilot.orchestration.company_contracts import CompanySourceCommand
from applypilot.orchestration.company_workflows import CompanySourceWorkflow, CompanyEvidenceWorkflow
from applypilot.orchestration.contracts import ActivityRequest, digest, identity, task_queue


class Research:
    def __init__(self):
        self.sources = [digest(('source', index)) for index in range(20)]
        self.vacancies = [digest(('vacancy', index)) for index in range(70)]
        self.source_reads, self.source_publications, self.vacancy_reads = [], [], []
        self.company_reads = self.company_assessments = self.search_attempts = 0
        self.company_done = asyncio.Event()

    @activity.defn(name='applypilot.company_source_read.v1')
    async def source_read(self, request: ActivityRequest) -> dict:
        self.source_reads.append(request.reference)
        return {'reference': request.reference, 'identities': [request.reference],
                'issuers': [digest('issuer')] if request.reference == self.sources[0] else []}

    @activity.defn(name='applypilot.company_source_enrich.v1')
    async def source_enrich(self, request: ActivityRequest) -> dict:
        if request.options['operation'] == 'search':
            self.search_attempts += 1
            if activity.info().attempt == 1:
                raise RuntimeError('Previously unseen fixture search failure')
            return {'candidates': [digest('homepage')]}
        return {'reference': self.sources[0], 'identified': True}

    @activity.defn(name='applypilot.company_source_extract.v1')
    async def source_extract(self, request: ActivityRequest) -> dict:
        index = self.sources.index(request.reference)
        following = self.sources[index + 1] if index + 1 < len(self.sources) else None
        return {'reference': request.reference, 'details': [],
                'companies': [{'reference': digest('company'), 'subject': digest('company-subject')}] if index == 0 else [],
                'continuation': {'reference': following, 'identity': following} if following else None}

    @activity.defn(name='applypilot.company_source_publish.v1')
    async def source_publish(self, request: ActivityRequest) -> dict:
        self.source_publications.append(request.reference)
        return {'state': 'published'}

    @activity.defn(name='applypilot.company_read.v1')
    async def company_read(self, request: ActivityRequest) -> dict:
        self.company_reads += 1
        return {'reference': request.reference}

    @activity.defn(name='applypilot.company_assess.v1')
    async def company_assess(self, request: ActivityRequest) -> dict:
        self.company_assessments += 1
        return {'reference': request.reference,
                'pages': [{'reference': self.vacancies[0], 'identity': self.vacancies[0], 'depth': 0}]}

    @activity.defn(name='applypilot.company_vacancy.v1')
    async def company_vacancy(self, request: ActivityRequest) -> dict:
        reference = request.options['page_reference']
        self.vacancy_reads.append(reference)
        index = self.vacancies.index(reference)
        following = self.vacancies[index + 1] if index + 1 < len(self.vacancies) else None
        return {'reference': request.reference,
                'pages': [{'reference': following, 'identity': following, 'depth': 0}] if following else []}

    @activity.defn(name='applypilot.company_publish.v1')
    async def company_publish(self, request: ActivityRequest) -> dict:
        self.company_done.set()
        return {'state': 'published'}


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeCompanyResearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_source_and_child_evidence_continue_and_replay_without_repeating_observations(self):
        client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        settings = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-research',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('maintenance', 'research', 'models', 'discovery')}}
        queue = lambda role: task_queue(settings['queue_prefix'], settings['workspace'], role)
        fake = Research()
        registrations = {'research': [fake.source_read, fake.source_enrich, fake.company_read],
            'models': [fake.source_extract, fake.company_assess], 'discovery': [fake.company_vacancy],
            'maintenance': [fake.source_publish, fake.company_publish]}
        async with AsyncExitStack() as stack:
            for role, functions in registrations.items():
                await stack.enter_async_context(Worker(client, task_queue=queue(role), activities=functions))
            await stack.enter_async_context(Worker(client, task_queue=queue('workflow'),
                workflows=[CompanySourceWorkflow, CompanyEvidenceWorkflow]))
            source = await client.start_workflow(CompanySourceWorkflow.run,
                CompanySourceCommand(fake.sources[0], settings), id='source-' + uuid.uuid4().hex,
                task_queue=queue('workflow'))
            result = await asyncio.wait_for(source.result(), 45)
            await asyncio.wait_for(fake.company_done.wait(), 45)
            evidence = client.get_workflow_handle(identity(settings['workspace'], 'company-evidence', digest('company-subject')))
            self.assertEqual(await asyncio.wait_for(evidence.result(), 15), {'state': 'published'})
        self.assertEqual(result, {'state': 'published', 'companies': 1})
        self.assertEqual(fake.source_reads, fake.sources)
        self.assertEqual(fake.source_publications, fake.sources)
        self.assertEqual(fake.vacancy_reads, fake.vacancies)
        self.assertEqual((fake.company_reads, fake.company_assessments, fake.search_attempts), (1, 1, 2))
        source_first = await client.get_workflow_handle(source.id, run_id=source.first_execution_run_id).fetch_history()
        evidence_last = await evidence.fetch_history()
        evidence_first_id = evidence_last.events[0].workflow_execution_started_event_attributes.first_execution_run_id
        evidence_first = await client.get_workflow_handle(evidence.id, run_id=evidence_first_id).fetch_history()
        for history in (source_first, evidence_first):
            self.assertTrue(any(event.HasField('workflow_execution_continued_as_new_event_attributes')
                                for event in history.events))
        replayer = Replayer(workflows=[CompanySourceWorkflow, CompanyEvidenceWorkflow])
        for history in (source_first, await source.fetch_history(), evidence_first, evidence_last):
            await replayer.replay_workflow(history)


if __name__ == '__main__':
    unittest.main()

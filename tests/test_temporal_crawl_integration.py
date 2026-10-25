"""Native SDK/server checks against an explicitly supplied isolated server only."""
import asyncio
from contextlib import AsyncExitStack
from datetime import timedelta
import os
import unittest
import uuid

from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.exceptions import CancelledError
from temporalio.worker import Replayer, Worker

from applypilot.orchestration.company_contracts import CompanyCrawlCommand
from applypilot.orchestration.company_workflows import CompanyCrawlWorkflow
from applypilot.orchestration.contracts import ActivityRequest, digest, task_queue


class Pages:
    def __init__(self, *, count=1, fail_first=False, hold=False):
        self.count, self.fail_first, self.hold = count, fail_first, hold
        self.preparations = 0
        self.attempts = {}
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    @activity.defn(name='applypilot.company_crawl_prepare.v1')
    async def prepare(self, request: ActivityRequest) -> dict:
        self.preparations += 1
        return dict(root_reference=digest('root'), backend='http', allow_browseros=False,
                    max_pages=self.count, max_depth=1)

    @activity.defn(name='applypilot.company_crawl_page.v1')
    async def page(self, request: ActivityRequest) -> dict:
        self.attempts[request.reference] = self.attempts.get(request.reference, 0) + 1
        if self.fail_first and request.reference == digest('root') and activity.info().attempt == 1:
            raise RuntimeError('An unclassified provider failure')
        self.started.set()
        if self.hold:
            try:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(0.05)
            finally:
                self.cancelled.set()
        return {'document_reference': digest(('document', request.reference)),
                'final_reference': request.reference, 'thin': False,
                'links': [digest(index) for index in range(self.count - 1)]
                    if request.reference == digest('root') else []}

    @activity.defn(name='applypilot.company_crawl_publish.v1')
    async def publish(self, request: ActivityRequest) -> dict:
        return request.options


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeCrawlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        self.policy = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-check',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('maintenance', 'research')}}
        self.workflow_id = 'isolated-crawl-' + uuid.uuid4().hex

    def queue(self, role):
        return task_queue(self.policy['queue_prefix'], self.policy['workspace'], role)

    def workflows(self):
        return Worker(self.client, task_queue=self.queue('workflow'), workflows=[CompanyCrawlWorkflow],
                      max_cached_workflows=0)

    async def start(self):
        return await self.client.start_workflow(CompanyCrawlWorkflow.run,
            CompanyCrawlCommand(digest('options'), self.policy), id=self.workflow_id,
            task_queue=self.queue('workflow'))

    async def activities(self, stack, pages, *, research=True):
        await stack.enter_async_context(Worker(self.client, task_queue=self.queue('maintenance'),
            activities=[pages.prepare, pages.publish]))
        if research:
            await stack.enter_async_context(Worker(self.client, task_queue=self.queue('research'),
                activities=[pages.page], max_heartbeat_throttle_interval=timedelta(milliseconds=50)))

    async def test_native_unknown_retry_continue_as_new_and_history_replay(self):
        pages = Pages(count=71, fail_first=True)
        async with AsyncExitStack() as stack:
            await self.activities(stack, pages)
            await stack.enter_async_context(self.workflows())
            handle = await self.start()
            result = await asyncio.wait_for(handle.result(), 45)
        self.assertTrue(result['complete'])
        self.assertEqual(len(result['documents']), 71)
        self.assertEqual(pages.preparations, 1)
        self.assertEqual(pages.attempts.pop(digest('root')), 2)
        self.assertEqual(set(pages.attempts.values()), {1})
        first = await self.client.get_workflow_handle(self.workflow_id,
            run_id=handle.first_execution_run_id).fetch_history()
        self.assertTrue(any(event.HasField('workflow_execution_continued_as_new_event_attributes')
                            for event in first.events))
        replayer = Replayer(workflows=[CompanyCrawlWorkflow])
        await replayer.replay_workflow(first)
        await replayer.replay_workflow(await handle.fetch_history())

    async def test_worker_restart_resumes_scheduled_page_without_repreparing(self):
        pages = Pages()
        async with AsyncExitStack() as stack:
            await self.activities(stack, pages, research=False)
            async with self.workflows():
                handle = await self.start()
                async def scheduled():
                    while True:
                        history = await handle.fetch_history()
                        if any(event.HasField('activity_task_scheduled_event_attributes') and
                            event.activity_task_scheduled_event_attributes.activity_type.name ==
                                'applypilot.company_crawl_page.v1' for event in history.events):
                            return
                        await asyncio.sleep(0.05)
                await asyncio.wait_for(scheduled(), 15)
            async with self.workflows(), Worker(self.client, task_queue=self.queue('research'), activities=[pages.page]):
                result = await asyncio.wait_for(handle.result(), 15)
        self.assertTrue(result['complete'])
        self.assertEqual(pages.preparations, 1)
        self.assertEqual(pages.attempts, {digest('root'): 1})

    async def test_cancellation_reaches_the_inflight_activity(self):
        pages = Pages(hold=True)
        async with AsyncExitStack() as stack:
            await self.activities(stack, pages)
            await stack.enter_async_context(self.workflows())
            handle = await self.start()
            await asyncio.wait_for(pages.started.wait(), 15)
            await handle.cancel()
            with self.assertRaises(WorkflowFailureError) as failed:
                await asyncio.wait_for(handle.result(), 15)
            self.assertIsInstance(failed.exception.cause, CancelledError)
            await asyncio.wait_for(pages.cancelled.wait(), 15)


if __name__ == '__main__':
    unittest.main()

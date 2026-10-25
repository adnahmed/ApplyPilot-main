"""Current application, mailbox, learning and Schedule contracts on an isolated server."""
import asyncio
from collections import Counter
from contextlib import AsyncExitStack
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import uuid

from temporalio import activity
from temporalio.client import Client, ScheduleOverlapPolicy
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Replayer, Worker

from applypilot.orchestration import client as controls
from applypilot.orchestration.contracts import ActivityRequest, ApplicationCommand, Command, Observation, digest, identity, task_queue
from applypilot.orchestration.learning_workflows import LearningWorkflow, LearningRunWorkflow
from applypilot.orchestration.mailbox_workflows import MAILBOX_WORKFLOWS, MailboxSyncWorkflow
from applypilot.orchestration.settings import load
from applypilot.orchestration.utility_workflows import UtilityWorkflow
from applypilot.orchestration.workflows import ApplicationWorkflow


class Applications:
    def __init__(self):
        self.observations = self.preparations = self.effects = 0
        self.attempts = []

    @activity.defn(name='applypilot.operation.v1')
    async def operation(self, request: ActivityRequest) -> Observation:
        if request.phase == 'application_evidence':
            self.observations += 1
            if self.observations == 1:
                return Observation('not_started')
            if self.observations == 2:
                return Observation('prepared', {'attempt_id': self.attempts[0], 'channel': 'external'})
            return Observation('submitted', {'attempt_id': self.attempts[0]})
        if request.phase == 'application_prepare':
            self.preparations += 1
            return Observation('prepared', {'channel': 'external'})
        if request.phase == 'application_attempt':
            self.attempts.append(request.options['attempt_id'])
            if not self.effects:
                self.effects += 1
                raise RuntimeError('Response lost after the provider accepted the application')
            return Observation('reconciling')
        raise AssertionError(request.phase)


class Mailbox:
    def __init__(self):
        self.pages = []
        self.fetches = Counter()
        self.published, self.documents = [], []
        self.references = {name: digest(name) for name in ('recent', 'history', 'backlog', 'reply', 'next-reply', 'old-reply')}

    @activity.defn(name='applypilot.operation.v1')
    async def operation(self, request: ActivityRequest) -> Observation:
        phase = request.phase
        if phase == 'mailbox_begin':
            return Observation('ready', {'account': 'fixture-account',
                'windows': [{'reference': self.references[lane], 'lane': lane} for lane in ('recent', 'history')],
                'backlog': self.references['backlog']})
        if phase == 'mailbox_page':
            self.pages.append((request.reference, request.cursor, request.sequence))
            recent = request.reference == self.references['recent']
            name = ('next-reply' if request.cursor else 'reply') if recent else 'old-reply'
            cursor = 'next-page' if recent and request.cursor is None else None
            return Observation('observed', {'messages': [{'reference': self.references[name], 'subject': name}]}, cursor)
        if phase == 'mailbox_pending_page':
            return Observation('observed', {'messages': []})
        if phase == 'mailbox_fetch_message':
            self.fetches[request.reference] += 1
            if request.reference == self.references['reply'] and activity.info().attempt == 1:
                raise RuntimeError('Previously unseen mailbox response')
            return Observation('fetched')
        if phase == 'mailbox_classify_message':
            return Observation('classified', {'reference': request.reference})
        if phase == 'mailbox_publish_message':
            self.published.append(request.reference)
            return Observation('processed')
        if phase == 'mailbox_tracking_document':
            self.documents.append(request.reference)
            return Observation('written')
        if phase in {'mailbox_observations', 'mailbox_outcome_model'}:
            return Observation('complete')
        raise AssertionError(phase)


class Learning:
    def __init__(self):
        self.collects = 0
        self.phases = {'healthy': [], 'recovering': []}
        self.collection = digest('collection')
        self.references = {name: digest(name) for name in self.phases}
        self.release_recovering = asyncio.Event()
        self.retry_started = asyncio.Event()

    @activity.defn(name='applypilot.operation.v1')
    async def operation(self, request: ActivityRequest) -> Observation:
        if request.phase == 'learning_collect':
            self.collects += 1
            return Observation('collected', {'reference': self.collection})
        if request.phase == 'learning_build_dataset' and request.reference == self.collection:
            if request.sequence == 2:
                return Observation('complete')
            name = ('recovering', 'healthy')[request.sequence]
            return Observation('cohort', {'reference': self.references[name], 'program_scope': name})
        name = next(name for name, reference in self.references.items() if reference == request.reference)
        self.phases[name].append(request.phase)
        if request.phase == 'learning_build_dataset':
            return Observation('dataset_ready', {'reference': request.reference,
                **{key: name + '-' + key for key in ('learning_run_id', 'dataset_id', 'program_id', 'optimizer_run_id')}})
        if request.phase == 'learning_optimize' and name == 'recovering':
            if activity.info().attempt == 1:
                raise RuntimeError('Previously unseen optimizer provider response')
            self.retry_started.set()
            while not self.release_recovering.is_set():
                activity.heartbeat()
                await asyncio.sleep(0.02)
        return Observation('promoted' if request.phase == 'learning_publish' else 'evaluated')


class Referrals:
    def __init__(self):
        self.calls = []

    @activity.defn(name='applypilot.utility.referral_discovery.v1')
    async def discovery(self, request: ActivityRequest) -> Observation:
        provider_operation = request.options['discovery_operation']
        self.calls.append((provider_operation, request.sequence))
        if provider_operation == 'connections':
            return Observation('no_results')
        if activity.info().attempt == 1:
            raise RuntimeError('Previously unseen referral provider response')
        return Observation('discovered', {'reference': digest('contacts')})


class ReferralPreparation:
    def __init__(self):
        self.selection = digest('selected-referral-cases')
        self.inventories = self.finishes = 0
        self.prepared, self.revised = [], []

    @activity.defn(name='applypilot.utility.referral_inventory.v1')
    async def inventory(self, request: ActivityRequest) -> Observation:
        self.inventories += 1
        return Observation('selected', {'selection_reference': self.selection})

    @activity.defn(name='applypilot.utility.referral_case_prepare.v1')
    async def prepare(self, request: ActivityRequest) -> Observation:
        if request.reference != self.selection:
            raise AssertionError('A continuation lost the selected case inventory')
        self.prepared.append(request.sequence)
        return Observation('complete' if request.sequence == 2 else 'prepared')

    @activity.defn(name='applypilot.utility.referral_case_revise.v1')
    async def revise(self, request: ActivityRequest) -> Observation:
        self.revised.append(request.sequence)
        if request.sequence == 0 and activity.info().attempt == 1:
            raise RuntimeError('Previously unseen model revision response')
        return Observation('revised')

    @activity.defn(name='applypilot.utility.referral_prepare_finish.v1')
    async def finish(self, request: ActivityRequest) -> Observation:
        self.finishes += 1
        return Observation('prepared', {'cases': 2})


@unittest.skipUnless(os.getenv('APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'), 'An isolated Temporal server is required')
class NativeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = await Client.connect(os.environ['APPLYPILOT_ISOLATED_TEMPORAL_ADDRESS'])
        self.policy = {'workspace': uuid.uuid4().hex, 'queue_prefix': 'isolated-lifecycle',
            'activity_retry_initial_seconds': 0.1, 'activity_retry_maximum_seconds': 1,
            'evidence_poll_seconds': 0.02, 'evidence_observations_per_run': 1,
            'pages_per_run': 1, 'discovery_pages_per_run': 1,
            'activities': {role: {'heartbeat_interval_seconds': 0.1,
                'start_to_close_seconds': 30, 'heartbeat_seconds': 2}
                for role in ('applications', 'maintenance', 'email', 'models', 'documents', 'learning', 'network')}}

    def queue(self, role):
        return task_queue(self.policy['queue_prefix'], self.policy['workspace'], role)

    async def workers(self, stack, fixture, roles, workflows):
        for role in roles:
            await stack.enter_async_context(Worker(self.client, task_queue=self.queue(role), activities=[fixture.operation]))
        await stack.enter_async_context(Worker(self.client, task_queue=self.queue('workflow'), workflows=workflows))

    async def replay(self, handle, workflows):
        latest = await handle.fetch_history()
        run = latest.events[0].workflow_execution_started_event_attributes.first_execution_run_id
        replay = Replayer(workflows=workflows)
        count = 0
        while run:
            history = await self.client.get_workflow_handle(handle.id, run_id=run).fetch_history()
            await replay.replay_workflow(history)
            count += 1
            run = next((event.workflow_execution_continued_as_new_event_attributes.new_execution_run_id
                for event in history.events if event.HasField('workflow_execution_continued_as_new_event_attributes')), '')
        return count

    async def test_lost_application_response_keeps_one_dispatch_identity_through_reconciliation(self):
        fake = Applications()
        async with AsyncExitStack() as stack:
            await self.workers(stack, fake, ('applications',), [ApplicationWorkflow])
            handle = await self.client.start_workflow(ApplicationWorkflow.run,
                ApplicationCommand(digest('input'), self.policy, digest('application')),
                id='isolated-application-' + uuid.uuid4().hex, task_queue=self.queue('workflow'))
            self.assertEqual((await asyncio.wait_for(handle.result(), 20))['state'], 'submitted')
        self.assertEqual((fake.effects, fake.preparations, fake.observations), (1, 1, 3))
        self.assertEqual(len(fake.attempts), 2)
        self.assertEqual(len(set(fake.attempts)), 1)
        self.assertEqual(await self.replay(handle, [ApplicationWorkflow]), 3)

    async def test_mailbox_sync_windows_continue_with_cursor_and_independent_message_children(self):
        fake = Mailbox()
        async with AsyncExitStack() as stack:
            await self.workers(stack, fake, ('email', 'maintenance', 'models', 'documents', 'learning'), MAILBOX_WORKFLOWS)
            handle = await self.client.start_workflow(MailboxSyncWorkflow.run,
                Command(digest('input'), self.policy, 'tracking'), id='isolated-mailbox-' + uuid.uuid4().hex,
                task_queue=self.queue('workflow'))
            self.assertEqual(await asyncio.wait_for(handle.result(), 20), {'state': 'accepted', 'started': 4})
            windows = [self.client.get_workflow_handle(identity(self.policy['workspace'], 'mailbox-window',
                        'fixture-account:' + lane)) for lane in ('recent', 'history')]
            messages = [self.client.get_workflow_handle(identity(self.policy['workspace'], 'mailbox-message', name))
                        for name in ('reply', 'next-reply', 'old-reply')]
            for child in windows:
                self.assertEqual((await asyncio.wait_for(child.result(), 20))['state'], 'complete')
            for child in messages:
                self.assertEqual(await asyncio.wait_for(child.result(), 20), {'state': 'processed'})
            for kind in ('mailbox-backlog', 'mailbox-observation'):
                child = self.client.get_workflow_handle(identity(self.policy['workspace'], kind, 'fixture-account'))
                self.assertEqual((await asyncio.wait_for(child.result(), 20))['state'], 'complete')
        expected = {fake.references[name] for name in ('reply', 'next-reply', 'old-reply')}
        self.assertEqual(set(fake.published), expected)
        self.assertEqual(set(fake.documents), expected)
        self.assertEqual(len(fake.published), 3)
        self.assertEqual(fake.fetches.pop(fake.references['reply']), 2)
        self.assertEqual(set(fake.fetches.values()), {1})
        self.assertEqual([row[1:] for row in fake.pages if row[0] == fake.references['recent']], [(None, 0), ('next-page', 1)])
        self.assertEqual(await self.replay(windows[0], MAILBOX_WORKFLOWS), 2)
        await self.replay(handle, MAILBOX_WORKFLOWS)
        for child in messages:
            await self.replay(child, MAILBOX_WORKFLOWS)

    async def test_learning_failure_does_not_block_other_cohorts_or_skip_evaluation_and_promotion(self):
        fake = Learning()
        workflows = [LearningWorkflow, LearningRunWorkflow]
        async with AsyncExitStack() as stack:
            await self.workers(stack, fake, ('learning',), workflows)
            handle = await self.client.start_workflow(LearningWorkflow.run,
                Command(digest('input'), self.policy, 'learning'), id='isolated-learning-' + uuid.uuid4().hex,
                task_queue=self.queue('workflow'))
            result = await asyncio.wait_for(handle.result(), 20)
            self.assertEqual(result, {'state': 'accepted', 'cohorts': 2, 'outcomes': {'started': 2}})
            children = {name: self.client.get_workflow_handle(identity(self.policy['workspace'], 'learning-program', name))
                        for name in fake.references}
            try:
                await asyncio.wait_for(fake.retry_started.wait(), 10)
                self.assertEqual((await asyncio.wait_for(children['healthy'].result(), 10))['state'], 'promoted')
                self.assertNotIn('learning_publish', fake.phases['recovering'])
            finally:
                fake.release_recovering.set()
            self.assertEqual((await asyncio.wait_for(children['recovering'].result(), 10))['state'], 'promoted')
        self.assertEqual(fake.collects, 1)
        phases = ['learning_build_dataset', 'learning_evaluate_current', 'learning_optimize',
                  'learning_evaluate_candidate', 'learning_publish']
        self.assertEqual(fake.phases['healthy'], phases)
        self.assertEqual(fake.phases['recovering'], phases[:3] + phases[2:])
        self.assertEqual(await self.replay(handle, workflows), 3)
        for child in children.values():
            await self.replay(child, workflows)

    async def test_real_schedule_install_update_pause_and_declared_disabled_policy(self):
        root = Path(os.environ['APPLYPILOT_DIR'])
        initial = load(root)
        value = initial.model_dump(mode='json')
        value.update(workspace=self.policy['workspace'], queue_prefix=self.policy['queue_prefix'], namespace='default')
        value['schedules'] = {name: {'every_seconds': 86400, 'enabled': name == 'jobs'} for name in initial.schedules}
        selected = [type(initial).model_validate(value)]
        with patch.object(controls, 'load', side_effect=lambda root=None: selected[0]), \
                patch.object(controls, 'connect', new=AsyncMock(return_value=self.client)), \
                patch.object(controls, 'capture', return_value=digest('schedule-input')), \
                patch('applypilot.orchestration.target_schedules.sync_target_schedules', new=AsyncMock(return_value={'targets': []})), \
                patch('applypilot.orchestration.observability.write_snapshot'):
            try:
                first = await controls.install({}, root=root)
                self.assertTrue(all(row['action'] == 'created' and row['paused'] for row in first['schedules']))
                jobs = self.client.get_schedule_handle(controls.schedule_id(selected[0], 'jobs'))
                description = await jobs.describe()
                self.assertEqual(description.schedule.policy.overlap, ScheduleOverlapPolicy.SKIP)
                self.assertFalse(description.schedule.policy.pause_on_failure)
                value['schedules']['jobs']['every_seconds'] = 172800
                selected[0] = type(initial).model_validate(value)
                updated = await controls.install({}, root=root)
                self.assertTrue(all(row['action'] == 'updated' for row in updated['schedules']))
                description = await jobs.describe()
                self.assertTrue(description.schedule.state.paused)
                self.assertEqual(description.schedule.spec.intervals[0].every.total_seconds(), 172800)
                await controls.install({}, root=root, start=True)
                self.assertFalse((await jobs.describe()).schedule.state.paused)
                paused = await controls.change_schedules('pause', ('jobs',), root=root)
                self.assertFalse(paused['existing_workflows_cancelled'])
                self.assertTrue((await jobs.describe()).schedule.state.paused)
                await controls.install({}, root=root)
                self.assertTrue((await jobs.describe()).schedule.state.paused)
                await controls.change_schedules('resume', ('jobs',), root=root)
                self.assertFalse((await jobs.describe()).schedule.state.paused)
                value['schedules']['jobs']['enabled'] = False
                selected[0] = type(initial).model_validate(value)
                await controls.install({}, root=root, start=True)
                self.assertTrue((await jobs.describe()).schedule.state.paused)
                refused = await controls.change_schedules('resume', ('jobs',), root=root)
                self.assertEqual(refused['schedules'][0]['action'], 'disabled_by_declared_policy')
            finally:
                for name in selected[0].schedules:
                    try:
                        await self.client.get_schedule_handle(controls.schedule_id(selected[0], name)).delete()
                    except RPCError as exc:
                        if exc.status != RPCStatusCode.NOT_FOUND:
                            raise

    async def test_referral_provider_fallback_is_workflow_owned_and_unknown_errors_retry_same_operation(self):
        fake = Referrals()
        async with Worker(self.client, task_queue=self.queue('workflow'), workflows=[UtilityWorkflow]), \
                Worker(self.client, task_queue=self.queue('network'), activities=[fake.discovery]):
            handle = await self.client.start_workflow(UtilityWorkflow.run,
                Command(digest('input'), self.policy, 'referral_discovery', cursor='linkedin_rs'),
                id='isolated-referral-' + uuid.uuid4().hex, task_queue=self.queue('workflow'))
            result = await asyncio.wait_for(handle.result(), 20)
        self.assertEqual(result.state, 'discovered')
        self.assertEqual(fake.calls, [('connections', 0), ('people', 1), ('people', 1)])
        self.assertEqual(await self.replay(handle, [UtilityWorkflow]), 1)

    async def test_referral_case_preparation_and_model_revision_remain_separate_across_continuations(self):
        for kind in ('referral_plan', 'referral_prepare'):
            with self.subTest(kind=kind):
                fake = ReferralPreparation()
                async with Worker(self.client, task_queue=self.queue('workflow'), workflows=[UtilityWorkflow]), \
                        Worker(self.client, task_queue=self.queue('maintenance'),
                               activities=[fake.inventory, fake.prepare, fake.finish]), \
                        Worker(self.client, task_queue=self.queue('models'), activities=[fake.revise]):
                    handle = await self.client.start_workflow(UtilityWorkflow.run,
                        Command(digest('input'), self.policy, kind),
                        id='isolated-referral-cases-' + uuid.uuid4().hex, task_queue=self.queue('workflow'))
                    result = await asyncio.wait_for(handle.result(), 20)
                self.assertEqual(result, Observation('prepared', {'cases': 2}))
                self.assertEqual((fake.inventories, fake.finishes), (1, 1))
                self.assertEqual(fake.prepared, [0, 1, 2])
                self.assertEqual(fake.revised, [0, 0, 1])
                self.assertEqual(await self.replay(handle, [UtilityWorkflow]), 3)


if __name__ == '__main__':
    unittest.main()

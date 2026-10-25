"""Isolated crawl recovery checks: no external services, providers, or business DB."""
import asyncio
from copy import deepcopy
from dataclasses import asdict
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from temporalio import activity, workflow
from temporalio.converter import DataConverter
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from applypilot.orchestration import company_crawl, company_workflows
from applypilot.orchestration.company_activities import COMPANY_ACTIVITIES_BY_ROLE
from applypilot.orchestration.company_contracts import CompanyCrawlCommand, CrawlPage
from applypilot.orchestration.contracts import ActivityRequest, digest


def policy():
    return {'workspace': 'isolated-crawl', 'queue_prefix': 'test',
            'activity_retry_initial_seconds': 1, 'activity_retry_maximum_seconds': 10,
            'activities': {role: {'heartbeat_interval_seconds': 1,
                'start_to_close_seconds': 60, 'heartbeat_seconds': 10}
                for role in ('maintenance', 'research')}}


class ContinueRun(BaseException):
    def __init__(self, command):
        self.command = command


class CrawlWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def converted(self, command):
        payloads = await DataConverter.default.encode([command])
        return (await DataConverter.default.decode(payloads, [CompanyCrawlCommand]))[0]

    async def drive(self, command, operation, *, suggest=None):
        continuations = []
        with patch.object(company_workflows, 'operation', side_effect=operation), \
                patch.object(company_workflows.workflow, 'info',
                    return_value=SimpleNamespace(is_continue_as_new_suggested=suggest or (lambda: False))), \
                patch.object(company_workflows.workflow, 'continue_as_new',
                    side_effect=lambda command: (_ for _ in ()).throw(ContinueRun(command))):
            while True:
                try:
                    return await company_workflows.CompanyCrawlWorkflow().run(command), continuations
                except ContinueRun as resumed:
                    command = await self.converted(resumed.command)
                    continuations.append(deepcopy(command))

    async def test_render_state_and_private_references_survive_continue_as_new(self):
        calls, prepared, published = [], [], []
        root, child = digest('root'), digest('child')

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_crawl_prepare':
                prepared.append(command.reference)
                return dict(root_reference=root, backend='auto', allow_browseros=True,
                            max_pages=5, max_depth=1)
            if phase == 'company_crawl_publish':
                published.append(deepcopy(options))
                return {'reference': digest('result'), 'complete': True}
            calls.append((reference, deepcopy(options)))
            backend = options['backend']
            return {'document_reference': digest((reference, backend)), 'final_reference': reference,
                    'thin': reference == root and backend != 'browseros',
                    'links': [root, child, child] if reference == root else [root]}

        suggestions = iter([True, False, False, False])
        result, continuations = await self.drive(CompanyCrawlCommand(digest('options'), policy()),
            operation, suggest=lambda: next(suggestions, False))
        self.assertTrue(result['complete'])
        self.assertEqual(len(prepared), 1)
        self.assertEqual([(ref, options['backend']) for ref, options in calls],
                         [(root, 'http'), (root, 'playwright'), (root, 'browseros'), (child, 'http')])
        self.assertEqual(continuations[0].pending,
                         [CrawlPage(root, 0, 'playwright', digest((root, 'http')))])
        self.assertEqual(calls[2][1]['previous_reference'], digest((root, 'playwright')))
        self.assertEqual(published, [{'documents': [digest((root, 'browseros')), digest((child, 'http'))],
                                     'complete': True}])
        self.assertNotIn('url', str(asdict(continuations[0])))

    async def test_iteration_continuation_keeps_queue_and_visited(self):
        root = digest('root')
        links = [digest(index) for index in range(70)]
        observed = []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_crawl_prepare':
                return dict(root_reference=root, backend='http', allow_browseros=False,
                            max_pages=100, max_depth=1)
            if phase == 'company_crawl_publish':
                return deepcopy(options)
            observed.append(reference)
            return {'document_reference': digest(('document', reference)), 'final_reference': reference,
                    'thin': False, 'links': links if reference == root else [root]}

        result, continuations = await self.drive(CompanyCrawlCommand(digest('options'), policy()), operation)
        self.assertEqual(observed, [root, *links])
        self.assertEqual(len(continuations), 1)
        self.assertEqual(len(continuations[0].documents), 64)
        self.assertEqual(len(continuations[0].pending), 7)
        self.assertEqual(len(continuations[0].seen), 64)
        self.assertTrue(result['complete'])

    async def test_page_limit_preserves_incomplete_business_result(self):
        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_crawl_prepare':
                return dict(root_reference=digest('root'), backend='http', allow_browseros=False,
                            max_pages=1, max_depth=1)
            if phase == 'company_crawl_publish':
                return options
            return {'document_reference': digest('doc'), 'final_reference': reference,
                    'thin': False, 'links': [digest('unread')]}

        result, _ = await self.drive(CompanyCrawlCommand(digest('options'), policy()), operation)
        self.assertEqual(result, {'documents': [digest('doc')], 'complete': False})

    async def test_redirect_observation_deduplicates_an_already_queued_url(self):
        root, alias, target = (digest(value) for value in ('root', 'alias', 'target'))
        calls = []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_crawl_prepare':
                return dict(root_reference=root, backend='http', allow_browseros=False,
                            max_pages=5, max_depth=1)
            if phase == 'company_crawl_publish':
                return options
            calls.append(reference)
            return {'document_reference': digest(('doc', reference)),
                    'final_reference': target if reference == alias else reference,
                    'thin': False, 'links': [alias, target] if reference == root else []}

        result, _ = await self.drive(CompanyCrawlCommand(digest('options'), policy()), operation)
        self.assertEqual(calls, [root, alias])
        self.assertTrue(result['complete'])

    async def test_unknown_failure_retains_current_page_and_native_retry_policy(self):
        command = CompanyCrawlCommand(digest('options'), policy(), prepared=True,
                                     pending=[CrawlPage(digest('root'), 0, 'http')])
        before = deepcopy(command)
        with patch.object(company_workflows, 'operation', side_effect=RuntimeError('new unknown failure')):
            with self.assertRaisesRegex(RuntimeError, 'new unknown failure'):
                await company_workflows.CompanyCrawlWorkflow().run(command)
        self.assertEqual(command, before)
        captured = {}

        async def execute_activity(name, request, **options):
            captured.update(options)
            return {'ok': True}

        with patch.object(company_workflows.workflow, 'execute_activity', side_effect=execute_activity):
            await company_workflows.operation(command, 'company_crawl_page')
        self.assertEqual(captured['retry_policy'].maximum_attempts, 0)
        self.assertFalse(captured['retry_policy'].non_retryable_error_types)

    async def test_real_sdk_prepares_company_workflow_sandbox(self):
        runner = SandboxedWorkflowRunner()
        for implementation in company_workflows.COMPANY_WORKFLOWS:
            runner.prepare_workflow(workflow._Definition.must_from_class(implementation))

    async def test_all_production_registrations_are_sdk_compatible(self):
        from applypilot.orchestration import worker
        workflow_names = set()
        for implementation in worker.workflow_types():
            definition = workflow._Definition.must_from_class(implementation)
            self.assertNotIn(definition.name, workflow_names)
            workflow_names.add(definition.name)
            SandboxedWorkflowRunner().prepare_workflow(definition)
        for role in worker.ROLES:
            if role == 'workflow':
                continue
            names = [activity._Definition.must_from_callable(function).name
                     for function in worker.activities_for(role)]
            self.assertEqual(len(names), len(set(names)), role)
            self.assertEqual(names, [activity._Definition.must_from_callable(function).name
                                    for function in worker.activities_for(role)], role)

    async def test_observed_challenge_routes_to_solver_after_continuation(self):
        calls = []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_crawl_prepare':
                return dict(root_reference=digest('root'), backend='auto', allow_browseros=False,
                            max_pages=1, max_depth=0)
            if phase == 'company_crawl_publish':
                return options
            calls.append(options['backend'])
            if options['backend'] == 'http':
                return {'challenge': True}
            return {'document_reference': digest('doc'), 'final_reference': reference,
                    'thin': False, 'links': []}

        suggestions = iter([True, False])
        result, continuations = await self.drive(CompanyCrawlCommand(digest('options'), policy()),
            operation, suggest=lambda: next(suggestions, False))
        self.assertEqual(calls, ['http', 'flaresolverr'])
        self.assertEqual(continuations[0].pending[0].backend, 'flaresolverr')
        self.assertTrue(result['complete'])

    async def test_each_company_operation_has_one_activity_on_its_declared_queue(self):
        registered = {}
        for role, functions in COMPANY_ACTIVITIES_BY_ROLE.items():
            for function in functions:
                name = activity._Definition.must_from_callable(function).name
                self.assertNotIn(name, registered)
                registered[name] = role
        for phase, role in company_workflows.ROLES.items():
            self.assertEqual(registered['applypilot.' + phase + '.v1'], role)


if __name__ == '__main__':
    unittest.main()

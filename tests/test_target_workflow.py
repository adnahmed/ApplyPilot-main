"""Owner-target execution state survives SDK conversion and Continue-As-New."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from temporalio.converter import DataConverter
from applypilot.orchestration import target_workflows
from applypilot.orchestration.target_contracts import TargetCommand, TargetPage
from applypilot.orchestration.contracts import digest


class Continued(BaseException):
    def __init__(self, command):
        self.command = command


class TargetWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, command, observe):
        continued = []
        with patch.object(target_workflows, 'operation', side_effect=observe), \
                patch.object(target_workflows.workflow, 'info', return_value=SimpleNamespace(
                    run_id='isolated-sweep', is_continue_as_new_suggested=lambda: True)), \
                patch.object(target_workflows.workflow, 'continue_as_new',
                    side_effect=lambda cmd: (_ for _ in ()).throw(Continued(cmd))):
            while True:
                try:
                    return await target_workflows.TargetWorkflow().run(command), continued
                except Continued as raised:
                    payloads = await DataConverter.default.encode([raised.command])
                    command = (await DataConverter.default.decode(payloads, [TargetCommand]))[0]
                    continued.append(deepcopy(command))

    async def test_search_solver_and_independent_vacancies_survive_continuations(self):
        home, redirect = digest('homepage'), digest('redirect')
        inventory, first, second = [digest(value) for value in ('inventory', 'posting-one', 'posting-two')]
        reads, searches, vacancies, publications = [], [], [], []

        async def observe(command, phase, **options):
            if phase == 'target_prepare':
                return {'reference': digest('prepared'), 'query_reference': digest('query'),
                        'read_references': [home], 'needs_research': True, 'pages': []}
            if phase == 'target_search':
                searches.append(options['provider'])
                return {'sources': [digest(options['provider'])], 'pages': [home, redirect]}
            if phase == 'target_read':
                reads.append((options['page_reference'], options['backend']))
                if options['backend'] == 'http':
                    return {'challenge': True}
                return {'source_reference': digest('homepage-evidence'), 'final_reference': redirect}
            if phase == 'target_synthesize':
                self.assertEqual(len(options['sources']), 3)
                return {'reference': digest('synthesized'), 'outreach_only': False,
                        'pages': [{'reference': inventory, 'identity': inventory}]}
            if phase == 'target_vacancy':
                reference = options['page_reference']
                vacancies.append(reference)
                following = [first, second] if reference == inventory else []
                return {'reference': digest(('observation', reference)),
                        'pages': [{'reference': item, 'identity': item} for item in following],
                        'found': 0 if reference == inventory else 1, 'coverage_complete': True}
            if phase == 'target_publish':
                publications.append(options['reference'])
                return {'new': 0 if len(publications) == 1 else 1}
            self.assertEqual(len(options['observations']), 3)
            return {'state': 'exhausted'}

        result, continued = await self.drive(TargetCommand(digest('input'), {}), observe)
        self.assertEqual(searches, ['searxng', 'ddgs'])
        self.assertEqual(reads, [(home, 'http'), (home, 'flaresolverr')])
        self.assertEqual(vacancies, [inventory, first, second])
        self.assertEqual(result.detail, {'terminal': True, 'found': 2, 'new': 2, 'coverage_complete': True})
        self.assertTrue(any(command.reads and command.reads[0].backend == 'flaresolverr' for command in continued))
        self.assertTrue(any(len(command.pending) == 2 for command in continued))

    async def test_outreach_research_completes_without_vacancy_fetches(self):
        phases = []
        async def observe(command, phase, **options):
            phases.append(phase)
            if phase == 'target_prepare':
                return {'reference': digest('prepared'), 'query_reference': digest('query'),
                        'read_references': [], 'needs_research': True, 'pages': []}
            if phase == 'target_search':
                return {'sources': [digest(options['provider'])], 'pages': []}
            if phase == 'target_synthesize':
                return {'reference': digest('synthesized'), 'outreach_only': True, 'pages': []}
            return {'state': 'exhausted'}
        result, _ = await self.drive(TargetCommand(digest('input'), {}), observe)
        self.assertEqual(result.state, 'exhausted')
        self.assertNotIn('target_vacancy', phases)
        self.assertNotIn('target_publish', phases)

    async def test_unknown_vacancy_failure_preserves_the_requested_page(self):
        command = TargetCommand(digest('input'), {}, token='sweep', phase='vacancy',
                                pending=[TargetPage(digest('page'), digest('identity'))])
        before = deepcopy(command)
        with patch.object(target_workflows, 'operation', side_effect=RuntimeError('new failure')):
            with self.assertRaisesRegex(RuntimeError, 'new failure'):
                await target_workflows.TargetWorkflow().run(command)
        self.assertEqual(command, before)


if __name__ == '__main__':
    unittest.main()

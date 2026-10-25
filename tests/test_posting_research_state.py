"""Isolated posting observations and SDK serialization; no database or providers."""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from temporalio.converter import DataConverter
from temporalio import workflow
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from applypilot.orchestration import company_job_domain as domain
from applypilot.orchestration import posting_research_workflows as flows
from applypilot.orchestration.contracts import ActivityRequest, digest
from applypilot.orchestration.posting_research_contracts import PostingResearchCommand, SignalPage, SignalVacancyCommand
from applypilot.integrations.types import SourceError


def module(name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


class DomainObservations(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.values = {}
        self.conn = SimpleNamespace(commit=Mock())

        def put(conn, value):
            reference = digest(value)
            self.values[reference] = deepcopy(value)
            return reference

        self.put = lambda value: put(self.conn, value)
        self.stack.enter_context(patch.object(domain.inputs, 'put', put))
        self.stack.enter_context(patch.object(domain.inputs, 'get', lambda conn, key: deepcopy(self.values[key])))
        self.search = Mock()
        self.crawl = Mock()
        self.resolve = Mock()
        self.researcher = Mock()
        self.research = module('applypilot.integrations.research', re=re,
            _source=lambda *args, **kwargs: {'url': args[0], 'text': args[2], 'kind': args[3]},
            Researcher=self.researcher, FIELDS=('website',), _SIGNAL_SITES={'web_search'})
        modules = {
            'applypilot.integrations.research': self.research,
            'applypilot.integrations.state': module('applypilot.integrations.state', SourceState=Mock()),
            'applypilot.integrations.search': module('applypilot.integrations.search', SearchService=self.search),
            'applypilot.integrations.crawl': module('applypilot.integrations.crawl', CrawlService=self.crawl),
            'applypilot.discovery.repository': module('applypilot.discovery.repository', hydrate_job=Mock(), transaction=Mock()),
            'applypilot.database': module('applypilot.database', preparation_stock_sql=Mock()),
            'applypilot.discovery.signal_research': module('applypilot.discovery.signal_research', resolve_signal=self.resolve),
        }
        self.stack.enter_context(patch.dict(sys.modules, modules))
        import applypilot.integrations
        self.stack.enter_context(patch.object(applypilot.integrations, 'research', self.research, create=True))

    def execute(self, phase, value, **options):
        request = ActivityRequest(self.put(value), phase, options=options)
        return domain.execute(request, deepcopy(value), self.conn, lambda: None)

    def assert_no_private_queues(self):
        for value in self.values.values():
            self.assertFalse({'searches', 'urls', 'signal_traversal', 'pending', 'visited'} & value.keys())

    def test_prepare_returns_private_query_and_page_references(self):
        observed = self.execute('company_report_prepare', {'options': {'company_report': {
            'target': 'example.org', 'fields': ['website'], 'queries': 2, 'search': 'auto',
            'max_pages': 3, 'crawler': 'hybrid', 'ai': True}}})
        self.assertEqual(len(observed['queries']), 2)
        self.assertEqual(observed['search_provider'], 'auto')
        self.assertEqual(self.values[observed['pages'][0]], {'url': 'https://example.org'})
        self.assertEqual(set(self.values[observed['queries'][0]]), {'query'})
        self.assert_no_private_queues()

    def test_search_executes_one_explicit_provider_and_returns_observed_pages(self):
        hit = SimpleNamespace(url='https://example.org/jobs', title='Jobs', snippet='Hiring',
                              fetched_at='2026-01-01', provider='ddgs')
        self.search.return_value.search.return_value = [hit]
        observed = self.execute('posting_research_search', {'sources': []},
            query_reference=self.put({'query': 'Example jobs'}), provider='ddgs')
        self.search.assert_called_once()
        self.assertEqual(self.search.call_args.args, ('ddgs',))
        self.search.return_value.search.assert_called_once_with('Example jobs', limit=4)
        self.assertEqual(self.values[observed['pages'][0]], {'url': hit.url})
        self.assertEqual(len(self.values[observed['reference']]['sources']), 1)
        self.assert_no_private_queues()

    def test_read_returns_thin_observation_without_choosing_fallback(self):
        self.crawl.return_value.read.return_value = SimpleNamespace(
            url='https://example.org/', title='Example', markdown='javascript required',
            links=(), fetched_at='2026-01-01', provider='http', truncated=False)
        observed = self.execute('posting_research_read', {'sources': []},
            page_reference=self.put({'url': 'https://example.org/'}), backend='http')
        self.crawl.return_value.read.assert_called_once_with('https://example.org/', fresh=True)
        self.assertTrue(observed['thin'])
        self.assertEqual(set(observed), {'reference', 'thin'})
        self.assert_no_private_queues()

    def test_auto_provider_or_backend_cannot_expand_inside_activity(self):
        with self.assertRaises(ValueError):
            self.execute('posting_research_search', {'sources': []},
                query_reference=self.put({'query': 'Example jobs'}), provider='auto')
        with self.assertRaises(ValueError):
            self.execute('posting_research_read', {'sources': []},
                page_reference=self.put({'url': 'https://example.org/'}), backend='hybrid')
        self.search.assert_not_called()
        self.crawl.assert_not_called()

    def test_read_observes_only_actual_challenge_and_solver_failure_propagates(self):
        value = {'sources': []}
        page = self.put({'url': 'https://example.org/'})
        error = SourceError('challenge observed', 'access_denied')
        error.challenge_url = 'https://example.org/challenge'
        self.crawl.return_value.read.side_effect = error
        observed = self.execute('posting_research_read', value, page_reference=page, backend='http')
        self.assertEqual(self.values[observed['challenge_reference']], {'url': error.challenge_url})
        with self.assertRaises(SourceError) as solver:
            self.execute('posting_research_read', value, page_reference=page, backend='flaresolverr')
        self.assertIs(solver.exception, error)
        self.crawl.return_value.read.side_effect = SourceError('ordinary denial', 'access_denied')
        with self.assertRaises(SourceError):
            self.execute('posting_research_read', value, page_reference=page, backend='http')

    def test_signal_observes_one_page_and_keeps_next_pages_out_of_report(self):
        @dataclass
        class Context:
            reserve_budget: object = None

        self.stack.enter_context(patch.object(domain, '_context', return_value=Context()))
        entry = {'url': 'https://example.org/jobs'}
        self.resolve.return_value = {'resolved_postings': [], 'status': 'deferred',
            'observations': [{'url': entry['url']}], 'coverage': 'partial',
            'traversal': {'pending': [{'url': 'https://example.org/next', 'depth': 2}], 'visited': [entry['url']]}}
        observed = self.execute('posting_research_signal', {
            'job': {'url': 'private-job', 'company': 'Example'}, 'report': {}},
            page_reference=self.put(entry), depth=1, backend='http')
        self.assertEqual(self.resolve.call_args.kwargs['traversal'], {
            'pending': [{**entry, 'depth': 1}], 'visited': []})
        self.assertEqual(len(observed['pages']), 1)
        self.assertEqual(observed['pages'][0]['depth'], 2)
        report = self.values[observed['reference']]['report']['signal_research']
        self.assertNotIn('traversal', report)
        finalized = self.execute('posting_research_signal', self.values[observed['reference']], finalize=True)
        self.assertEqual(self.values[finalized['reference']]['report']['signal_research']['coverage'], 'observed_pages')
        self.resolve.assert_called_once()
        self.assert_no_private_queues()

    def test_signal_challenge_keeps_one_page_and_explicit_solver_backend(self):
        @dataclass
        class Context:
            reserve_budget: object = None

        self.stack.enter_context(patch.object(domain, '_context', return_value=Context()))
        error = SourceError('observed browser challenge', 'access_denied')
        error.challenge_url = 'https://example.org/challenge'
        self.resolve.side_effect = error
        value = {'job': {'url': 'private-job', 'company': 'Example'}, 'report': {}}
        page = self.put({'url': 'https://example.org/jobs'})
        observed = self.execute('posting_research_signal', value,
                                page_reference=page, depth=1, backend='http')
        self.assertEqual(self.values[observed['challenge_reference']], {'url': error.challenge_url})
        with self.assertRaises(SourceError):
            self.execute('posting_research_signal', value,
                         page_reference=observed['challenge_reference'], depth=1, backend='flaresolverr')
        self.assertEqual(self.researcher.call_args.kwargs['crawler'], 'flaresolverr')
        self.assertEqual(self.resolve.call_args.kwargs['traversal']['pending'],
                         [{'url': error.challenge_url, 'depth': 1}])
        self.assert_no_private_queues()


class ContinueRun(BaseException):
    def __init__(self, command):
        self.command = command


async def run_restarting(implementation, command, execute):
    def restart(value):
        raise ContinueRun(value)

    info = SimpleNamespace(is_continue_as_new_suggested=lambda: True, workflow_id='isolated-posting')
    converter = DataConverter.default
    with patch.object(flows, 'operation', execute), patch.object(flows.workflow, 'info', return_value=info), \
            patch.object(flows.workflow, 'continue_as_new', restart):
        for _ in range(200):
            try:
                return await implementation.run(command)
            except ContinueRun as continued:
                payload = await converter.encode([continued.command])
                command, = await converter.decode(payload, [type(command)])
        raise AssertionError('Workflow failed to finish')


class WorkflowState(unittest.IsolatedAsyncioTestCase):
    async def test_workflows_prepare_in_real_sdk_sandbox(self):
        for implementation in (flows.CompanyPostingResearchWorkflow, flows.CompanyReportWorkflow,
                               flows.CompanySignalVacancyWorkflow):
            SandboxedWorkflowRunner().prepare_workflow(workflow._Definition.must_from_class(implementation))

    async def test_posting_without_requested_limit_keeps_all_observed_pages(self):
        command = PostingResearchCommand('evidence', {})
        references = ['page' + str(index) for index in range(20)]
        flows._add_pages(command, references)
        flows._add_pages(command, references)
        self.assertEqual([page.reference for page in command.pending], references)

    async def test_report_restarts_keep_provider_order_page_cap_and_backend_fallback(self):
        calls = []

        async def execute(command, phase, **kwargs):
            options = kwargs.get('options', {})
            calls.append((phase, deepcopy(options)))
            if phase == 'company_report_prepare':
                return {'reference': 'evidence', 'queries': ['query1', 'query2'], 'pages': ['root'],
                        'search_provider': 'auto', 'crawler': 'auto', 'max_pages': 2}
            if phase == 'posting_research_search':
                return {'reference': 'evidence', 'pages': ['root', 'other', 'over-limit']}
            if phase == 'posting_research_read':
                return {'reference': 'evidence', 'thin': options['page_reference'] == 'root'}
            if phase == 'posting_research_synthesize':
                return {'reference': 'report', 'signal': False}
            raise AssertionError(phase)

        result = await run_restarting(flows.CompanyReportWorkflow(), PostingResearchCommand('input', {}), execute)
        self.assertEqual(result['reference'], 'report')
        searches = [options for phase, options in calls if phase == 'posting_research_search']
        self.assertEqual(searches, [{'query_reference': query, 'provider': provider}
            for query in ['query1', 'query2'] for provider in ['searxng', 'ddgs']])
        reads = [options for phase, options in calls if phase == 'posting_research_read']
        self.assertEqual(reads, [{'page_reference': 'root', 'backend': 'http'},
            {'page_reference': 'root', 'backend': 'playwright'}, {'page_reference': 'other', 'backend': 'http'}])
        self.assertEqual(sum(phase == 'company_report_prepare' for phase, _ in calls), 1)

    async def test_signal_restart_retains_dedupe_and_native_cursor_identity(self):
        calls = []
        first = SignalPage('board-page1', 'board-cursor1')
        second = SignalPage('board-page2', 'board-cursor2')

        async def execute(command, phase, **kwargs):
            options = kwargs['options']
            calls.append(deepcopy(options))
            if options.get('finalize'):
                return {'reference': 'final-report', 'signal': False}
            if options['page_reference'] == first.reference:
                return {'reference': 'report1', 'pages': [vars(first), vars(second)]}
            self.assertEqual(options['page_reference'], second.reference)
            return {'reference': 'report2', 'pages': [vars(first), vars(second)]}

        result = await run_restarting(flows.CompanySignalVacancyWorkflow(),
            SignalVacancyCommand('report', {}, pending=[first]), execute)
        self.assertEqual(result['reference'], 'final-report')
        self.assertEqual(calls, [{'page_reference': first.reference, 'depth': 0, 'backend': 'http'},
            {'page_reference': second.reference, 'depth': 0, 'backend': 'http'}, {'finalize': True}])

    async def test_report_challenge_solver_selection_survives_continue_as_new(self):
        calls = []

        async def execute(command, phase, **kwargs):
            options = kwargs.get('options', {})
            calls.append((phase, deepcopy(options)))
            if phase == 'company_report_prepare':
                return {'reference': 'evidence', 'queries': [], 'pages': ['original'],
                        'search_provider': 'auto', 'crawler': 'http', 'max_pages': 1}
            if phase == 'posting_research_read':
                if options['backend'] == 'http':
                    return {'reference': 'evidence', 'challenge_reference': 'actual-challenge'}
                self.assertEqual(options, {'page_reference': 'actual-challenge', 'backend': 'flaresolverr'})
                return {'reference': 'solved-evidence', 'thin': False}
            return {'reference': 'report', 'signal': False}

        result = await run_restarting(flows.CompanyReportWorkflow(), PostingResearchCommand('input', {}), execute)
        self.assertEqual(result['reference'], 'report')
        self.assertEqual([options for phase, options in calls if phase == 'posting_research_read'], [
            {'page_reference': 'original', 'backend': 'http'},
            {'page_reference': 'actual-challenge', 'backend': 'flaresolverr'}])

    async def test_signal_challenge_solver_selection_survives_continue_as_new(self):
        calls = []

        async def execute(command, phase, **kwargs):
            options = kwargs['options']
            calls.append(deepcopy(options))
            if options.get('finalize'):
                return {'reference': 'final-report', 'signal': False}
            if options['backend'] == 'http':
                return {'reference': 'report', 'challenge_reference': 'actual-challenge'}
            self.assertEqual(command.seen, [])
            self.assertFalse(command.observed)
            return {'reference': 'resolved-report', 'pages': []}

        result = await run_restarting(flows.CompanySignalVacancyWorkflow(),
            SignalVacancyCommand('report', {}, pending=[SignalPage('original', 'original-id', 1)]), execute)
        self.assertEqual(result['reference'], 'final-report')
        self.assertEqual(calls, [{'page_reference': 'original', 'depth': 1, 'backend': 'http'},
            {'page_reference': 'actual-challenge', 'depth': 1, 'backend': 'flaresolverr'}, {'finalize': True}])

    async def test_posting_restart_does_not_repeat_models_or_publish(self):
        calls = []

        async def execute(command, phase, **kwargs):
            calls.append(phase)
            if phase == 'posting_research_synthesize':
                return {'reference': 'report', 'signal': False, 'pages': []}
            if phase == 'posting_research_assess':
                return {'reference': 'decision'}
            if phase == 'posting_research_publish':
                return {'state': 'complete', 'detail': {'suitability': 'suitable'}}
            raise AssertionError(phase)

        result = await run_restarting(flows.CompanyPostingResearchWorkflow(),
            PostingResearchCommand('evidence', {}, phase='synthesize'), execute)
        self.assertEqual(result.state, 'complete')
        self.assertEqual(calls, ['posting_research_synthesize', 'posting_research_assess', 'posting_research_publish'])


if __name__ == '__main__':
    unittest.main()

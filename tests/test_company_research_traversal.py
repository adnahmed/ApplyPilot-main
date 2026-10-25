"""Source enrichment and vacancy queues belong to replayable Workflow state."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from temporalio.converter import DataConverter

from applypilot.orchestration import company_workflows
from applypilot.orchestration.company_contracts import (CompanyEvidenceCommand, CompanyPage,
    CompanySourceCommand, IssuerObservation)
from applypilot.orchestration.contracts import digest


class Continued(BaseException):
    def __init__(self, command):
        self.command = command


class ResearchTraversalTests(unittest.IsolatedAsyncioTestCase):
    async def drive(self, implementation, command, operation, *, always_continue=True):
        continuations = []
        children = AsyncMock()
        with patch.object(company_workflows, 'operation', side_effect=operation), \
                patch.object(company_workflows, 'start_child', children), \
                patch.object(company_workflows.workflow, 'info', return_value=SimpleNamespace(
                    is_continue_as_new_suggested=lambda: always_continue)), \
                patch.object(company_workflows.workflow, 'continue_as_new',
                    side_effect=lambda cmd: (_ for _ in ()).throw(Continued(cmd))):
            while True:
                try:
                    result = await implementation().run(command)
                    return result, continuations, children
                except Continued as continued:
                    payload = await DataConverter.default.encode([continued.command])
                    command = (await DataConverter.default.decode(payload, [type(command)]))[0]
                    continuations.append(deepcopy(command))

    async def test_source_enrichment_and_page_depth_survive_every_continuation(self):
        root, second, detail, grandchild, too_deep = [digest(value) for value in range(5)]
        issuer, article, home = [digest(value) for value in range(5, 8)]
        reads, enrichments, published = [], [], []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_source_read':
                reads.append(reference)
                return {'reference': reference, 'identities': [reference],
                        'issuers': [issuer] if reference == root else []}
            if phase == 'company_source_enrich':
                enrichments.append((reference, options['operation'], options['provider']))
                if options['operation'] == 'search':
                    return {'candidates': [article, article]}
                if reference == article:
                    return {'candidates': [home]}
                return {'reference': root, 'identified': True}
            if phase == 'company_source_extract':
                page = command.current.reference
                children = {root: [detail, detail, root], second: [detail], detail: [grandchild],
                            grandchild: [too_deep]}[page]
                return {'reference': page, 'companies': [{'subject': digest('company'),
                    'reference': digest('company-evidence')}] if page == root else [],
                    'details': [{'reference': item, 'identity': item} for item in children],
                    'continuation': {'reference': second, 'identity': second} if page == root else None}
            published.append(command.reference)
            return {'state': 'published'}

        result, continued, children = await self.drive(company_workflows.CompanySourceWorkflow,
            CompanySourceCommand(root, {}), operation)
        self.assertEqual(result, {'state': 'published', 'companies': 1})
        self.assertEqual(reads, [root, second, detail, grandchild])
        self.assertEqual(published, reads)
        self.assertEqual(enrichments, [(issuer, 'search', 'searxng'), (article, 'read', ''), (home, 'read', '')])
        self.assertTrue(any(item.identified_issuers == [issuer] for item in continued))
        self.assertTrue(any(item.current and item.current.depth == 2 for item in continued))
        self.assertIsInstance(children.call_args.args[0], CompanyEvidenceCommand)

    async def test_source_iteration_boundary_keeps_the_exact_next_stage(self):
        reads = []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_source_read':
                index = int(reference)
                reads.append(index)
                return {'reference': reference, 'identities': [reference], 'issuers': []}
            if phase == 'company_source_extract':
                index = int(command.reference)
                return {'reference': command.reference, 'companies': [], 'details': [],
                    'continuation': {'reference': str(index + 1), 'identity': str(index + 1)} if index < 19 else None}
            return {'state': 'published'}

        result, continued, _ = await self.drive(company_workflows.CompanySourceWorkflow,
            CompanySourceCommand('0', {}), operation, always_continue=False)
        self.assertEqual(reads, list(range(20)))
        self.assertEqual(len(continued), 1)
        self.assertEqual(continued[0].pending, [CompanyPage('16', '16', 0)])
        self.assertEqual(result['state'], 'published')

    async def test_vacancy_cursor_and_discovered_page_references_survive_continuation(self):
        pages = [digest(value) for value in ('board', 'next-cursor', 'careers')]
        prepared, fetched, published = [], [], []

        async def operation(command, phase, *, reference=None, options=None):
            if phase == 'company_read':
                prepared.append(phase)
                return {'reference': digest('read')}
            if phase == 'company_assess':
                prepared.append(phase)
                return {'reference': digest('assessment'),
                        'pages': [{'reference': pages[0], 'identity': pages[0], 'depth': 0}]}
            if phase == 'company_vacancy':
                page = options['page_reference']
                fetched.append((page, options['depth']))
                following = {pages[0]: [(pages[1], 0), (pages[2], 1)],
                             pages[1]: [(pages[2], 1)], pages[2]: [(pages[0], 0)]}[page]
                return {'reference': page, 'pages': [{'reference': ref, 'identity': ref, 'depth': depth}
                                                    for ref, depth in following]}
            published.append(command.reference)
            return {'state': 'published'}

        result, continued, _ = await self.drive(company_workflows.CompanyEvidenceWorkflow,
            CompanyEvidenceCommand(digest('input'), {}), operation)
        self.assertEqual(prepared, ['company_read', 'company_assess'])
        self.assertEqual(fetched, [(pages[0], 0), (pages[1], 0), (pages[2], 1)])
        self.assertEqual(published, [pages[2]])
        self.assertEqual(continued[0].pending, [CompanyPage(pages[1], pages[1], 0), CompanyPage(pages[2], pages[2], 1)])
        self.assertEqual(result['state'], 'published')

    async def test_unclassified_failures_preserve_enrichment_and_vacancy_heads(self):
        source = CompanySourceCommand(digest('document'), {}, initialized=True, phase='enrich',
            current=CompanyPage(digest('source')), enrichment=[IssuerObservation(digest('issuer'), digest('issuer'), 'search', 'ddgs')])
        evidence = CompanyEvidenceCommand(digest('company'), {}, prepared=True,
            pending=[CompanyPage(digest('page'), digest('identity'))])
        for command, implementation in ((source, company_workflows.CompanySourceWorkflow),
                                       (evidence, company_workflows.CompanyEvidenceWorkflow)):
            expected = deepcopy(command)
            with patch.object(company_workflows, 'operation', side_effect=RuntimeError('unknown external failure')):
                with self.assertRaisesRegex(RuntimeError, 'unknown external failure'):
                    await implementation().run(command)
            self.assertEqual(command, expected)

    async def test_source_and_evidence_challenges_select_solver_in_workflow_state(self):
        source_backends, evidence_backends, vacancy_backends = [], [], []
        async def source_operation(command, phase, *, reference=None, options=None):
            if phase == 'company_source_read':
                source_backends.append(options['backend'])
                if options['backend'] == 'http':
                    return {'challenge': True}
                return {'reference': digest('document'), 'identities': [digest('source')], 'issuers': []}
            if phase == 'company_source_extract':
                return {'reference': command.reference, 'companies': [], 'details': [], 'continuation': None}
            return {'state': 'published'}
        await self.drive(company_workflows.CompanySourceWorkflow,
            CompanySourceCommand(digest('source'), {}), source_operation)

        async def evidence_operation(command, phase, *, reference=None, options=None):
            if phase == 'company_read':
                evidence_backends.append(options['backend'])
                if options['backend'] == 'http':
                    return {'challenge': True}
                return {'reference': digest('document')}
            if phase == 'company_assess':
                return {'reference': digest('assessment'), 'pages': [
                    {'reference': digest('vacancy'), 'identity': digest('vacancy'), 'depth': 0}]}
            if phase == 'company_vacancy':
                vacancy_backends.append(options['backend'])
                if options['backend'] == 'http':
                    return {'challenge': True}
                return {'reference': digest('observed'), 'pages': []}
            return {'state': 'published'}
        await self.drive(company_workflows.CompanyEvidenceWorkflow,
            CompanyEvidenceCommand(digest('company'), {}), evidence_operation)
        self.assertEqual(source_backends, ['http', 'flaresolverr'])
        self.assertEqual(evidence_backends, ['http', 'flaresolverr'])
        self.assertEqual(vacancy_backends, ['http', 'flaresolverr'])


if __name__ == '__main__':
    unittest.main()

"""Prediction traversal replay state checked with the native Temporal SDK."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from temporalio.converter import DataConverter
from applypilot.orchestration.contracts import digest
from applypilot.orchestration.prediction_contracts import PredictionCommand, PredictionPage
from applypilot.orchestration.prediction_workflows import PredictiveCompanyWorkflow


class Continued(Exception):
    def __init__(self, command):
        self.command = deepcopy(command)


def continue_as_new(command):
    raise Continued(command)


class PredictionWorkflowState(unittest.IsolatedAsyncioTestCase):
    async def test_continuation_retains_exact_operation_and_does_not_repeat_research(self):
        from applypilot.orchestration import company_workflows
        phases, searches, vacancies = [], [], []
        root, homepage, query = digest('root'), digest('homepage'), digest('query')

        async def operation(command, phase, *, reference=None, options=None):
            phases.append(phase)
            if phase == 'prediction_prepare':
                return {'reference': root, 'homepage': homepage, 'vacancies': [
                    {'reference': digest(('vacancy', i)), 'identity': str(i), 'depth': 0} for i in range(80)]}
            if phase == 'prediction_read':
                self.assertTrue(options['home'])
                return {'identities': [homepage, homepage], 'pages': [homepage], 'queries': [query], 'document': True}
            if phase == 'prediction_search':
                searches.append(options)
                return {'pages': [homepage]}
            if phase == 'prediction_vacancy':
                vacancies.append(options['page_reference'])
                return {'reference': command.reference, 'pages': []}
            if phase in {'prediction_analyze', 'prediction_audit'}:
                return {'reference': digest(phase)}
            return {'state': 'audited'}

        command = PredictionCommand(root, {})
        with patch.object(company_workflows, 'operation', new=operation), \
                patch('applypilot.orchestration.prediction_workflows.workflow.info',
                      return_value=SimpleNamespace(is_continue_as_new_suggested=lambda: False)), \
                patch('applypilot.orchestration.prediction_workflows.workflow.continue_as_new', side_effect=continue_as_new):
            with self.assertRaises(Continued) as continued:
                await PredictiveCompanyWorkflow().run(command)
            saved = continued.exception.command
            self.assertEqual(saved.phase, 'vacancies')
            converter = DataConverter.default
            payloads = await converter.encode([saved])
            restored, = await converter.decode(payloads, [PredictionCommand])
            result = await PredictiveCompanyWorkflow().run(restored)
        self.assertEqual(result, {'state': 'audited'})
        self.assertEqual(phases.count('prediction_prepare'), 1)
        self.assertEqual(phases.count('prediction_read'), 1)
        self.assertEqual(phases.count('prediction_analyze'), 1)
        self.assertEqual(phases.count('prediction_audit'), 1)
        self.assertEqual(len(vacancies), len(set(vacancies)))
        self.assertEqual(len(vacancies), 80)
        self.assertEqual(searches, [{'query_reference': query, 'provider': 'searxng'},
                                   {'query_reference': query, 'provider': 'ddgs'}])

    async def test_selected_research_is_exhausted_without_a_hidden_page_budget(self):
        from applypilot.orchestration import company_workflows
        calls = []

        async def operation(command, phase, **kwargs):
            calls.append(phase)
            if phase == 'prediction_read':
                page = kwargs['options']['page_reference']
                return {'identities': [page, page], 'pages': [], 'queries': [], 'document': True}
            if phase == 'prediction_publish':
                return {'state': 'audited'}
            return {'reference': command.reference}

        command = PredictionCommand(digest('root'), {}, phase='research',
            pending=[PredictionPage(digest(('page', i))) for i in range(15)], homepage_observed=True)
        with patch.object(company_workflows, 'operation', new=operation), \
                patch('applypilot.orchestration.prediction_workflows.workflow.info',
                      return_value=SimpleNamespace(is_continue_as_new_suggested=lambda: False)):
            await PredictiveCompanyWorkflow().run(command)
        self.assertEqual(calls.count('prediction_read'), 15)
        self.assertEqual(calls[-3:], ['prediction_analyze', 'prediction_audit', 'prediction_publish'])


if __name__ == '__main__':
    unittest.main()

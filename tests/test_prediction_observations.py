"""Prediction Activities observe one operation; only Temporal stores traversal."""
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import patch


from applypilot.orchestration import company_prediction as domain
from applypilot.orchestration.contracts import ActivityRequest, digest


@dataclass
class Context:
    reserve_budget: object = None


class PredictionObservations(unittest.TestCase):
    def setUp(self):
        self.stored = {}
        self.conn = SimpleNamespace(commit=lambda: None)
        self.value = {'company': {'company': 'Example Research', 'website': 'https://example.com',
                                 'careers_url': 'https://example.com/careers'}, 'snapshot': digest('snapshot')}
        self.root = self.put(self.conn, self.value)
        self.patches = [patch.object(domain, '_context', return_value=Context()),
                        patch.object(domain.inputs, 'put', side_effect=self.put),
                        patch.object(domain.inputs, 'get', side_effect=lambda conn, key: deepcopy(self.stored[key]))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def put(self, conn, value):
        key = digest(value)
        self.stored[key] = deepcopy(value)
        return key

    def assert_no_persisted_traversal(self):
        for value in self.stored.values():
            self.assertFalse({'pending', 'seen', 'queries', 'traversal', 'visited', 'prepared'} & value.keys())

    def test_page_returns_private_references_without_saving_a_queue(self):
        from applypilot.integrations.types import Document
        page = self.put(self.conn, {'url': 'https://example.com'})
        document = Document('https://example.com', 'Private title', 'Example Research engineering',
                            ('https://example.com/blog/research', 'https://outside.example/blog/research'), 'http')
        with patch('applypilot.integrations.crawl.CrawlService') as crawler, \
                patch('applypilot.company_discovery.predictive_store.save_documents') as save, \
                patch('applypilot.company_discovery.predictive_store.documents', return_value=[]):
            crawler.return_value.read.return_value = document
            observed = domain.execute(ActivityRequest(self.root, 'prediction_read',
                options={'page_reference': page, 'home': True, 'backend': 'http'}), deepcopy(self.value), self.conn, lambda: None)
        self.assertTrue(observed['document'])
        self.assertEqual(len(observed['queries']), 3)
        self.assertEqual(observed['identities'], [page, page])
        self.assertEqual([self.stored[key]['url'] for key in observed['pages']], ['https://example.com/blog/research'])
        self.assertNotIn('Example Research', str(observed))
        self.assertEqual(save.call_args.args[2][0]['text'], document.markdown)
        self.assert_no_persisted_traversal()

    def test_search_uses_exact_provider_and_query_without_consuming_shared_state(self):
        query = self.put(self.conn, {'query': 'private research query'})
        with patch('applypilot.integrations.search.SearchService') as search:
            search.return_value.search.return_value = [SimpleNamespace(url='https://example.com/blog/result'),
                SimpleNamespace(url='https://outside.example/result')]
            observed = domain.execute(ActivityRequest(self.root, 'prediction_search',
                options={'query_reference': query, 'provider': 'ddgs'}), deepcopy(self.value), self.conn, lambda: None)
            search.return_value.search.assert_called_once_with('private research query', limit=4)
            self.assertEqual(search.call_args.args, ('ddgs',))
        self.assertEqual([self.stored[key]['url'] for key in observed['pages']], ['https://example.com/blog/result'])
        self.assert_no_persisted_traversal()

    def test_vacancy_observes_one_entry_and_returns_the_observed_continuation(self):
        page = self.put(self.conn, {'url': 'https://example.com/careers', 'cursor': 'page-1'})
        with patch('applypilot.discovery.company_targets.vacancy_step') as vacancy, \
                patch('applypilot.company_discovery.predictive_store.record_observations') as record, \
                patch('applypilot.integrations.crawl.CrawlService'):
            vacancy.return_value = ([], [{'provider': 'fixture'}], {'pending': [
                {'url': 'https://example.com/careers', 'cursor': 'page-2', 'depth': 1}], 'visited': ['private visited']})
            observed = domain.execute(ActivityRequest(self.root, 'prediction_vacancy',
                options={'page_reference': page, 'depth': 1, 'backend': 'http'}), deepcopy(self.value), self.conn, lambda: None)
            self.assertEqual(vacancy.call_args.args[0], {'pending': [
                {'url': 'https://example.com/careers', 'cursor': 'page-1', 'depth': 1}], 'visited': []})
            record.assert_not_called()
        self.assertEqual(self.stored[observed['pages'][0]['reference']]['cursor'], 'page-2')
        self.assertEqual(observed['pages'][0]['depth'], 1)
        self.assertEqual(self.stored[observed['reference']]['vacancy_observations'], [{'provider': 'fixture'}])
        self.assert_no_persisted_traversal()

    def test_only_observed_challenges_choose_solver_and_solver_errors_propagate(self):
        from applypilot.integrations.types import SourceError
        page = self.put(self.conn, {'url': 'https://example.com'})
        for backend, challenge in [('http', True), ('http', False), ('flaresolverr', True)]:
            with self.subTest(backend=backend, challenge=challenge):
                error = SourceError('Observed provider failure')
                if challenge:
                    error.challenge_url = 'https://example.com/private-challenge'
                with patch('applypilot.integrations.crawl.CrawlService') as crawler:
                    crawler.return_value.read.side_effect = error
                    request = ActivityRequest(self.root, 'prediction_read', options={
                        'page_reference': page, 'home': True, 'backend': backend})
                    if backend == 'http' and challenge:
                        observed = domain.execute(request, deepcopy(self.value), self.conn, lambda: None)
                        self.assertEqual(self.stored[observed['challenge_reference']]['url'], error.challenge_url)
                    else:
                        with self.assertRaises(SourceError) as raised:
                            domain.execute(request, deepcopy(self.value), self.conn, lambda: None)
                        self.assertIs(raised.exception, error)


if __name__ == "__main__":
    unittest.main()

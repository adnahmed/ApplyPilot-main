"""Finite crawl evidence checks with no network or PostgreSQL access."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from applypilot.orchestration import company_crawl
from applypilot.orchestration.contracts import ActivityRequest, digest


class CrawlObservationTests(unittest.TestCase):
    def test_only_observed_challenges_select_solver_and_solver_failures_retry(self):
        from applypilot.integrations.types import SourceError
        secret_url = 'https://example.com/?token=private'
        for configured, selected, challenge, switches in (
                ('auto', 'http', True, True), ('hybrid', 'playwright', True, True),
                ('http', 'http', True, False), ('auto', 'http', False, False),
                ('auto', 'flaresolverr', True, False)):
            with self.subTest(configured=configured, selected=selected, challenge=challenge):
                failure = SourceError('Observed page response')
                if challenge:
                    failure.challenge_url = secret_url
                value = {'options': {'crawl': {'backend': configured, 'fresh': False}}}
                with patch.object(company_crawl.inputs, 'get', return_value=value), \
                        patch('applypilot.integrations.crawl.CrawlService') as crawler:
                    crawler.return_value.read.side_effect = failure
                    request = ActivityRequest(digest('url'), 'company_crawl_page',
                        options={'crawl_reference': digest('options'), 'backend': selected})
                    if switches:
                        self.assertEqual(company_crawl.execute(request, {'url': secret_url}, None, lambda: None),
                                         {'challenge': True})
                    else:
                        with self.assertRaises(SourceError) as raised:
                            company_crawl.execute(request, {'url': secret_url}, None, lambda: None)
                        self.assertIs(raised.exception, failure)

    def test_activity_returns_observations_without_persisting_traversal(self):
        from applypilot.integrations.types import Document
        secret_url = 'https://example.com/?token=private'
        crawl_options = {'url': secret_url, 'backend': 'auto', 'allow_browseros': False,
                         'fresh': False, 'focus': 'careers', 'depth': 1, 'max_pages': 4}
        stored = {}

        def put(conn, value):
            key = digest(value)
            stored[key] = deepcopy(value)
            return key

        conn = SimpleNamespace(commit=lambda: None)
        source = put(conn, {'options': {'crawl': crawl_options}})
        document = Document(secret_url, 'Private title', 'body ' * 150,
            ('https://example.com/about', 'https://other.example/outside',
             'https://example.com/careers#jobs', 'https://example.com/careers'), 'http')
        with patch.object(company_crawl.inputs, 'put', side_effect=put), \
                patch.object(company_crawl.inputs, 'get', side_effect=lambda conn, ref: deepcopy(stored[ref])), \
                patch('applypilot.integrations.crawl.CrawlService') as crawler:
            crawler.return_value.read.return_value = document
            prepared = company_crawl.execute(ActivityRequest(source, 'company_crawl_prepare'),
                                             stored[source], conn, lambda: None)
            page = company_crawl.execute(ActivityRequest(prepared['root_reference'], 'company_crawl_page',
                options={'crawl_reference': source, 'backend': 'http'}),
                stored[prepared['root_reference']], conn, lambda: None)
            crawler.return_value.read.assert_called_once_with(secret_url, fresh=False)
            self.assertEqual([stored[ref]['url'] for ref in page['links']],
                             ['https://example.com/careers', 'https://example.com/about'])
            self.assertNotIn('private', str(prepared) + str(page))
            self.assertFalse(any({'pending', 'seen', 'depth', 'backend'} & value.keys()
                                 for value in stored.values()))
            published = company_crawl.execute(ActivityRequest(source, 'company_crawl_publish',
                options={'documents': [page['document_reference']], 'complete': True}),
                stored[source], conn, lambda: None)
            self.assertEqual(stored[published['reference']]['result']['documents'], [document.to_dict()])


if __name__ == '__main__':
    unittest.main()

"""Finite company observations persist evidence, never traversal execution state."""
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from applypilot.integrations.types import Document
from applypilot.orchestration import company_domain
from applypilot.orchestration.contracts import ActivityRequest, digest


@dataclass
class Context:
    reserve_budget: object = None


class CompanyObservationTests(unittest.TestCase):
    def setUp(self):
        self.stored = {}
        self.conn = SimpleNamespace(commit=lambda: None,
            execute=lambda *args: SimpleNamespace(fetchone=lambda: {'state': 'active'}))
        self.enterContext(patch('applypilot.database.get_connection', return_value=self.conn))
        self.enterContext(patch.object(company_domain.inputs, 'put', side_effect=lambda conn, value: self.put(value)))
        self.enterContext(patch.object(company_domain.inputs, 'get', side_effect=lambda conn, ref: deepcopy(self.stored[ref])))
        self.enterContext(patch.object(company_domain, '_context', return_value=Context()))

    def put(self, value):
        reference = digest(value)
        self.stored[reference] = deepcopy(value)
        return reference

    def source(self):
        return {'snapshot': digest('snapshot'), 'source': {'id': 'source', 'url': 'https://source.example/feed',
                'kind': 'funding_news'}, 'cursor': {}, 'limit': 8}

    def test_source_issuer_search_and_homepage_reads_return_private_evidence_refs(self):
        value = self.source()
        doc = Document(value['source']['url'], 'Funding feed', 'Acme exists', (), 'company_feed:sec_form_d',
                       structured_data=({'company_name': 'Acme'},))
        root = self.put(value)
        with patch('applypilot.company_discovery.feeds.load', return_value=(doc, None)):
            observed = company_domain.execute(ActivityRequest(root, 'company_source_read'), lambda: None)
        self.assertEqual(len(observed['issuers']), 1)
        self.assertEqual(self.stored[observed['issuers'][0]], {'name': 'Acme'})
        self.assertFalse({'enrichment', 'identified_issuers', 'visited', 'depth'} & self.stored[observed['reference']].keys())
        article_url, homepage = 'https://acme.example/article?token=private', 'https://acme.example/'
        with patch('applypilot.integrations.search.SearchService') as search:
            search.return_value.search.return_value = [SimpleNamespace(url=article_url)]
            candidate = company_domain.execute(ActivityRequest(observed['issuers'][0], 'company_source_enrich',
                options={'operation': 'search', 'provider': 'ddgs'}), lambda: None)['candidates'][0]
        with patch.object(company_domain, '_crawler') as crawler:
            crawler.return_value.read.return_value = Document(article_url, 'Acme article', 'Acme', (homepage,), 'http')
            home_ref = company_domain.execute(ActivityRequest(candidate, 'company_source_enrich',
                options={'operation': 'read', 'document_reference': observed['reference']}), lambda: None)['candidates'][0]
            crawler.return_value.read.return_value = Document(homepage, 'Acme', 'Acme builds products', (), 'http')
            identified = company_domain.execute(ActivityRequest(home_ref, 'company_source_enrich',
                options={'operation': 'read', 'document_reference': observed['reference']}), lambda: None)
        self.assertTrue(identified['identified'])
        self.assertIn(homepage, self.stored[identified['reference']]['document']['links'])
        self.assertNotIn('private', str(observed) + str(identified))
        self.assertFalse(any({'pending', 'visited', 'depth', 'enrichment'} & item.keys() for item in self.stored.values()))

    def test_source_extraction_returns_pages_and_provider_cursor_without_a_plan(self):
        value = self.source()
        detail = 'https://source.example/company?private=secret'
        doc = Document(value['source']['url'], 'Feed', 'Evidence', (detail,), 'company_feed:other')
        value.update(document=doc.to_dict(), native='feed', native_cursor='private-api-cursor')
        root = self.put(value)
        batch = {'source': {}, 'companies': [], 'company_pages': [detail], 'next_page': ''}
        with patch('applypilot.company_discovery.discovery.extract', return_value=batch):
            observed = company_domain.execute(ActivityRequest(root, 'company_source_extract'), lambda: None)
        self.assertEqual(self.stored[observed['details'][0]['reference']]['detail_url'], detail)
        next_page = self.stored[observed['continuation']['reference']]
        self.assertEqual(next_page['cursor'], {'feed_cursor': 'private-api-cursor'})
        self.assertNotIn('private', str(observed))
        self.assertFalse(any({'pending', 'visited', 'depth', 'enrichment'} & item.keys() for item in self.stored.values()))

    def test_vacancy_activity_reads_only_requested_entry_and_returns_new_page_refs(self):
        value = {'snapshot': digest('snapshot'), 'company': {'company': 'Acme', 'careers_url': 'https://acme.example/jobs',
                 'verification': {'matching_jobs': [], 'careers_observations': []}}}
        root = self.put(value)
        page = self.put({'url': 'https://acme.example/jobs?token=private', 'cursor': {'page': 2}})
        next_url = 'https://acme.example/careers'
        with patch('applypilot.discovery.company_targets.vacancy_step') as fetch, \
                patch.object(company_domain, '_crawler', return_value=object()):
            fetch.return_value = ([], [{'provider': 'observed'}],
                                  {'pending': [{'url': next_url, 'depth': 2}], 'visited': ['private-url']})
            observed = company_domain.execute(ActivityRequest(root, 'company_vacancy',
                options={'page_reference': page, 'depth': 1}), lambda: None)
        self.assertEqual(fetch.call_args.args[0], {'pending': [{**self.stored[page], 'depth': 1}], 'visited': []})
        self.assertEqual(observed['pages'][0]['depth'], 2)
        self.assertEqual(self.stored[observed['pages'][0]['reference']], {'url': next_url})
        self.assertEqual(self.stored[observed['reference']]['company']['verification']['careers_observations'],
                         [{'provider': 'observed'}])
        self.assertFalse(any({'pending', 'visited', 'depth', 'traversal'} & item.keys() for item in self.stored.values()))


if __name__ == '__main__':
    unittest.main()

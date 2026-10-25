"""Target business publication stays atomic and preserves owner/profile provenance."""
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from applypilot.orchestration import target_domain
from applypilot.orchestration.contracts import ActivityRequest, digest


@dataclass
class Context:
    reserve_budget: object = None


class TargetObservationTests(unittest.TestCase):
    def setUp(self):
        self.values, self.receipts = {}, {}
        self.conn = SimpleNamespace(commit=lambda: None, in_transaction=True)
        self.target = {'id': 'target', 'revision': 3, 'state': 'active', 'company': 'Acme',
            'website': 'https://acme.example', 'careers_url': 'https://acme.example/jobs', 'job_url': '', 'action': 'watch'}
        self.source = {'provider': 'company_targets', 'scope': 'target', 'kind': 'company_target',
                       'request_params': [['revision', 3]], 'origins': ['target:target']}
        self.root = self.put({'context_key': digest('profile-and-intent')})
        self.input = self.put({'snapshot': self.root, 'target': self.source, 'kind': 'discover'})
        self.enterContext(patch('applypilot.database.get_connection', return_value=self.conn))
        self.enterContext(patch.object(target_domain.inputs, 'get', side_effect=lambda conn, ref: deepcopy(self.values[ref])))
        self.enterContext(patch.object(target_domain.inputs, 'put', side_effect=lambda conn, value: self.put(value)))
        self.enterContext(patch.object(target_domain.inputs, 'context_from', return_value=Context()))
        self.enterContext(patch.object(target_domain.inputs, 'receipt', side_effect=lambda conn, key: self.receipts.get(key)))
        self.enterContext(patch.object(target_domain.inputs, 'save_receipt',
            side_effect=lambda conn, key, value: self.receipts.update({key: deepcopy(value)})))
        self.enterContext(patch('applypilot.targets.store.get', side_effect=lambda *args: deepcopy(self.target)))
        self.enterContext(patch('applypilot.targets.store.boards', return_value=[]))
        self.enterContext(patch('applypilot.company_discovery.source_policy.target_research_allowed', return_value=True))
        self.enterContext(patch('applypilot.discovery.repository.transaction', side_effect=lambda *args, **kwargs: nullcontext()))
        self.records = self.enterContext(patch('applypilot.discovery.repository.publish_records',
                                             return_value=SimpleNamespace(new=1, fetched=1)))
        self.published = self.enterContext(patch('applypilot.discovery.company_targets.published'))
        self.fresh = self.enterContext(patch('applypilot.discovery.freshness.record_complete'))

    def put(self, value):
        reference = digest(value)
        self.values[reference] = deepcopy(value)
        return reference

    def prepare(self):
        return target_domain.execute(ActivityRequest(self.input, 'target_prepare'), lambda: None)

    def test_prepare_returns_exact_inputs_without_persisting_execution_state(self):
        observed = self.prepare()
        self.assertFalse(observed['needs_research'])
        self.assertEqual(len(observed['pages']), 1)
        self.assertEqual(self.values[observed['pages'][0]['reference']], {'url': self.target['careers_url']})
        self.assertFalse(any({'phase', 'pending', 'visited', 'searches', 'urls', 'traversal'} & value.keys()
                             for value in self.values.values()))

    def test_publication_receipt_preserves_profile_origin_and_only_final_completion_marks_freshness(self):
        prepared = self.prepare()
        observation = self.put({'rows': [{'url': 'https://acme.example/jobs/one', 'title': 'Engineer'}],
                                'observations': [{'url': self.target['careers_url'], 'provider': 'http'}]})
        request = ActivityRequest(prepared['reference'], 'target_publish',
                                  options={'reference': observation, 'observation': 'sweep'})
        first = target_domain.execute(request, lambda: None)
        self.assertEqual(first, target_domain.execute(request, lambda: None))
        self.records.assert_called_once()
        self.assertEqual(self.records.call_args.kwargs['origins'], ('target:target',))
        self.assertEqual(self.records.call_args.kwargs['tenant'], 'target')
        self.fresh.assert_not_called()
        report = self.published.call_args.args[1].request_metadata['target_evidence']
        self.assertEqual((report['owner_target_id'], report['target_revision'], report['careers_coverage']),
                         ('target', 3, 'in_progress'))
        completed = ActivityRequest(prepared['reference'], 'target_complete', options={
            'observations': [observation], 'observation': 'sweep', 'coverage_clean': True, 'found': 1, 'new': 1})
        self.assertEqual(target_domain.execute(completed, lambda: None), {'state': 'exhausted'})
        target_domain.execute(completed, lambda: None)
        self.fresh.assert_called_once()
        self.assertEqual(self.fresh.call_args.args[1], self.values[self.root]['context_key'])
        self.assertEqual(self.published.call_args.args[1].request_metadata['target_evidence']['careers_coverage'], 'observed_pages')

    def test_superseded_target_never_fetches_or_publishes(self):
        prepared = self.prepare()
        self.target['revision'] += 1
        with patch('applypilot.integrations.search.SearchService') as search:
            result = target_domain.execute(ActivityRequest(prepared['reference'], 'target_search',
                options={'query_reference': prepared['query_reference'], 'provider': 'ddgs'}), lambda: None)
        self.assertEqual(result, {'state': 'superseded'})
        search.assert_not_called()
        self.records.assert_not_called()

    def test_unknown_vacancy_failure_is_not_converted_to_empty_success(self):
        prepared = self.prepare()
        failure = RuntimeError('unseen provider response')
        with patch('applypilot.discovery.company_targets.vacancy_step', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                target_domain.execute(ActivityRequest(prepared['reference'], 'target_vacancy', options={
                    'page_reference': prepared['pages'][0]['reference'], 'depth': 0, 'backend': 'http',
                    'found': 0, 'last_page': True}), lambda: None)
        self.assertIs(caught.exception, failure)


if __name__ == '__main__':
    unittest.main()

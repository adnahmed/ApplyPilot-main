"""Jobseek inventory and exact posting observations never carry execution queues."""
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from applypilot.discovery import jobseek
from applypilot.discovery.models import Relevance


class JobseekObservationTests(unittest.TestCase):
    def setUp(self):
        self.board = {'company_slug': 'acme', 'board_slug': 'engineering', 'board_url': 'https://acme.example/jobs',
            'monitor_type': 'greenhouse', 'monitor_config': {}, 'scraper_type': 'greenhouse', 'scraper_config': {}}
        self.context = SimpleNamespace(intent=SimpleNamespace(fingerprint=lambda: 'profile'),
                                       reserve_budget=lambda *args, **kwargs: True)
        self.enterContext(patch.object(jobseek, '_runtime', return_value=Path('isolated-fixture-python')))
        self.enterContext(patch.object(jobseek, '_policy'))
        self.enterContext(patch('applypilot.execution.browser_operation', side_effect=nullcontext))
        self.enterContext(patch('applypilot.integrations.worker.minimal_env', return_value={}))
        self.worker = self.enterContext(patch('applypilot.integrations.worker.run_json'))
        self.enterContext(patch('applypilot.discovery.matching._role', return_value=(Relevance.RELEVANT, None)))

    def test_inventory_schedules_independent_postings_and_completes_only_with_all_evidence(self):
        urls = ['https://acme.example/jobs/one', 'https://acme.example/jobs/two']
        self.worker.return_value = {'commit': jobseek.UPSTREAM_COMMIT, 'urls': urls,
                                    'rich': {}, 'authoritative': True, 'partial': False}
        rows, inventory, discovered = jobseek.execute_board(self.board, {'company': 'Acme'}, self.context)
        self.assertEqual(rows, [])
        self.assertEqual([posting['url'] for posting in discovered], urls)
        self.assertFalse(any('urls' in posting or 'cursor' in posting for posting in discovered))
        self.assertNotIn('hiring_snapshot', jobseek.assemble_hiring_observations([inventory])[0])
        observations = [inventory]
        for posting in reversed(discovered):
            self.worker.return_value = {'commit': jobseek.UPSTREAM_COMMIT,
                'records': [{'title': 'Engineer', 'url': posting['url'], 'description': 'Engineering role'}]}
            rows, observed, following = jobseek.execute_board(self.board, {'company': 'Acme'}, self.context, posting)
            self.assertEqual(self.worker.call_args.args[1]['url'], posting['url'])
            self.assertEqual(following, [])
            self.assertEqual(len(rows), 1)
            observations.append(observed)
        incomplete = jobseek.assemble_hiring_observations(observations[:-1])
        self.assertNotIn('hiring_snapshot', incomplete[0])
        complete = jobseek.assemble_hiring_observations(observations)
        snapshot = complete[0]['hiring_snapshot']
        self.assertTrue(snapshot['complete'])
        self.assertEqual(snapshot['total'], 2)
        self.assertEqual({row['url'] for row in snapshot['postings']}, set(urls))

    def test_empty_authoritative_inventory_is_complete_but_partial_inventory_is_not(self):
        self.worker.return_value = {'commit': jobseek.UPSTREAM_COMMIT, 'urls': [], 'authoritative': True}
        _, observed, discovered = jobseek.execute_board(self.board, {'company': 'Acme'}, self.context)
        self.assertEqual(discovered, [])
        self.assertEqual(jobseek.assemble_hiring_observations([observed])[0]['hiring_snapshot']['total'], 0)
        observed['jobseek_inventory']['partial'] = True
        self.assertNotIn('hiring_snapshot', jobseek.assemble_hiring_observations([observed])[0])

    def test_unknown_worker_failure_is_propagated(self):
        failure = RuntimeError('unseen worker failure')
        self.worker.side_effect = failure
        with self.assertRaises(RuntimeError) as caught:
            jobseek.execute_board(self.board, {'company': 'Acme'}, self.context)
        self.assertIs(caught.exception, failure)


if __name__ == '__main__':
    unittest.main()

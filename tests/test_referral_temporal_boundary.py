from contextlib import ExitStack, nullcontext
import sqlite3
import unittest
from unittest.mock import Mock, patch

from applypilot.referrals import discovery
from applypilot.orchestration import utility_workflows
from applypilot.orchestration.contracts import Command, Observation


class ReferralObservationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.execute('CREATE TABLE referral_search_runs(id TEXT PRIMARY KEY, case_id TEXT, provider TEXT, '
                          'started_at TEXT, finished_at TEXT, status TEXT, result_json TEXT)')
        self.addCleanup(self.conn.close)
        stack = self.enterContext(ExitStack())
        case = {'id': 'case', 'job_url': 'https://example.com/job', 'title': 'Engineer', 'plan_json': '{}'}
        for name, value in [('policy', {'mode': 'draft', 'daily_search_limit': 0}),
                            ('require_case', case), ('application_phase', 'not_started'),
                            ('case_subject', {'company': 'Example'})]:
            stack.enter_context(patch.object(discovery.s, name, return_value=value))
        stack.enter_context(patch.object(discovery.s, 'transaction', side_effect=lambda conn: nullcontext(conn)))
        stack.enter_context(patch.object(discovery, 'open_case', return_value=case))
        stack.enter_context(patch.object(discovery, 'list_leads', return_value=[]))
        self.stage = stack.enter_context(patch.object(discovery, 'stage_people',
            return_value={'lead_ids': [], 'rejected_rows': []}))

    def test_zero_legacy_budget_does_not_admit_or_block_temporal_operation(self):
        search = Mock()
        search.search.return_value = []
        result = discovery.discover(self.conn, 'case', operation='people', run_id='observation', search=search)
        self.assertEqual(result['status'], 'no_results')
        self.assertEqual(search.search.call_count, 1)
        self.assertEqual(self.conn.execute('SELECT status FROM referral_search_runs').fetchone()[0], 'no_results')
        replay = discovery.discover(self.conn, 'case', operation='people', run_id='observation', search=search)
        self.assertEqual(replay, result)
        self.assertEqual(search.search.call_count, 1)

    def test_one_connection_observation_never_dispatches_people_search(self):
        social = Mock()
        with patch.object(discovery, 'normalize_people', return_value={'leads': [], 'rejected': []}):
            result = discovery.discover(self.conn, 'case', provider='linkedin_rs', authorized=True,
                operation='connections', run_id='connections', social=social)
        self.assertEqual(result['status'], 'no_results')
        social.read.assert_called_once_with('connections', query=None, limit=10, authorized=True)

    def test_unknown_provider_failure_reaches_temporal_without_success_receipt(self):
        error = RuntimeError('unseen provider failure')
        search = Mock()
        search.search.side_effect = error
        with self.assertRaises(RuntimeError) as failure:
            discovery.discover(self.conn, 'case', operation='people', run_id='failed', search=search)
        self.assertIs(failure.exception, error)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM referral_search_runs').fetchone()[0], 0)
        self.stage.assert_not_called()


class ReferralCommandTests(unittest.TestCase):
    def test_cli_commands_submit_semantics_without_opening_database(self):
        from applypilot.referrals import cli
        from applypilot.orchestration import utility_client
        with patch.object(cli, 'db', side_effect=AssertionError('CLI executed domain work')), \
                patch.object(cli, 'output'), patch.object(utility_client, 'run_utility') as submit:
            cli.discover('case', provider='web', limit=8, authorized=False, start=0)
            self.assertEqual(submit.call_args.args[0], 'referral_discovery')
            self.assertEqual(submit.call_args.args[1]['case_id'], 'case')
            cli.ai_draft('draft')
            submit.assert_called_with('referral_revise', {'outbox_id': 'draft'})
            cli.reconcile('attempt')
            submit.assert_called_with('referral_reconcile', {'outbox_id': 'attempt'})
            cli.github_observation('example')
            submit.assert_called_with('github_person', {'username': 'example'})


class ReferralWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_workflow_owns_order_and_stable_phase_identity(self):
        calls = []
        async def operation(command, phase, **options):
            calls.append((phase, command.page, options['discovery_operation']))
            return Observation('no_results' if command.page == 0 else 'review_required', {'reference': 'receipt'})
        with patch.object(utility_workflows, 'operation', side_effect=operation):
            result = await utility_workflows.UtilityWorkflow().run(
                Command('input', {}, 'referral_discovery', cursor='linkedin_rs'))
        self.assertEqual(result.state, 'review_required')
        self.assertEqual(calls, [('referral_discovery', 0, 'connections'), ('referral_discovery', 1, 'people')])

    async def test_existing_connection_ends_discovery_without_second_provider_call(self):
        with patch.object(utility_workflows, 'operation', return_value=Observation('review_required')) as operation:
            await utility_workflows.UtilityWorkflow().run(
                Command('input', {}, 'referral_discovery', cursor='linkedin_rs'))
        operation.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

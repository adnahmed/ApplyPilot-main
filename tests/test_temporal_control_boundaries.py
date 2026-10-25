import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from typer.testing import CliRunner

from applypilot import config, control
from applypilot.orchestration import client as temporal_client
from applypilot.orchestration.contracts import identity
from applypilot.reasoning.optimizers import compile_mipro, select_optimizer


ACCEPTED = {'workflow_id': 'ap:fixture:job:123', 'accepted': True, 'completed': False}
SCHEDULES = {'workspace': 'fixture', 'schedules': [{'name': 'jobs', 'installed': True, 'paused': False}]}


class CLIControlBoundaryTests(unittest.TestCase):
    def setUp(self):
        from applypilot import cli
        self.cli = cli
        self.runner = CliRunner()
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(patch.object(config, 'APP_DIR', self.root))
        self.enterContext(patch.object(config, 'load_env'))
        self.enterContext(patch('applypilot.operations.runtime.initialize'))
        self.enterContext(patch.object(cli, '_bootstrap', side_effect=AssertionError('Local bootstrap is not a control action')))
        self.job = self.enterContext(patch.object(temporal_client, 'run_job', new_callable=AsyncMock, return_value=ACCEPTED))
        self.work = self.enterContext(patch.object(temporal_client, 'run_work', new_callable=AsyncMock, return_value=ACCEPTED))
        self.install = self.enterContext(patch.object(temporal_client, 'install', new_callable=AsyncMock, return_value=SCHEDULES))

    def invoke(self, arguments):
        result = self.runner.invoke(self.cli.app, arguments)
        self.assertEqual(result.exit_code, 0, f'{result.output}\n{result.exception!r}')
        return json.loads(result.output)

    def test_exact_application_returns_durable_acceptance(self):
        result = self.invoke(['apply', '--url', 'https://jobs.example/role', '--limit', '1', '--dry-run'])
        self.assertEqual(result, ACCEPTED)
        args = self.job.await_args.args
        self.assertEqual(args[0], 'https://jobs.example/role')
        self.assertTrue(args[1]['apply'])
        self.assertTrue(args[1]['dry_run'])
        self.assertEqual(args[1]['limit'], 1)
        self.work.assert_not_awaited()
        self.install.assert_not_awaited()

    def test_discovery_and_preparation_are_separate_native_requests(self):
        result = self.invoke(['run', 'discover', 'score', '--once'])
        self.assertEqual(set(result), {'discovery', 'preparation'})
        self.assertEqual([call.args[0] for call in self.work.await_args_list], ['discover', 'jobs'])
        self.assertEqual(self.work.await_args_list[1].args[1]['job_phases'], ['score'])
        self.install.assert_not_awaited()

    def test_invalid_application_limits_are_rejected_before_submission(self):
        for args in (['apply', '--continuous', '--limit', '2'],
                     ['apply', '--url', 'https://jobs.example/role', '--limit', '2']):
            with self.subTest(arguments=args):
                result = self.runner.invoke(self.cli.app, args)
                self.assertNotEqual(result.exit_code, 0)
        self.job.assert_not_awaited()
        self.work.assert_not_awaited()
        self.install.assert_not_awaited()

    def test_start_restores_saved_options_and_stop_only_pauses_schedules(self):
        with patch.object(control, 'read_json', return_value={'min_score': 8, 'apply': False, 'legacy_workers': 999}), \
             patch.object(temporal_client, 'schedule_status', new_callable=AsyncMock, return_value=SCHEDULES), \
             patch.object(temporal_client, 'change_schedules', new_callable=AsyncMock) as change, \
             patch.object(temporal_client, 'cancel', new_callable=AsyncMock) as cancel:
            started = self.invoke(['start', '--json', '--no-open'])
            self.assertEqual(started['execution_owner'], 'Temporal')
            self.install.assert_awaited_once_with({'min_score': 8, 'apply': False}, start=True, root=self.root)
            stopped = self.invoke(['stop', '--json'])
            self.assertFalse(stopped['existing_workflows_cancelled'])
            change.assert_awaited_once_with('pause', root=self.root)
            cancel.assert_not_awaited()

    def test_native_pacing_command_invokes_update_client(self):
        with patch.object(temporal_client, 'update_application_pacing', new_callable=AsyncMock,
                          return_value={'usage_history_preserved': True}) as update:
            result = self.invoke(['temporal', 'application-pacing', 'primary', '--spacing-seconds', '1200',
                                  '--hourly-limit', '2', '--daily-limit', '10', '--restriction-retry-seconds', '172800'])
        self.assertTrue(result['usage_history_preserved'])
        update.assert_awaited_once_with('primary', spacing_seconds=1200, hourly_limit=2, daily_limit=10,
                                       restriction_retry_seconds=172800)


class DashboardControlBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from applypilot.web.server import API, Security
        self.root = Path(self.enterContext(TemporaryDirectory()))
        api = API.__new__(API)
        api.app_dir, api.read_only, api.dashboard_owner = self.root, False, None
        api.security = Security('isolated-dashboard-token', 8399)
        self.api = api
        self.install = self.enterContext(patch.object(temporal_client, 'install', new_callable=AsyncMock, return_value=SCHEDULES))
        self.change = self.enterContext(patch.object(temporal_client, 'change_schedules', new_callable=AsyncMock))
        self.cancel = self.enterContext(patch.object(temporal_client, 'cancel', new_callable=AsyncMock,
                                                    return_value={'cancellation_requested': True}))
        self.enterContext(patch.object(temporal_client, 'schedule_status', new_callable=AsyncMock, return_value=SCHEDULES))
        self.enterContext(patch.object(control, 'read_json', return_value={'min_score': 7}))
        self.http = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api.application()), base_url='http://127.0.0.1:8399'))

    async def unlock(self):
        response = await self.http.post('/api/session', json={'token': 'isolated-dashboard-token'})
        self.assertEqual(response.status_code, 200)
        self.http.headers['x-applypilot-csrf'] = response.json()['csrf']

    async def test_authenticated_start_and_pause_route_to_temporal(self):
        await self.unlock()
        started = await self.http.post('/api/autopilot/control', json={'action': 'start'})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()['execution_owner'], 'Temporal')
        self.install.assert_awaited_once_with({'min_score': 7, 'apply': False}, start=True, root=self.root)
        paused = await self.http.post('/api/autopilot/control', json={'action': 'pause'})
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertFalse(paused.json()['existing_workflows_cancelled'])
        self.change.assert_awaited_once_with('pause', root=self.root)
        self.cancel.assert_not_awaited()

    async def test_cancel_names_exact_workflow_and_does_not_pause_other_work(self):
        await self.unlock()
        response = await self.http.post('/api/temporal/cancel', json={'workflow_id': ACCEPTED['workflow_id']})
        self.assertEqual(response.status_code, 200, response.text)
        self.cancel.assert_awaited_once_with(ACCEPTED['workflow_id'], root=self.root)
        self.change.assert_not_awaited()
        self.install.assert_not_awaited()

    async def test_missing_session_csrf_and_read_only_never_send_commands(self):
        response = await self.http.post('/api/autopilot/control', json={'action': 'start'})
        self.assertEqual(response.status_code, 401)
        await self.unlock()
        csrf = self.http.headers.pop('x-applypilot-csrf')
        response = await self.http.post('/api/autopilot/control', json={'action': 'start'})
        self.assertEqual(response.status_code, 403)
        self.http.headers['x-applypilot-csrf'] = csrf
        self.api.read_only = True
        response = await self.http.post('/api/autopilot/control', json={'action': 'start'})
        self.assertEqual(response.status_code, 403)
        self.install.assert_not_awaited()


class TemporalClientBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_pacing_updates_existing_workflow_without_starting_one(self):
        handle = SimpleNamespace(execute_update=AsyncMock(return_value={'spacing_seconds': 900}))
        native = SimpleNamespace(get_workflow_handle=Mock(return_value=handle), start_workflow=AsyncMock())
        with patch.object(temporal_client, 'load', return_value=SimpleNamespace(workspace='fixture')), \
             patch.object(temporal_client, 'connect', new_callable=AsyncMock, return_value=native):
            result = await temporal_client.update_application_pacing('primary', spacing_seconds=900,
                                                                     hourly_limit=3, daily_limit=15)
        native.get_workflow_handle.assert_called_once_with(identity('fixture', 'linkedin-applications', 'primary'))
        handle.execute_update.assert_awaited_once()
        self.assertEqual(handle.execute_update.await_args.args[1],
                         {'spacing_seconds': 900, 'hourly_limit': 3, 'daily_limit': 15})
        self.assertTrue(result['usage_history_preserved'])
        native.start_workflow.assert_not_awaited()

    async def test_cancellation_rejects_other_workspace_before_connecting(self):
        with patch.object(temporal_client, 'load', return_value=SimpleNamespace(workspace='fixture')), \
             patch.object(temporal_client, 'connect', new_callable=AsyncMock) as connect:
            with self.assertRaises(ValueError):
                await temporal_client.cancel('ap:another:job:123')
        connect.assert_not_awaited()

    async def test_schedule_names_are_validated_before_any_remote_write(self):
        policy = SimpleNamespace(workspace='fixture', schedules={'jobs': SimpleNamespace(enabled=True)})
        with patch.object(temporal_client, 'load', return_value=policy), \
             patch.object(temporal_client, 'connect', new_callable=AsyncMock) as connect:
            with self.assertRaises(ValueError):
                await temporal_client.change_schedules('pause', ('jobs', 'unknown'))
        connect.assert_not_awaited()


class OptimizerBoundaryTests(unittest.TestCase):
    def test_completed_rounds_alternate_and_failed_rounds_retry_same_optimizer(self):
        for state in ('promoted', 'candidate', 'rejected', 'unchanged'):
            for previous, expected in (('gepa', 'mipro'), ('mipro', 'gepa')):
                with self.subTest(state=state, previous=previous):
                    self.assertEqual(select_optimizer('browser_observation', {'state': state, 'optimizer': previous}), expected)
        for state in ('failed', 'interrupted'):
            for previous in ('gepa', 'mipro'):
                self.assertEqual(select_optimizer('browser_observation', {'state': state, 'optimizer': previous}), previous)
        self.assertEqual(select_optimizer('score', {'state': 'promoted', 'optimizer': 'gepa'}), 'gepa')
        self.assertEqual(select_optimizer('browser_observation', {'state': 'promoted'}, enabled=False), 'gepa')

    def test_mipro_uses_installed_public_api_and_only_train_validation(self):
        from applypilot.reasoning.dspy_runtime import dspy, GatewayLM
        train, validation, holdout = [object()], [object()], [object()]
        evaluator = SimpleNamespace(assess=Mock(return_value={'score': 0.75}))
        reflection = GatewayLM(SimpleNamespace(model='fixture'))
        candidate = SimpleNamespace(score=0.75)
        report = {}
        with patch.object(dspy.MIPROv2, 'compile', autospec=True, return_value=candidate) as compile:
            result = compile_mipro(object(), {'train': train, 'validation': validation, 'holdout': holdout},
                                   evaluator, reflection, {'mipro_candidates': 2, 'mipro_trials': 1}, report)
        self.assertIs(result, candidate)
        self.assertIs(compile.call_args.kwargs['trainset'], train)
        self.assertIs(compile.call_args.kwargs['valset'], validation)
        self.assertNotIn('holdout', compile.call_args.kwargs)
        optimizer = compile.call_args.args[0]
        self.assertEqual(optimizer.metric('gold', 'prediction'), 0.75)
        evaluator.assess.assert_called_once_with('gold', 'prediction')
        self.assertEqual(report['optimizer_api'], 'dspy.MIPROv2.compile')

    def test_proposal_adapter_validates_schema_and_records_failure_without_retry(self):
        from applypilot.reasoning.dspy_runtime import dspy, ProposalAdapter
        from applypilot.reasoning.budget import InferenceContext

        class Proposal(dspy.Signature):
            evidence: str = dspy.InputField()
            instruction: str = dspy.OutputField()

        lm = Mock(return_value=['{"instruction": "Preserve evidence"}'], budget=InferenceContext())
        adapter = ProposalAdapter()
        result = adapter(lm, {}, Proposal, [], {'evidence': 'fixture'})
        self.assertEqual(result, [{'instruction': 'Preserve evidence'}])
        self.assertEqual(lm.call_args.kwargs['response_format']['json_schema']['schema']['required'], ['instruction'])
        lm.reset_mock()
        lm.return_value = ['{}']
        with self.assertRaisesRegex(RuntimeError, 'Optimizer proposal failed'):
            adapter(lm, {}, Proposal, [], {'evidence': 'fixture'})
        lm.assert_called_once()
        with self.assertRaises(Exception):
            lm.budget.remaining()

    def test_gepa_public_api_never_receives_holdout_examples(self):
        from applypilot.reasoning import learning_compile
        from applypilot.reasoning.dspy_runtime import dspy
        dataset = {name: name for name in ('learning_run_id', 'dataset_id', 'program_id', 'optimizer_run_id', 'schema_hash')}
        dataset.update(task='score', optimizer='gepa', settings={'metric_calls': 8},
                       instructions='previous instruction', schema={'type': 'object'}, params={})
        examples = {'train': [object()], 'validation': [object()]}
        candidate = SimpleNamespace(generate=SimpleNamespace(signature=SimpleNamespace(instructions='improved instruction')))
        with patch.object(learning_compile, '_program', return_value=object()), \
             patch.object(learning_compile, '_examples', side_effect=lambda dataset, split: examples[split]) as example_reader, \
             patch.object(learning_compile, 'Evaluator'), \
             patch('applypilot.llm.get_client', return_value=SimpleNamespace(model='fixture')), \
             patch.object(dspy.GEPA, 'compile', autospec=True, return_value=candidate) as compile:
            report = learning_compile.optimize_dataset(dataset, threading.Event())
        self.assertEqual([call.args[1] for call in example_reader.call_args_list], ['train', 'validation'])
        self.assertIs(compile.call_args.kwargs['trainset'], examples['train'])
        self.assertIs(compile.call_args.kwargs['valset'], examples['validation'])
        self.assertEqual(report['optimizer_api'], 'dspy.GEPA.compile')
        self.assertEqual(report['state'], 'optimized')
        self.assertNotIn('approved', report)


if __name__ == '__main__':
    unittest.main()

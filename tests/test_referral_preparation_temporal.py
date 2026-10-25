"""Referral case selection and model work survive independent Activity retries."""
import asyncio
from contextlib import nullcontext
from dataclasses import asdict
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from applypilot.orchestration import referral_preparation as domain, utility_workflows
from applypilot.orchestration.contracts import ActivityRequest, Command, Observation


class Continued(BaseException):
    pass


class ReferralPreparationTests(unittest.TestCase):
    def test_workflow_preserves_selection_and_advances_only_completed_case_across_continuation(self):
        phases, continued = [], []
        command = Command('owner-request', {'pages_per_run': 1}, 'referral_prepare')

        async def operation(command, phase, **options):
            phases.append((phase, command.reference, command.page))
            if phase == 'referral_inventory':
                return Observation('inventoried', {'selection_reference': 'selected-cases'})
            if phase == 'referral_case_prepare':
                return Observation('prepared' if command.page == 0 else 'complete')
            return Observation('complete', {'reference': 'finished-result'})

        def continue_as_new(value):
            continued.append(Command(**json.loads(json.dumps(asdict(value)))))
            raise Continued()

        with patch.object(utility_workflows, 'operation', operation), \
                patch.object(utility_workflows.workflow, 'continue_as_new', continue_as_new):
            with self.assertRaises(Continued):
                asyncio.run(utility_workflows.UtilityWorkflow().run(command))
            self.assertEqual(continued[0].page, 1)
            self.assertEqual(continued[0].reference, 'selected-cases')
            result = asyncio.run(utility_workflows.UtilityWorkflow().run(continued[0]))
        self.assertEqual([phase for phase, _, _ in phases], ['referral_inventory', 'referral_case_prepare',
            'referral_case_revise', 'referral_case_prepare', 'referral_prepare_finish'])
        self.assertEqual(result.detail['reference'], 'finished-result')

    def test_failed_model_does_not_advance_or_complete_the_case(self):
        command = Command('selection', {'pages_per_run': 8}, 'referral_plan', cursor='cases', page=3)
        phases = []

        async def operation(command, phase, **options):
            phases.append(phase)
            if phase == 'referral_case_revise':
                raise RuntimeError('unseen model failure')
            return Observation('prepared')

        with patch.object(utility_workflows, 'operation', operation), self.assertRaisesRegex(RuntimeError, 'unseen'):
            asyncio.run(utility_workflows.UtilityWorkflow().run(command))
        self.assertEqual(command.page, 3)
        self.assertEqual(phases, ['referral_case_prepare', 'referral_case_revise'])

    def test_selection_receipt_survives_activity_response_loss(self):
        from applypilot.referrals import planner, store
        conn, receipts, captured = MagicMock(), {}, []
        request = ActivityRequest('owner-request', 'referral_inventory')
        value = {'operation': 'referral_plan', 'payload': {'limit': 2}}
        with patch('applypilot.database.get_connection', return_value=conn), \
                patch('applypilot.config.load_profile', return_value={'name': 'fixture'}), \
                patch.object(store, 'transaction', side_effect=lambda conn: nullcontext(conn)), \
                patch.object(domain.store, 'receipt', side_effect=lambda conn, key: receipts.get(key)), \
                patch.object(domain.store, 'save_receipt', side_effect=lambda conn, key, value: receipts.update({key: value})), \
                patch.object(domain.store, 'put', side_effect=lambda conn, value: captured.append(value) or 'selection'), \
                patch.object(planner, 'plan_jobs', return_value=[{'id': 'first'}, {'id': 'second'}]) as select:
            first = domain.perform(request, value)
            second = domain.perform(request, value)
        self.assertEqual(first, second)
        select.assert_called_once()
        self.assertEqual(captured[0]['cases'], ['first', 'second'])

    def test_case_preparation_receipt_keeps_draft_identity_and_never_calls_model(self):
        from applypilot.referrals import planner, store
        conn, receipts = MagicMock(), {}
        request = ActivityRequest('selection', 'referral_case_prepare', sequence=0)
        value = {'operation': 'referral_prepare', 'cases': ['case-1'], 'profile': {'name': 'fixture'}}
        with patch('applypilot.database.get_connection', return_value=conn), \
                patch.object(store, 'transaction', side_effect=lambda conn: nullcontext(conn)), \
                patch.object(store, 'require_case', return_value={'id': 'case-1'}), \
                patch.object(domain.store, 'receipt', side_effect=lambda conn, key: receipts.get(key)), \
                patch.object(domain.store, 'save_receipt', side_effect=lambda conn, key, value: receipts.update({key: value})), \
                patch.object(planner, 'maybe_hold', return_value=None), \
                patch.object(planner, 'create_initial_drafts', return_value=[{'id': 'draft-1'}]) as draft:
            first = domain.perform(request, value)
            second = domain.perform(request, value)
        self.assertEqual(first, second)
        draft.assert_called_once_with(conn, [{'id': 'case-1'}], value['profile'], improve=False)
        self.assertEqual(first[0]['draft_ids'], ['draft-1'])

    def test_model_failure_propagates_without_marking_preparation_complete(self):
        from applypilot.referrals import planner, outreach
        request = ActivityRequest('selection', 'referral_case_revise')
        value = {'operation': 'referral_prepare', 'profile': {}}
        with patch('applypilot.database.get_connection', return_value=MagicMock()), \
                patch.object(domain.store, 'receipt', return_value={'case_id': 'case-1', 'draft_ids': ['draft-1']}), \
                patch.object(outreach, '_outbox', return_value={'id': 'draft-1', 'state': 'draft'}), \
                patch('applypilot.integrations.referrals.improve_draft', side_effect=RuntimeError('provider failed')), \
                patch.object(planner, 'prepared_case') as complete, self.assertRaisesRegex(RuntimeError, 'provider failed'):
            domain.perform(request, value)
        complete.assert_not_called()

    def test_cli_plan_and_prepare_only_submit_owner_requests(self):
        from applypilot.referrals import cli
        with patch('applypilot.orchestration.utility_client.run_utility', return_value={}) as submit, \
                patch.object(cli, 'output'), patch.object(cli, 'db') as database:
            cli.plan(limit=2)
            cli.prepare(limit=3)
        self.assertEqual([call.args for call in submit.call_args_list], [
            ('referral_plan', {'limit': 2}), ('referral_prepare', {'limit': 3})])
        database.assert_not_called()


if __name__ == '__main__':
    unittest.main()

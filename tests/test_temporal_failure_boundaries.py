from contextlib import nullcontext
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from temporalio.exceptions import ApplicationError

from applypilot.documents.contracts import PrepareDocument, RenderDocument
from applypilot.integrations.types import SourceError
from applypilot.orchestration import activities, document_activities, signal_activities
from applypilot.orchestration.activity_runtime import provider_retry_delay
from applypilot.orchestration.account_workflows import ApplicationAccountWorkflow
from applypilot.signals.contracts import SignalInput


class FailureBoundaryTests(unittest.TestCase):
    def test_generic_operation_preserves_delay_without_provider_error_context(self):
        error = RuntimeError('private provider payload')
        error.retry_after = 73
        request = SimpleNamespace(phase='test', cursor=None)
        database = ModuleType('applypilot.database')
        database.get_connection = Mock()
        boundary = activities.Activities('test')
        with patch.dict('sys.modules', {'applypilot.database': database}), \
                patch.object(boundary, '_check'), \
                patch.object(activities, 'execution', return_value=nullcontext((None, None))):
            with self.assertRaises(ApplicationError) as failure:
                boundary._invoke(request, Mock(side_effect=error))
        self.assertEqual(failure.exception.next_retry_delay.total_seconds(), 73)
        self.assertFalse(failure.exception.non_retryable)
        self.assertIsNone(failure.exception.type)
        self.assertIsNone(failure.exception.__context__)
        self.assertNotIn('private provider payload', str(failure.exception))

    def test_invalid_provider_delay_leaves_timing_to_temporal_policy(self):
        for value in (None, True, -1, 0, '73', float('nan'), float('inf'), 1e300, 10**400):
            with self.subTest(value=value):
                self.assertIsNone(provider_retry_delay(SimpleNamespace(retry_after=value)))

    def test_http_provider_header_reaches_native_retry_delay(self):
        import httpx
        request = httpx.Request('GET', 'https://example.com/provider')
        response = httpx.Response(429, request=request, headers={'Retry-After': '120'})
        error = httpx.HTTPStatusError('Provider restriction', request=request, response=response)
        self.assertEqual(provider_retry_delay(error).total_seconds(), 120)

    def test_unexpected_nested_document_errors_remain_retryable(self):
        for error in (ValueError('provider response'), KeyError('unknown field'),
                      TypeError('provider regression'), LookupError('missing provider evidence')):
            with self.subTest(error=type(error).__name__):
                service = ModuleType('applypilot.documents.service')
                service.prepare = Mock(side_effect=error)
                with patch.dict('sys.modules', {'applypilot.documents.service': service}):
                    with self.assertRaises(ApplicationError) as failure:
                        document_activities.prepare_document(PrepareDocument('a' * 64, 'tailor'))
                self.assertFalse(failure.exception.non_retryable)
                self.assertIsNone(failure.exception.type)

    def test_invalid_document_request_never_calls_renderer(self):
        service = ModuleType('applypilot.documents.service')
        service.render = Mock()
        with patch.dict('sys.modules', {'applypilot.documents.service': service}):
            with self.assertRaises(ApplicationError) as failure:
                document_activities.render_document(RenderDocument('invalid', 'pdf'))
        self.assertTrue(failure.exception.non_retryable)
        service.render.assert_not_called()

    def test_unexpected_signal_error_is_not_input_failure(self):
        operation = Mock(side_effect=ValueError('previously unseen provider reply'))
        with patch.object(signal_activities, 'heartbeats', return_value=nullcontext(lambda: None)):
            with self.assertRaises(ApplicationError) as failure:
                signal_activities._perform(SignalInput('a' * 64), operation)
        self.assertFalse(failure.exception.non_retryable)

    def test_provider_delay_is_preserved_without_permanent_error_taxonomy(self):
        error = SourceError('provider response', 'auth', retry_after=37)
        with patch.object(signal_activities, 'heartbeats', return_value=nullcontext(lambda: None)):
            with self.assertRaises(ApplicationError) as failure:
                signal_activities._perform(SignalInput('a' * 64), Mock(side_effect=error))
        self.assertFalse(failure.exception.non_retryable)
        self.assertEqual(failure.exception.next_retry_delay.total_seconds(), 37)

    def test_invalid_signal_input_never_calls_provider(self):
        operation = Mock()
        with self.assertRaises(ApplicationError) as failure:
            signal_activities._perform(SignalInput('invalid'), operation)
        self.assertTrue(failure.exception.non_retryable)
        operation.assert_not_called()


class AccountPacingTests(unittest.TestCase):
    def test_update_changes_policy_without_resetting_identity_or_history(self):
        account = ApplicationAccountWorkflow()
        account.handled = {'previous-attempt': 1000}
        account.active = 'current-attempt'
        pacing = {'spacing_seconds': 1200, 'hourly_limit': 2, 'daily_limit': 10,
                  'restriction_retry_seconds': 86400}
        self.assertEqual(account.set_pacing(pacing), pacing)
        self.assertEqual(account.handled, {'previous-attempt': 1000})
        self.assertEqual(account.active, 'current-attempt')
        self.assertEqual(account.pacing_revision, 1)
        pacing['daily_limit'] = 1
        self.assertEqual(account.status()['pacing']['daily_limit'], 10)

    def test_invalid_update_leaves_policy_unchanged(self):
        account = ApplicationAccountWorkflow()
        pacing = {'spacing_seconds': 600, 'hourly_limit': 4, 'daily_limit': 20,
                  'restriction_retry_seconds': 86400}
        account.set_pacing(pacing)
        for bad in ({**pacing, 'hourly_limit': True}, {**pacing, 'spacing_seconds': float('nan')},
                    {**pacing, 'daily_limit': 0}, {**pacing, 'unknown': 1}):
            with self.assertRaises(ValueError):
                account.set_pacing(bad)
            self.assertEqual(account.pacing, pacing)
            self.assertEqual(account.pacing_revision, 1)


if __name__ == '__main__':
    unittest.main()

"""One source operation can finish without obsolete application request budgets."""
from io import BytesIO, StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from applypilot.discovery import jobspy, jobspy_transport as transport
from applypilot.discovery.models import SearchTask, SourceTarget
from applypilot.execution import ExecutionStopped


class JobSpyTemporalTransportTests(unittest.TestCase):
    def test_parent_accepts_more_than_old_http_ceiling_and_checks_cancellation_each_time(self):
        events = [{'type': 'http', 'url': 'https://www.indeed.com/jobs', 'method': 'GET',
                   'listing_pages': number} for number in range(205)]
        events.append({'type': 'done', 'rows': [], 'native_page': True, 'next_cursor': 'following'})
        process = MagicMock()
        process.stdin = BytesIO()
        process.stdout = BytesIO(('\n'.join(json.dumps(event) for event in events) + '\n').encode())
        process.poll.return_value = 0
        context = SimpleNamespace(runtime={'http_budget': 0, 'max_pages_per_task': 0},
                                  reserve_budget=MagicMock(return_value=True))
        with patch.object(transport.subprocess, 'Popen', return_value=process):
            observed = transport.scrape_snapshot({'results_wanted': 100}, context, native_page=True)
        self.assertEqual(observed.failures, ())
        self.assertEqual(observed.metadata['http_attempts'], 205)
        self.assertEqual(observed.metadata['next_native_cursor'], 'following')
        self.assertEqual(context.reserve_budget.call_count, 205)
        process.wait.assert_called_once()

    def test_cancelled_wire_handshake_stops_the_process(self):
        event = {'type': 'http', 'url': 'https://www.indeed.com/jobs', 'listing_pages': 1}
        process = MagicMock()
        process.stdin, process.stdout = BytesIO(), BytesIO((json.dumps(event) + '\n').encode())
        process.poll.return_value = None
        context = SimpleNamespace(runtime={}, reserve_budget=MagicMock(side_effect=ExecutionStopped('cancelled')))
        with patch.object(transport.subprocess, 'Popen', return_value=process), self.assertRaises(ExecutionStopped):
            transport.scrape_snapshot({'results_wanted': 100}, context)
        process.kill.assert_called_once()
        process.wait.assert_called_once()

    def test_zero_legacy_budgets_do_not_hide_an_enabled_source(self):
        context = SimpleNamespace(cycle_id='fixture', runtime={'http_budget': 0, 'request_budget': 0,
            'max_pages_per_task': 0}, intent=SimpleNamespace(sources_enabled=('jobspy',), jobspy_sites=('indeed',)),
            reserve_budget=lambda *args: True)
        target = SourceTarget('jobspy', scope='indeed', request_params=(('query', 'engineer'), ('country', 'usa')))
        snapshot = transport.Snapshot((), (), {'native_page': True, 'next_native_cursor': None})
        with patch.object(transport, 'scrape_snapshot', return_value=snapshot) as read, \
                patch.object(jobspy, 'version', return_value=transport.VERSION):
            result = jobspy.execute_task(SearchTask(task_id='fixture', target=target), context)
        read.assert_called_once()
        self.assertFalse(result.failures)
        self.assertNotIn('no_work', result.request_metadata)

    def test_child_can_read_more_than_old_listing_page_ceiling(self):
        wire, error = StringIO(), StringIO()
        payload = {'kwargs': {'site_name': ['indeed'], 'country_indeed': 'usa', 'results_wanted': 1},
                   'seconds': 120, 'native_page': False, 'max_pages': 0}
        inbound = StringIO(json.dumps(payload) + '\n' + '{"allowed":true}\n' * 12)
        response = SimpleNamespace(status_code=200, headers={}, text='Observed result', url='https://www.indeed.com/jobs')
        session = SimpleNamespace(headers={}, cookies=SimpleNamespace(clear=lambda: None),
                                  execute_request=lambda *args, **kwargs: response)

        class Scraper:
            def __init__(self, **kwargs):
                self.session = module.create_session()

            def _process_job(self, *args, **kwargs):
                return None

            def scrape(self, inputs):
                for _ in range(12):
                    self.session.get('https://www.indeed.com/jobs')
                return SimpleNamespace(jobs=[])

        module = SimpleNamespace(Indeed=Scraper, create_session=None)
        with patch.object(transport.sys, 'stdout', wire), patch.object(transport.sys, 'stderr', error), \
                patch.object(transport.sys, 'stdin', inbound), \
                patch.object(transport, 'version', return_value=transport.VERSION), \
                patch.object(transport, 'import_module', return_value=module), \
                patch('tls_client.Session', return_value=session):
            transport._worker()
        events = [json.loads(line) for line in wire.getvalue().splitlines()]
        self.assertEqual(sum(event['type'] == 'http' for event in events), 12)
        self.assertEqual(events[-1]['type'], 'done')
        self.assertFalse(any(event['type'] == 'failure' for event in events))


if __name__ == '__main__':
    unittest.main()

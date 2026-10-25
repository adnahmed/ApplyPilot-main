import unittest

from applypilot.discovery.builtin import _failure_page
from applypilot.discovery.transport import deferred_page


class DiscoveryFailureTests(unittest.TestCase):
    def test_unseen_provider_exceptions_are_not_reclassified_as_schema_failures(self):
        class UnexpectedProviderFailure(Exception):
            pass

        for adapter in (_failure_page, deferred_page):
            for error in (UnexpectedProviderFailure('unseen provider failure'),
                          ValueError('provider payload changed'), TimeoutError('upstream stopped responding')):
                with self.subTest(adapter=adapter.__name__, exception=type(error).__name__):
                    with self.assertRaises(type(error)) as raised:
                        adapter(None, None, error)
                    self.assertIs(raised.exception, error)


if __name__ == '__main__':
    unittest.main()

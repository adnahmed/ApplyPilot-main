"""Research observes one Twikit page without hidden pagination or sleeping."""
import unittest
from unittest.mock import AsyncMock, patch

from applypilot.integrations import worker_main


class Page(list):
    next_cursor = 'another-provider-page'
    next = AsyncMock()


class SocialPageObservationTests(unittest.TestCase):
    def test_research_reads_one_page_even_when_requested_count_is_larger(self):
        page = Page([{'id': str(index)} for index in range(20)])
        with patch.object(worker_main, 'tweet_row', side_effect=lambda tweet: tweet):
            result = worker_main.tweet_page(page, 100)
        self.assertEqual(result, list(page))
        page.next.assert_not_called()

    def test_duplicate_rows_and_explicit_output_selection(self):
        page = Page([{'id': 'one'}, {'id': 'one'}, {'id': 'two'}, {'id': 'three'}])
        with patch.object(worker_main, 'tweet_row', side_effect=lambda tweet: tweet):
            result = worker_main.tweet_page(page, 2)
        self.assertEqual(result, [{'id': 'one'}, {'id': 'two'}])
        page.next.assert_not_called()


if __name__ == '__main__':
    unittest.main()

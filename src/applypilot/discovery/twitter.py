"""X hiring signals from authorized exports or free/local read-only connectors.

runtime.source_inputs.twitter.mode: auto (default), export, twikit,
xactions, x_use. The metered bearer-token API has been removed; there is no paid fallback.
"""
from __future__ import annotations

from .free_signals import execute as execute_task
from .signal_cache import parse_markdown, run_source


def _parse_tweets_from_markdown(markdown_text: str) -> list[dict]:
    return parse_markdown(markdown_text, "twitter")


def run_twitter_discovery(*, context=None, workers=1) -> dict:
    return run_source("twitter", context=context, workers=workers)

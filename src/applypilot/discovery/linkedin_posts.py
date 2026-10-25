"""Authorized LinkedIn export/local reader signals; no hosted enrichment API."""
from __future__ import annotations

from .free_signals import execute as execute_task
from .signal_cache import parse_markdown, run_source


def _parse_posts_from_markdown(markdown_text: str) -> list[dict]:
    return parse_markdown(markdown_text, "linkedin_posts")


async def _search_posts_async(task, context, cursor=None):
    import asyncio
    return await asyncio.to_thread(execute_task, task, context, cursor)


def run_linkedin_posts_discovery(*, context=None, workers=1) -> dict:
    return run_source("linkedin_posts", context=context, workers=workers)

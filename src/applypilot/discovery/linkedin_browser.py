"""Explicitly authorized, exact-task LinkedIn recovery through BrowserOS neo.

An enabled source is not permission to bypass LinkedIn restrictions. No Seattle
defaults, search matrices, page-wide Easy Apply inference, or submission tabs.
"""

from urllib.parse import urlencode


def _build_linkedin_search_url(query: str, location: str | None = None,
                               start: int = 0, easy_apply: bool = False) -> str:
    if not isinstance(query, str) or not query.strip() or type(start) is not int or start < 0:
        raise ValueError("A query and nonnegative page offset are required")
    params = {"keywords": query, "start": start}
    if location:
        params["location"] = location
    if easy_apply:
        params["f_AL"] = "true"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


def execute_task(task, context, cursor=None):
    from .recovery import execute_task as recover
    return recover(task, context, cursor, executor="browseros_linkedin")


def run_browseros_linkedin_discovery(*, task=None, context=None, cursor=None, workers=1):
    """Finite exact-task compatibility seam; batch recovery migration is unfinished."""
    if task is None or context is None:
        raise ValueError('The local browseros_linkedin recovery scheduler is retired. '
                         'Supply one authorized task and its context to execute_task; '
                         'native recovery traversal remains part of the unfinished Part 2 scope.')
    if workers != 1:
        raise ValueError('Configure concurrency on the Temporal worker deployment')
    return execute_task(task, context, cursor)

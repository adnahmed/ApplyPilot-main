"""Official HN WhoIsHiring submitted-list/item discovery, with resumable signals.

Reference: https://github.com/HackerNews/API . No Algolia or HTML scraping.
The cursor freezes the current thread's top-level comment IDs for one refresh;
a new scheduler generation re-fetches the thread and detects edited comments.
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
from applypilot import postgres

from .models import DiscoveryPage, FailureKind, SearchTask
from .signal_cache import (
    cache_for, checkpoint_page, decode_cursor, normalize_post, page_limit,
    prefilter, run_source, signal_records,
)
from .transport import TaskDeferred, request_json, task_params

_HN_USER = "https://hacker-news.firebaseio.com/v0/user/whoishiring.json"
_HN_ITEM = "https://hacker-news.firebaseio.com/v0/item/{id}.json"


def _ids(value):
    if not isinstance(value, list) or any(type(item) is not int or item <= 0 for item in value):
        raise ValueError("HN IDs must be an array of positive integers")
    return list(dict.fromkeys(value))


def _fetch_item(item_id, context):
    if type(item_id) is not int or item_id <= 0:
        raise ValueError("HN item ID must be a positive integer")
    item = request_json(context, _HN_ITEM.format(id=item_id))
    if item is not None and (not isinstance(item, dict) or item.get("id") != item_id):
        raise ValueError("HN item response does not match the requested ID")
    return item


def _find_latest_thread(context, cache, state, limit):
    """Scan real submitted story IDs newest-first; checkpoint even mid-scan."""
    if "submitted" not in state:
        user = request_json(context, _HN_USER)
        if not isinstance(user, dict) or user.get("id", "").lower() != "whoishiring":
            raise ValueError("Invalid WhoIsHiring user response")
        submitted = sorted(_ids(user.get("submitted")), reverse=True)
        state.update(submitted=cache.snapshot(submitted), scan=0)
    submitted = cache.load_snapshot(state["submitted"])
    index = state.get("scan")
    if type(index) is not int or not 0 <= index <= len(submitted):
        raise ValueError("Invalid HN submitted-list cursor")
    for item_id in submitted[index:index + limit]:
        item = _fetch_item(item_id, context)
        if (item and not item.get("deleted") and not item.get("dead") and
                item.get("type") == "story" and item.get("by", "").lower() == "whoishiring" and
                re.match(r"^Ask HN:\s*Who is hiring\?\s*\(", item.get("title", ""), re.I)):
            _ids(item.get("kids", []))
            state.update(thread=cache.snapshot(item), index=0)
            return item
        state["scan"] += 1
    if state["scan"] == len(submitted):
        raise TaskDeferred("No current canonical WhoIsHiring thread in submitted items", FailureKind.SCHEMA)
    return None


def _prefilter_comment(text, context=None):
    """Role/intent prefilter, never a geographic keyword whitelist."""
    return prefilter(text, context, hiring_thread=True)


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    state, records, leads = {}, [], []
    try:
        params = task_params(task, ("hackernews",))
        if set(params) - {"query"}:
            raise TaskDeferred("HN accepts only a local role probe; thread discovery uses official items")
        if context.runtime.get("authorized_sources", {}).get("hackernews") is False:
            raise TaskDeferred("HN access explicitly denied", FailureKind.ACCESS_DENIED)
        state = decode_cursor(task, cursor)
        cache = cache_for(context)
        limit = page_limit(context)
        thread = cache.load_snapshot(state["thread"]) if "thread" in state else _find_latest_thread(
            context, cache, state, limit)
        if thread is None:
            return checkpoint_page(task, context, state, records, leads)
        kids = _ids(thread.get("kids", []))
        index = state.get("index")
        if type(index) is not int or not 0 <= index <= len(kids):
            raise ValueError("Invalid HN comment cursor")
        for item_id in kids[index:index + limit]:
            if "pending" in state:
                post = cache.load_snapshot(state["pending"])
                if post["id"] != str(item_id):
                    raise ValueError("HN pending comment differs from cursor ID")
            else:
                item = _fetch_item(item_id, context)
                if item is None:
                    raise TaskDeferred("HN comment item unavailable; retain cursor", FailureKind.SCHEMA)
                if not item.get("deleted") and not item.get("dead"):
                    if item.get("type") != "comment" or item.get("parent") != thread["id"]:
                        raise ValueError("Expected a top-level comment belonging to this thread")
                post = normalize_post("hackernews", {
                    **item, "text": item.get("text") or "",
                    "url": f"https://news.ycombinator.com/item?id={item_id}",
                })
                timestamp = item.get("time")
                if type(timestamp) in (int, float) and timestamp > 0:
                    post["posted_at"] = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
                cache.observe("hackernews", post)
                state["pending"] = cache.snapshot(post)
            extracted, boards = signal_records(context, cache, "hackernews", post, hiring_thread=True)
            records.extend(extracted)
            leads.extend(boards)
            state.pop("pending", None)
            state["index"] += 1
        return checkpoint_page(task, context, state, records, leads, exhausted=state["index"] == len(kids))
    except (TaskDeferred, ValueError, TypeError, OSError, postgres.Error) as exc:
        return checkpoint_page(task, context, state, records, leads, exc)


def _store_hn_job(conn, job, thread_title=None, posted_at=None, *, context=None):
    """Legacy store requires a real source identity; publication stays common."""
    from .transport import legacy_publish
    if not job.get("posting_id") and not job.get("source_posting_id"):
        raise ValueError("HN storage requires a real comment/role signal ID")
    record = dict(job)
    if posted_at is not None:
        record["posted_at"] = posted_at
    new, _ = legacy_publish([record], "hackernews", context=context, conn=conn)
    return bool(new)


def run_hn_discovery(accept_keywords=None, max_comments=500, delay=0.1, *, context=None, workers=1):
    """Legacy arguments never introduce a private filter, crawl cap or write path."""
    return run_source("hackernews", context=context, workers=workers)

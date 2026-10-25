"""Read one authorized failed request in a new, discovery-owned BrowserOS tab."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from urllib.parse import urlsplit

from .models import Coverage, DiscoveryPage, FailureKind
from .transport import (
    TaskDeferred, deferred_page, recovery_grant, reserve_budget,
)


def _read_script(url):
    page_code = r"""
const scripts = [...document.querySelectorAll('script[type="application/ld+json"]')];
const text = document.body?.innerText || '';
return {
  url: location.href, title: document.title,
  text: text.slice(0, 50000), truncated: text.length > 50000,
  json_ld: scripts.slice(0, 100).map(s => (s.textContent || '').slice(0, 150000)),
  links: [...document.querySelectorAll('a[href]')]
    .filter(a => /\/(jobs?|careers?|positions?|postings?)\//i.test(a.href))
    .slice(0, 200).map(a => ({url:a.href,title:(a.innerText || '').trim()}))
};
"""
    return """
const page = await browser.pages.newPage(URL_VALUE);
try {
  await browser.wait(page, {for: "selector", value: "body"});
  const snapshot = await browser.observe(page).snapshot();
  const value = await browser.evaluate(page, {code: PAGE_CODE, maxChars: 200000});
  return {...(value.value ?? value), page_id: page, snapshot: snapshot.text.slice(0, 12000)};
} finally {
  await browser.pages.close(page).catch(() => undefined);
}
""".replace("PAGE_CODE", json.dumps(page_code)).replace("URL_VALUE", json.dumps(url))


async def _read_page(url, timeout):
    from applypilot.unattended.browser import BrowserOS
    async with BrowserOS() as browser:
        code = _read_script(url)
        payload=await browser.run(code,timeout=timeout)
        if not isinstance(payload,dict) or not isinstance(payload.get("url"),str):
            raise TaskDeferred("BrowserOS did not return a page observation", FailureKind.SCHEMA)
        return payload


def execute_task(task, context, cursor=None, *, executor="smartextract"):
    """Read the exact configured discovery request in its own browser tab."""
    metadata = {'recovery_executor':executor, 'solver_attempted':False, 'recovery_stage':'authorization'}
    try:
        grant = recovery_grant(task, context, cursor)
        if context.runtime["authorized_sources"].get(executor) is not True:
            raise TaskDeferred("Recovery executor requires explicit authorization", FailureKind.ACCESS_DENIED)
        if executor == "browseros_linkedin":
            if not context.runtime["source_inputs"].get(executor, {}).get("allow_automated_browsing"):
                raise TaskDeferred("LinkedIn browsing is disabled in this source configuration",
                                   FailureKind.ACCESS_DENIED)
        reserve_budget(context, "browser")
        timeout = context.runtime["recovery"]["timeout_seconds"]
        if timeout <= 0:
            raise TaskDeferred("Browser recovery timeout budget is zero")
        metadata.update(url=grant['url'],method='GET',recovery_stage='browser_read')
        if task.target.provider == "hiring_cafe":
            from .hiring_cafe import page_from_data, read_browser_data
            data = asyncio.run(asyncio.wait_for(read_browser_data(grant["url"]), timeout=timeout))
            page = page_from_data(task, context, cursor, data)
            return replace(page, request_metadata={**page.request_metadata, **metadata,
                "recovered_by": executor, "recovery_stage": "complete", "recovery_result": "observed"})
        payload = asyncio.run(_read_page(grant["url"], timeout))
        expected, actual = urlsplit(grant["url"]), urlsplit(payload["url"])
        if ((expected.scheme, expected.netloc, expected.path, expected.query) !=
                (actual.scheme, actual.netloc, actual.path, actual.query)):
            raise TaskDeferred("Browser redirected outside the exact authorized request", FailureKind.ACCESS_DENIED)
        from .smartextract import extract_observation
        records = extract_observation(payload, context)
        if not records:
            raise ValueError("Browser snapshot contained no posting observations")
        # One browser snapshot cannot prove complete provider pagination or close
        # unseen jobs. Keep its input cursor and explicitly terminal capped status.
        return DiscoveryPage(task.task_id or "", records=tuple(records), next_cursor=None,
                             exhausted=True, complete=False, partial=True, capped=True,
                             coverage=Coverage.PARTIAL, request_metadata={
                                 "url": grant["url"], "cursor": cursor, "recovered_by": executor,
                                 "page_id": payload["page_id"], "authoritative": False,
                                 "request_key": task.target.request_key(),
                                 **metadata, 'recovery_stage':'complete', 'recovery_result':'observed',
                             })
    except TaskDeferred as exc:
        return deferred_page(task, context, exc, cursor=cursor, **metadata)

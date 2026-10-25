"""Challenge recovery inside the owning browser adapter."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
import os
from urllib.parse import urlsplit

from mcp.server.fastmcp import FastMCP

from applypilot.browser.backends import BrowserOSBackend
from applypilot.browser.cloudflare import FlareSolverrClient

log = logging.getLogger(__name__)


def _page_cookies(cookies: list[dict], url: str) -> list[dict]:
    host = urlsplit(url).hostname or ""
    selected = []
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".")
        if not domain or not (host == domain or host.endswith("." + domain)):
            continue
        selected.append({key: value for key, value in cookie.items()
                         if key in {"name", "value", "domain", "path", "secure", "httpOnly", "sameSite"}})
    return selected


async def _solve(url: str, cookies: list[dict], *, proxy: str | None = None):
    client = FlareSolverrClient(proxy=proxy)
    operation = asyncio.create_task(asyncio.to_thread(client.solve, url, cookies=_page_cookies(cookies, url)))
    try:
        solution = await asyncio.shield(operation)
    except asyncio.CancelledError:
        # The finite HTTP operation retains ownership until its provider session
        # is destroyed. Never apply its eventual result after cancellation.
        try:
            await operation
        except Exception:
            pass
        raise
    host = urlsplit(url).hostname or ""
    scoped = [cookie for cookie in solution.cookies
              if host == cookie.domain.lstrip(".") or host.endswith("." + cookie.domain.lstrip("."))]
    if not scoped or not solution.user_agent:
        raise RuntimeError("FlareSolverr returned no usable page clearance and user agent")
    return replace(solution, cookies=scoped)


async def solve_owned_page(page: int, run, *, proxy: str | None = None, check=None) -> dict:
    """Recover the existing page; the caller validates its submission ownership."""
    backend = BrowserOSBackend()
    before = await run(backend.capture_script(page))
    if not isinstance(before, dict) or not isinstance(before.get("url"), str):
        raise ValueError("The owned browser page returned no URL")
    solution = await _solve(before["url"], before.get("cookies", []), proxy=proxy)
    if check is not None:
        await check()
    applied = await run(backend.apply_clearance_script(page, solution, expected_url=before["url"]))
    if not isinstance(applied, dict) or applied.get("applied") is not True:
        raise RuntimeError("Browser did not confirm page clearance was applied")
    observed = await run(backend.observation_script(page))
    if not isinstance(observed, dict) or not isinstance(observed.get("url"), str):
        raise ValueError("The recovered page returned no observation")
    log.info("browser_challenge_recovery page=%s solver_session=%s route=%s", page,
             solution.solver_session_id, "proxy" if proxy else "direct")
    return {"page": page, "clearance_applied": True, "url": observed["url"],
            "title": observed.get("title", ""), "text": observed.get("text", "")}


def build_server(*, cdp_port: int, attempt_id: str) -> FastMCP:
    """Playwright's worker-specific CDP endpoint owns the actual browser context."""
    server = FastMCP("ApplyPilot Browser Challenge Recovery")

    @server.tool()
    async def solve_browser_challenge(page_id: int) -> dict:
        """Recover the current challenge on a tab index from browser_tabs, then read it."""
        from playwright.async_api import async_playwright
        from applypilot.apply.guard import attempt_status, GuardRejected

        async def check():
            attempt = await asyncio.to_thread(attempt_status, attempt_id)
            if attempt["status"] != "prepared":
                raise GuardRejected("Reconcile the existing submission before navigating its page")

        await check()
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
            try:
                if len(browser.contexts) != 1 or type(page_id) is not int or page_id < 0:
                    raise ValueError("Use a tab index from the owning application browser")
                context = browser.contexts[0]
                page = context.pages[page_id]
                url = page.url
                solution = await _solve(url, await context.cookies([url]),
                                        proxy=os.environ.get("APPLYPILOT_BROWSER_PROXY_URL"))
                await check()
                if page.url != url:
                    raise RuntimeError("Owned page navigated during challenge recovery")
                session = await context.new_cdp_session(page)
                try:
                    await session.send("Network.setCookies", {"cookies": [cookie.to_browser_os_dict() for cookie in solution.cookies]})
                    await session.send("Emulation.setUserAgentOverride", {"userAgent": solution.user_agent})
                    await page.reload(wait_until="domcontentloaded")
                    observed = await page.evaluate("({url:location.href,title:document.title,text:(document.body?.innerText||'').slice(0,12000)})")
                finally:
                    await session.detach()
                log.info("browser_challenge_recovery page=%s solver_session=%s", page_id, solution.solver_session_id)
                return {"page": page_id, "clearance_applied": True, **observed}
            finally:
                # connect_over_cdp disconnects this client; it does not own Chrome.
                await browser.close()

    return server


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--attempt-id", required=True)
    args = parser.parse_args()
    build_server(cdp_port=args.cdp_port, attempt_id=args.attempt_id).run(transport="stdio")


if __name__ == "__main__":
    main()

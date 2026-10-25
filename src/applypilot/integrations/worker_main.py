"""Standalone worker executed with an optional tool's Python, not ApplyPilot's.

Only stdlib imports occur before dispatch. The protocol has a single JSON result
line, even when an upstream SDK writes progress messages to stdout. Credentials
are read from explicitly supplied local paths; errors never echo their values.
"""
from __future__ import annotations

# Executing this file by path puts ``integrations/`` first on sys.path.  Its
# ``types.py`` would then shadow Python's stdlib ``types`` module before the
# optional worker imports anything.  Keep the standalone worker import-clean
# without adding ApplyPilot or the repository to the isolated environment.
import os as _os
import sys as _sys

_WORKER_DIR = _os.path.normcase(_os.path.abspath(_os.path.dirname(__file__)))
_sys.path[:] = [entry for entry in _sys.path
                if _os.path.normcase(_os.path.abspath(entry or _os.curdir)) != _WORKER_DIR]

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime
import ipaddress
import json
import math
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit


class WorkerFailure(Exception):
    def __init__(self, message, kind="configuration", retry_after=None):
        super().__init__(message)
        self.kind, self.retry_after = kind, retry_after


def failure_result(exc):
    """Classify bounded SDK evidence without returning upstream messages or bodies."""
    chain, seen = [], set()
    current = exc
    while isinstance(current, BaseException) and id(current) not in seen and len(chain) < 8:
        seen.add(id(current))
        chain.append(current)
        wrapped = current.args[0] if current.args and isinstance(current.args[0], BaseException) else None
        current = current.__cause__ or current.__context__ or wrapped
    names = [re.sub(r"[^A-Za-z0-9_]", "", type(error).__name__)[:100] for error in chain]
    status, retry_after = None, None
    for error in chain:
        response = getattr(error, "response", None)
        for value in (getattr(error, "status_code", None), getattr(error, "status", None),
                      getattr(response, "status_code", None)):
            if status is None and isinstance(value, (int, str)) and re.fullmatch(r"[45][0-9]{2}", str(value)):
                status = int(value)
        headers = getattr(response, "headers", None) or getattr(error, "headers", None)
        value = getattr(error, "retry_after", None)
        if value is None and hasattr(headers, "get"):
            value = headers.get("retry-after") or headers.get("Retry-After")
        if retry_after is None and isinstance(value, (int, float, str)):
            try:
                delay = float(value)
            except (ValueError, OverflowError):
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, OverflowError):
                    continue
            if math.isfinite(delay):
                retry_after = min(86400, max(1, delay))
    classes = " ".join(names).casefold()
    kind = (exc.kind if isinstance(exc, WorkerFailure) else
            "rate_limit" if status == 429 or any(word in classes for word in ("ratelimit", "toomanyrequests")) else
            "auth" if status == 401 or any(word in classes for word in ("unauthorized", "accountlocked", "noaccount", "login")) else
            "access_denied" if status == 403 or "forbidden" in classes else
            "timeout" if status in {408, 504} or "timeout" in classes or any(isinstance(error, TimeoutError) for error in chain) else
            "configuration" if any(isinstance(error, ImportError) for error in chain) else
            "schema" if status in {400, 404, 410, 413, 422} or any(isinstance(error, (ValueError, TypeError, KeyError, IndexError)) for error in chain) else
            "provider" if status is not None and status >= 500 else
            "network" if any(isinstance(error, (ConnectionError, socket.gaierror, socket.herror)) for error in chain)
                         or any(word in classes for word in ('connecterror', 'networkerror', 'proxyerror', 'remoteprotocolerror')) else
            "unknown")
    messages = {"rate_limit": "Optional SDK was rate limited", "auth": "Optional SDK requires authentication",
                "access_denied": "Optional SDK access was denied", "timeout": "Optional SDK request timed out",
                "configuration": "Optional SDK dependency could not be imported", "schema": "Optional SDK request or response was invalid",
                "network": "Reader could not reach the service", "provider": "Service returned a server error",
                "unknown": "Reader failed without reporting a specific cause"}
    message = (str(exc)[:1000] if isinstance(exc, WorkerFailure) else
               f"{messages[kind]} ({names[0]}" + (f"; HTTP {status}" if status else "") + ")")
    details = {"exception_type": names[0], "reason": " <- ".join(names), "error_kind": kind,
               "http_status": status, "retry_after": retry_after}
    tb = exc.__traceback__
    while tb is not None:
        frame = tb.tb_frame
        module = re.sub(r'[^A-Za-z0-9_.]', '', str(frame.f_globals.get('__name__', '')))[:160]
        function = re.sub(r'[^A-Za-z0-9_]', '', frame.f_code.co_name)[:100]
        details['reason'] = ' <- '.join(names) + ' at ' + module + '.' + function
        tb = tb.tb_next
    if kind in {'unknown', 'schema'} and not isinstance(exc, WorkerFailure):
        message += ': ' + details['reason']
    context = getattr(exc, 'reader_context', {})
    for key in ('phase', 'route'):
        if isinstance(context.get(key), str):
            details[key] = context[key][:200]
    return {"ok": False, "error": message, "kind": kind, "retry_after": retry_after, "details": details}


def serial(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    raise TypeError("Unsupported upstream value")


def existing_file(value):
    if not value or not Path(value).expanduser().is_file():
        raise WorkerFailure("An existing, operator-supplied session file is required", "auth")
    return str(Path(value).expanduser())


def public_target(url):
    p = urlsplit(url)
    if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
        return False
    try:
        addresses = socket.getaddrinfo(p.hostname, p.port or 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
    except (ValueError, OSError):
        return False


def tweet_row(tweet):
    data = serial(tweet) if hasattr(tweet, "dict") or hasattr(tweet, "model_dump") or is_dataclass(tweet) else {}
    legacy = getattr(tweet, "_legacy", {})
    identity = str(getattr(tweet, "id", data.get("id", "")))
    text = getattr(tweet, "full_text", None) or getattr(tweet, "text", None) or getattr(tweet, "rawContent", None) or data.get("rawContent") or data.get("text")
    if not identity.isdigit() or not isinstance(text, str):
        raise WorkerFailure("Upstream tweet schema changed", "schema")
    entities = data.get("entities") or legacy.get("entities", {})
    links = data.get("links", []) or []
    if not entities and links:
        entities = {"urls": [{"expanded_url": row.get("url") or row.get("tcourl")} for row in links if isinstance(row, dict)]}
    user = getattr(tweet, "user", None) or data.get("user") or {}
    def field(name):
        return user.get(name) if isinstance(user, dict) else getattr(user, name, None)
    author = {"name": field("name") or field("displayname") or field("display_name"),
              "username": field("username") or field("screen_name")}
    return {"id": identity, "url": f"https://x.com/i/web/status/{identity}", "text": text,
            "created_at": str(getattr(tweet, "created_at", None) or data.get("date") or "") or None,
            "entities": entities, "author": author}


async def twikit_search_page(client, arguments):
    """Decode one search response without Twikit's silent per-entry KeyError loss."""
    from twikit.client.client import tweet_from_data
    from twikit.utils import find_dict

    response, _ = await client.gql.search_timeline(
        arguments["query"], "Latest", min(arguments["limit"], 20), arguments.get("cursor"))
    instruction_lists = find_dict(response, "instructions")
    if not instruction_lists:
        raise WorkerFailure("X search response omitted timeline instructions", "schema")
    entries, next_cursor, terminated = {}, None, False
    instruction_count = 0
    for instructions in instruction_lists:
        if not isinstance(instructions, list):
            continue
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            instruction_count += 1
            if instruction.get("type") == "TimelineTerminateTimeline" and instruction.get("direction") == "Bottom":
                terminated = True
            candidates = instruction.get("entries", [])
            if isinstance(instruction.get("entry"), dict):
                candidates = [*candidates, instruction["entry"]] if isinstance(candidates, list) else [instruction["entry"]]
            if not isinstance(candidates, list):
                continue
            for entry in candidates:
                if isinstance(entry, dict) and isinstance(entry.get("entryId"), str):
                    entries[entry["entryId"]] = entry
    rows, raw_tweets, malformed, unavailable, recovered = [], 0, 0, 0, 0
    failure_types, missing_fields = {}, {}
    for identity, entry in entries.items():
        content = entry.get("content", {})
        if isinstance(content, dict) and (content.get("cursorType") == "Bottom" or identity.startswith("cursor-bottom")):
            next_cursor = content.get("value")
        if not identity.startswith(("tweet", "search-grid")):
            continue
        raw_tweets += 1
        try:
            results = find_dict(entry, "result", find_one=True)
            result = results[0] if results and isinstance(results[0], dict) else {}
            if result.get("__typename") in {"TweetTombstone", "TweetUnavailable"}:
                unavailable += 1
                continue
            tweet = tweet_from_data(client, entry)
            if tweet is None:
                malformed += 1
                continue
            rows.append(tweet_row(tweet))
        except (KeyError, TypeError, ValueError, AttributeError, WorkerFailure) as exc:
            kind = type(exc).__name__
            failure_types[kind] = failure_types.get(kind, 0) + 1
            # Twikit constructs a complete User even for search-post reads. X
            # moved several profile fields out of legacy; discovery needs only
            # the author identity attached to this tweet, not that full model.
            if isinstance(exc, KeyError) and exc.args and exc.args[0] in {
                    "name", "screen_name", "created_at", "profile_image_url_https", "location", "description",
                    "entities", "pinned_tweet_ids_str", "is_blue_verified", "verified", "possibly_sensitive",
                    "can_dm", "can_media_tag", "want_retweets", "default_profile", "default_profile_image",
                    "has_custom_timelines", "followers_count", "fast_followers_count", "normal_followers_count",
                    "friends_count", "favourites_count", "listed_count", "media_count", "statuses_count",
                    "is_translator", "translator_type", "withheld_in_countries"}:
                missing_fields[exc.args[0]] = missing_fields.get(exc.args[0], 0) + 1
            try:
                rows.append(twikit_raw_tweet_row(entry))
                recovered += 1
            except (KeyError, TypeError, ValueError, AttributeError, WorkerFailure):
                malformed += 1
    errors = []
    if malformed:
        errors.append({"provider": "twikit", "kind": "schema",
                       "error": f"X returned {raw_tweets} tweet entries but {malformed} could not be parsed; valid posts were retained"})
    return {"items": rows, "next_cursor": None if terminated else next_cursor,
            "diagnostics": {"instruction_count": instruction_count, "raw_entry_count": len(entries),
                            "raw_tweet_entries": raw_tweets, "parsed_tweet_entries": len(rows),
                            "unparsed_tweet_entries": malformed, "parser_failure_types": failure_types,
                            "sdk_missing_fields": missing_fields, "sdk_parser_recovered": recovered,
                            "unavailable_tweet_entries": unavailable,
                            "terminal_instruction": terminated},
            "provider_errors": errors}


def twikit_raw_tweet_row(entry):
    """Read only fields belonging to this tweet and its explicitly attached user."""
    from twikit.utils import find_dict
    results = find_dict(entry, "tweet_results", find_one=True)
    if not results or not isinstance(results[0], dict):
        raise WorkerFailure("X timeline entry omitted its tweet result", "schema")
    data = results[0]["result"]
    data = data.get("tweet", data)
    legacy = data["legacy"]
    identity = data.get("rest_id") or legacy.get("id_str")
    notes = data.get("note_tweet", {}).get("note_tweet_results", {}).get("result", {})
    text = notes.get("text") or legacy.get("full_text")
    if not isinstance(identity, str) or not identity.isdigit() or not isinstance(text, str):
        raise WorkerFailure("X post omitted its own numeric identity or text", "schema")
    user = data.get("core", {}).get("user_results", {}).get("result", {})
    profile = user.get("core", {})
    old_profile = user.get("legacy", {})
    author = {"name": profile.get("name") or old_profile.get("name"),
              "username": profile.get("screen_name") or old_profile.get("screen_name")}
    entities = legacy.get("entities", {})
    note_entities = notes.get("entity_set", {})
    if isinstance(note_entities, dict) and note_entities.get("urls"):
        entities = {**entities, "urls": [*entities.get("urls", []), *note_entities["urls"]]}
    return {"id": identity, "url": "https://x.com/i/web/status/" + identity, "text": text,
            "created_at": legacy.get("created_at"), "entities": entities, "author": author}


async def crawl(a):
    """Stateless Playwright rendering with public navigation and redirects."""
    from playwright.async_api import async_playwright
    url = a["url"]
    if not await asyncio.to_thread(public_target, url):
        raise WorkerFailure("Browser target needs a public URL", "access_denied")
    async with async_playwright() as toolkit:
        browser = await toolkit.chromium.launch(headless=True)
        try:
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            page = await context.new_page()
            async def guard(route):
                request = route.request
                if await asyncio.to_thread(public_target, request.url):
                    await route.continue_()
                else:
                    await route.abort()
            await context.route("**/*", guard)
            page.on("download", lambda download: asyncio.create_task(download.cancel()))
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                raise WorkerFailure("Browser could not read the page", "network") from None
            if not response:
                raise WorkerFailure("Browser returned no main-page response", "network")
            status = response.status
            if status in (401, 403, 429):
                raise WorkerFailure(f"Upstream HTTP {status}", {401:"auth",403:"access_denied",429:"rate_limit"}[status],900)
            if status >= 400:
                raise WorkerFailure("Upstream page returned an error", "network")
            html = await page.content()
            if len(html.encode("utf-8")) > 4_000_000:
                raise WorkerFailure("Rendered page exceeds the 4 MB document budget", "schema")
            # Markdown, JSON-LD and links are parsed on the host by html_document.
            return {"url": page.url, "html": html, "links": [], "title": await page.title()}
        finally:
            await browser.close()


def tweet_page(page, limit):
    """Normalize one observed provider page; cursor traversal belongs to Temporal."""
    rows, seen = [], set()
    for tweet in page:
        row = tweet_row(tweet)
        if row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
            if len(rows) >= limit:
                break
    return rows


async def twikit(operation, a):
    from twikit import Client
    client = Client("en-US")
    _twikit_transaction_compat(client)
    client.load_cookies(existing_file(a.get("session_file")))
    last_response = None

    async def capture_response(response):
        nonlocal last_response
        last_response = response

    client.http.event_hooks['response'].append(capture_response)
    try:
        if operation == "verify":
            # Client.user() depends on account/settings and two profile parsers.
            # This endpoint directly verifies the cookie's authenticated owner.
            data, _ = await client.get('https://x.com/i/api/1.1/account/verify_credentials.json',
                headers=client._base_headers, params={'skip_status': 'true', 'include_entities': 'false'})
            if not isinstance(data, dict) or not data.get('id_str') or not data.get('screen_name'):
                raise WorkerFailure('X did not return the signed-in identity from its credentials endpoint', 'schema')
            return {"id": data['id_str'], "screen_name": data['screen_name']}
        if operation == "posts":
            if a.get("paged"):
                return await twikit_search_page(client, a)
            tweets = await client.search_tweet(a["query"], "Latest", count=min(a["limit"], 20), cursor=a.get("cursor"))
            return tweet_page(tweets, a["limit"])
        if operation == "profile":
            user = await client.get_user_by_screen_name(a["identifier"])
            return {key: getattr(user, key, None) for key in
                    ("id", "name", "screen_name", "description", "url", "location", "followers_count", "following_count")}
        if operation == "timeline":
            user = await client.get_user_by_screen_name(a["identifier"])
            tweets = await client.get_user_tweets(user.id, "Tweets", count=min(a["limit"], 20))
            return tweet_page(tweets, a["limit"])
        raise WorkerFailure("Unsupported Twikit read operation")
    except Exception as exc:
        exc.reader_context = {'phase': 'reader_request'}
        if last_response is not None:
            exc.status_code = last_response.status_code
            # Do not retain query strings, response bodies, or account identifiers.
            path = last_response.url.path
            exc.reader_context['phase'] = 'api_request' if '/i/api/' in path else 'transaction_bootstrap'
            if path == '/i/api/1.1/account/verify_credentials.json':
                exc.reader_context['route'] = path
        raise
    finally:
        await client.http.aclose()




def _twikit_transaction_compat(client):
    """Bridge Twikit 2.3.3's webpack lookup and transaction timing bugs.

    See d60/twikit#408 and iSarabjitDhiman/XClientTransaction's frame-time rounding.
    """
    from importlib.metadata import version
    if version('twikit') != '2.3.3':
        return
    from twikit.x_client_transaction.transaction import ClientTransaction

    class Transaction(ClientTransaction):
        def get_animation_key(self, key_bytes, response):
            frames = self.get_2d_array(key_bytes, response)
            frame_time = math.prod(key_bytes[index] % 16 for index in self.DEFAULT_KEY_BYTES_INDICES)
            # Match JavaScript Math.round, including ties; Python round differs.
            frame_time = math.floor(frame_time / 10 + 0.5) * 10
            return self.animate(frames[key_bytes[self.DEFAULT_ROW_INDEX] % 16], frame_time / 4096)

        async def get_indices(self, home_page_response, session, headers):
            source = str(self.validate_response(home_page_response))
            # X now maps a numeric chunk ID to its name and hash separately.
            chunk = re.search(r'''(?:^|[,{])\s*["']?(\d+)["']?\s*:\s*["']ondemand\.s["']''', source)
            if chunk:
                match = re.search(r'''(?:^|[,{])\s*["']?''' + re.escape(chunk[1]) + r'''["']?\s*:\s*["']([a-fA-F0-9]+)["']''', source)
            else:
                match = re.search(r'''["']ondemand\.s["']\s*:\s*["']([a-fA-F0-9]+)["']''', source)
            if not match:
                raise WorkerFailure('Twikit could not locate X\'s transaction script; the reader needs a compatibility update, not a new login', 'schema')
            response = await session.request('GET',
                'https://abs.twimg.com/responsive-web/client-web/ondemand.s.' + match[1] + 'a.js', headers=headers)
            response.raise_for_status()
            indices = [int(value) for value in re.findall(r'''\(\s*[\w$]+\[(\d{1,3})\]\s*,\s*16\s*\)''', response.text)]
            if len(indices) < 2:
                raise WorkerFailure('Twikit could not parse X\'s transaction script (KEY_BYTE indices); the reader needs a compatibility update', 'schema')
            return indices[0], indices[1:]

    client.client_transaction = Transaction()


def _linkedin_login_required(url):
    return urlsplit(url).path.startswith(('/login', '/uas/login', '/checkpoint', '/authwall', '/challenge'))


async def _verify_linkedin_page(page):
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    # Also watch for client-side sign-in redirects after the feed shell loads.
    try:
        await page.wait_for_function(r'''() => {
            if (/^\/(login|uas\/login|checkpoint|authwall|challenge)(\/|$)/.test(location.pathname)) return true;
            return [...document.querySelectorAll('nav.global-nav, #global-nav, header nav, [role="navigation"]')]
                .some(nav => nav.getClientRects().length && nav.querySelector('a[href*="/feed"], a[href*="/mynetwork"], a[href*="/messaging"]'));
        }''', timeout=15000)
    except PlaywrightTimeout:
        if _linkedin_login_required(page.url) or await page.locator('input[type="password"]').count():
            raise WorkerFailure('LinkedIn rejected the saved browser session and opened sign-in; reconnect LinkedIn, then check access', 'auth') from None
        raise WorkerFailure('LinkedIn loaded without recognizable signed-in navigation; the page may be incomplete or the reader may need an update. An expired login was not confirmed', 'schema') from None
    if _linkedin_login_required(page.url):
        raise WorkerFailure('LinkedIn redirected to sign-in or an account challenge; reconnect LinkedIn, then check access', 'auth')
    if not urlsplit(page.url).path.startswith('/feed'):
        raise WorkerFailure('LinkedIn redirected away from its feed; signed-in access was not confirmed', 'unknown')


async def linkedin(operation, a):
    from linkedin_scraper import BrowserManager, PersonScraper, CompanyScraper, JobSearchScraper, CompanyPostsScraper
    async with BrowserManager(headless=True) as browser:
        await browser.load_session(existing_file(a.get("session_file")))
        if operation == "verify":
            response = await browser.page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=35000)
            if response is None:
                raise WorkerFailure("LinkedIn returned no response while opening its feed", "unknown")
            if response.status >= 400:
                status = response.status
                kind = {401: 'auth', 403: 'access_denied', 429: 'rate_limit', 408: 'timeout', 504: 'timeout'}.get(
                    status, 'provider' if status >= 500 else 'schema')
                failure = WorkerFailure(f"LinkedIn feed returned HTTP {status}", kind)
                failure.status_code = status
                raise failure
            await _verify_linkedin_page(browser.page)
            return {"authenticated": True}
        if operation == "profile":
            return serial(await PersonScraper(browser.page).scrape(a["identifier"]))
        if operation == "company":
            return serial(await CompanyScraper(browser.page).scrape(a["identifier"]))
        if operation == "jobs":
            data = await JobSearchScraper(browser.page).search(keywords=a["query"], location=a.get("location", ""), limit=a["limit"])
        elif operation == "company_posts":
            data = await CompanyPostsScraper(browser.page).scrape(a["identifier"], limit=a["limit"])
        else:
            raise WorkerFailure("linkedin_scraper supports profile, company, jobs, company_posts")
        return [serial(row) for row in data][:a["limit"]]


def dispatch(request):
    p, operation, a = request["provider"], request["operation"], request["arguments"]
    if operation == "doctor":
        if p not in {"ddgs", "playwright", "twikit", "linkedin_scraper"}:
            raise ValueError("Unknown local reader")
        import importlib, importlib.metadata
        importlib.import_module(p)
        return {"provider": p, "imported": True, "version": importlib.metadata.version(p.replace("_", "-"))}
    limit = a.get("limit", 10)
    if type(limit) is not int or not 1 <= limit <= (50 if p == "ddgs" else 500):
        raise WorkerFailure("Invalid per-call result limit")
    if a.get("cursor") is not None and (not isinstance(a["cursor"], str) or len(a["cursor"]) > 65536):
        raise WorkerFailure("Invalid page cursor", "schema")
    if p == "ddgs" and operation == "search":
        from ddgs import DDGS
        # DDGS selects available engines in auto mode; an optional comma-separated
        # engine list travels in the request, including through isolated workers.
        backend = a.get("backend", "auto")
        if not isinstance(backend, str) or not backend.strip() or len(backend) > 256:
            raise WorkerFailure("Invalid DDGS backend selection", "schema")
        search = DDGS(timeout=15)
        method = search.news if a.get("category") == "news" else search.text
        return method(a["query"], max_results=limit, page=a.get("page", 1), backend=backend)
    if p == "playwright" and operation == "crawl":
        return asyncio.run(crawl(a))
    if p == "twikit":
        return asyncio.run(twikit(operation, a))
    if p == "linkedin_scraper":
        return asyncio.run(linkedin(operation, a))
    raise WorkerFailure("Unknown worker operation")


def main():
    try:
        request = globals().get("_INITIAL_REQUEST")
        if request is None:
            raw = _sys.stdin.buffer.read(4_000_001)
            if len(raw) > 4_000_000:
                raise WorkerFailure("Worker input exceeds 4 MB", "schema")
            request = json.loads(raw)
        result = {"ok": True, "data": dispatch(request)}
        output = json.dumps(result, default=serial, ensure_ascii=True, allow_nan=False)
        if len(output) > 7_000_000:
            raise WorkerFailure("Worker result exceeds 7 MB", "schema")
    except Exception as exc:
        output = json.dumps(failure_result(exc), ensure_ascii=True, allow_nan=False)
    print("APPLYPILOT_RESULT:" + output)


if __name__ == "__main__":
    main()

"""JSON-only bridge to the pinned Jobseek Python 3.13 runtime.

Executed with an isolated interpreter. No ApplyPilot imports or credentials,
upstream database, queue, search-index, or object-store clients are needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import sys
import tempfile
from dataclasses import asdict
from urllib.parse import urlsplit

COMMIT = 'e65fb972e4fbb349d10ed66dc9418b1d271f52ca'


class BoundaryError(RuntimeError):
    def __init__(self, message, kind='configuration'):
        super().__init__(message)
        self.kind = kind


def public_url(url):
    import ipaddress
    parsed = urlsplit(str(url))
    host = parsed.hostname or ''
    if (parsed.scheme not in {'http', 'https'} or not host or parsed.username or parsed.password
            or parsed.port not in {None, 80, 443} or '\\' in str(url)
            or any(char.isspace() for char in str(url))):
        raise BoundaryError('Jobseek requested an invalid public HTTP(S) URL', 'access_denied')
    if host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise BoundaryError('Jobseek requested a private host', 'access_denied')
    addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80),
                                   type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise BoundaryError('Jobseek requested a non-public address', 'access_denied')
    return str(url)


def validate_config(value):
    """Remote CSV settings select extractors; they cannot supply executable code."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {'browser_expression', 'script', 'javascript', 'python', 'eval', 'exec',
                       'executable_path', 'executable', 'user_data_dir'} and item:
                raise BoundaryError('Jobseek configuration requires prohibited executable setting: ' + key,
                                    'unsupported')
            if key == 'action' and item in {'evaluate', 'execute', 'javascript'}:
                raise BoundaryError('Jobseek configuration contains an executable browser action', 'unsupported')
            if key in {'skip_ssl', 'ignore_https_errors'} and item:
                raise BoundaryError('Jobseek configuration requests disabled TLS verification', 'unsupported')
            validate_config(item)
    elif isinstance(value, list):
        for item in value:
            validate_config(item)


def install_boundary():
    """Count actual requests and enforce public egress inside upstream transports."""
    import httpx
    from src.shared import ssrf
    ssrf.is_internal_host_allowed = lambda *args, **kwargs: False
    spent = {'http': 0, 'failure': None}

    def reserve(url):
        public_url(url)
        spent['http'] += 1

    original = httpx.AsyncClient._send_single_request

    async def guarded(client, request):
        reserve(str(request.url))
        response = await original(client, request)
        # Upstream may swallow a provider error while probing alternatives; the
        # bridge must never report a denied/rate-limited board as an empty one.
        if response.status_code in {401, 403, 429}:
            spent['failure'] = BoundaryError('Jobseek provider returned HTTP ' + str(response.status_code),
                                             'rate_limit' if response.status_code == 429 else 'access_denied')
        upstream_stream = response.stream

        class BoundedStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                size = 0
                async for part in upstream_stream:
                    size += len(part)
                    if size > 20_000_000:
                        raise BoundaryError('Jobseek response exceeded 20 MB', 'schema')
                    yield part

            async def aclose(self):
                await upstream_stream.aclose()

        response.stream = BoundedStream()
        return response

    httpx.AsyncClient._send_single_request = guarded
    return spent, reserve


async def execute(payload):
    from src.core.monitor import monitor_one
    from src.core.monitors import all_monitor_types, complete_inventory_monitor_types
    from src.core.scrape import scrape_one
    from src.core.scrapers import all_scraper_types
    from src.shared.browser import BrowserBackend, ChromiumBrowserBackend
    from src.shared.http import create_http_client
    from src.workspace._compat import auto_scraper_type

    board = payload['board']
    monitor, monitor_config = board['monitor_type'], board['monitor_config']
    scraper, scraper_config = board['scraper_type'], board['scraper_config']
    if monitor not in all_monitor_types():
        raise BoundaryError('Monitor is unavailable at the pinned Jobseek revision: ' + monitor, 'unsupported')
    if not scraper:
        selected = auto_scraper_type(monitor, monitor_config)
        if selected:
            scraper, defaults = selected
            scraper_config = {**(defaults or {}), **scraper_config}
        else:
            scraper = 'json-ld'
    if scraper != 'skip' and scraper not in all_scraper_types():
        raise BoundaryError('Scraper is unavailable at the pinned Jobseek revision: ' + scraper, 'unsupported')
    validate_config(monitor_config)
    validate_config(scraper_config)
    public_url(board['board_url'])
    spent, reserve = install_boundary()

    class GuardedBrowser(BrowserBackend):
        implementation = 'applypilot-jobseek'

        def __init__(self):
            self.backend = ChromiumBrowserBackend()

        async def start(self):
            await self.backend.start()
            return self

        @contextlib.asynccontextmanager
        async def open_page(self, config=None, *, use_proxy=False, target_url=None):
            if target_url:
                public_url(target_url)
            validate_config(config or {})
            async with self.backend.open_page(config, use_proxy=use_proxy, target_url=target_url) as page:
                async def route(request_route):
                    request = request_route.request
                    try:
                        if request.url.startswith(('data:', 'blob:')):
                            await request_route.continue_()
                            return
                        public_url(request.url)
                        if request.resource_type in {'document', 'xhr', 'fetch'}:
                            reserve(request.url)
                        if request.resource_type in {'image', 'media', 'font'}:
                            await request_route.abort()
                        else:
                            await request_route.continue_()
                    except (BoundaryError, OSError) as exc:
                        spent['failure'] = exc
                        await request_route.abort()
                await page.context.route('**/*', route)
                await page.context.route_web_socket('**/*', lambda connection: connection.close())
                yield page

        async def stop(self):
            await self.backend.stop()

    browser = GuardedBrowser()
    # Some provider paths request rendering only after inspecting static HTML.
    # Starting Playwright's driver is cheap; Chromium launches only when needed.
    await browser.start()
    try:
        async with create_http_client() as http:
            operation = payload.get('operation', 'inventory')
            if operation == 'inventory':
                result = await monitor_one(board['board_url'], monitor, monitor_config, http, pw=browser)
                if spent['failure']:
                    raise spent['failure']
                urls = sorted(public_url(url) for url in result.urls)
                return {'commit': COMMIT, 'operation': operation, 'urls': urls,
                    'rich': {url: asdict(item) for url, item in (result.jobs_by_url or {}).items()},
                    'authoritative': not result.truncated and (monitor in complete_inventory_monitor_types()
                                                               or bool(result.verified_empty_reason)),
                    'partial': bool(result.truncated), 'requests_used': spent['http']}
            if operation != 'posting':
                raise BoundaryError('Unknown finite Jobseek operation')
            url = public_url(payload['url'])
            item = dict(payload.get('item') or {'url': url})
            if scraper != 'skip' or not item.get('title') or not item.get('description'):
                selected_scraper = scraper if scraper != 'skip' else 'json-ld'
                if urlsplit(url).path.lower().endswith('.pdf') and selected_scraper == 'json-ld':
                    selected_scraper = 'pdf'
                content = asdict(await scrape_one(url, selected_scraper, scraper_config, http, pw=browser))
                enrich = scraper_config.get('enrich')
                for key, value in content.items():
                    if value is not None and (not item.get(key) or enrich and key in enrich):
                        item[key] = value
            if spent['failure']:
                raise spent['failure']
            if not item.get('title'):
                raise BoundaryError('Posting did not expose a title', 'schema')
            if item.get('description') and len(item['description']) > 100_000:
                item['description'] = item['description'][:100_000]
                item['metadata'] = {**(item.get('metadata') or {}), 'description_truncated': True}
            return {'commit': COMMIT, 'operation': operation, 'records': [item],
                    'requests_used': spent['http']}

    finally:
        await browser.stop()


def main():
    try:
        payload = json.loads(sys.stdin.buffer.read(1_000_001))
        if payload.get('commit') != COMMIT:
            raise BoundaryError('Jobseek worker revision mismatch')
        # Upstream Settings reads .env in the current directory. Use an empty
        # owned directory so application credentials cannot cross this boundary.
        with tempfile.TemporaryDirectory(prefix='applypilot-jobseek-') as directory:
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                data = asyncio.run(execute(payload))
            finally:
                os.chdir(previous_directory)
        result = {'ok': True, 'data': data}
    except Exception as exc:  # noqa: BLE001 -- subprocess protocol must always emit its result envelope
        result = {'ok': False, 'kind': getattr(exc, 'kind', 'provider'),
                  'error': f'{type(exc).__name__}: {exc}'[:1500]}
    print('APPLYPILOT_RESULT:' + json.dumps(result, ensure_ascii=True, default=str))


if __name__ == '__main__':
    main()

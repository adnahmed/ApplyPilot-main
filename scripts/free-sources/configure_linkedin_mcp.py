"""Provision the installed LinkedIn MCP server from an authorized saved session."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit


async def configure(root, binary, session):
    from linkedin_mcp_server.core.browser import BrowserManager
    from linkedin_mcp_server.profile_claim import ensure_profile_claim
    from linkedin_mcp_server.session_state import write_source_state

    if not binary.is_file() or not session.is_file():
        raise ValueError('The CloakBrowser executable and saved session must exist')
    source = json.loads(session.read_text(encoding='utf-8-sig'))
    cookies = [item for item in source.get('cookies', [])
               if isinstance(item, dict) and
               (item.get('domain', '').lstrip('.') == 'linkedin.com'
                or item.get('domain', '').lstrip('.').endswith('.linkedin.com'))]
    if not any(item.get('name') == 'li_at' and item.get('value') for item in cookies):
        raise ValueError('The saved session has no LinkedIn authentication cookie')
    profile = root / 'sessions' / 'linkedin-mcp' / 'profile'
    if any(path.is_symlink() for path in (root, root / 'sessions', profile.parent, profile)):
        raise ValueError('LinkedIn MCP profile paths must not be symlinks')
    if profile.exists() and any(profile.iterdir()):
        raise ValueError('A dedicated MCP profile already exists; it was preserved')
    ensure_profile_claim(profile)
    async with BrowserManager(user_data_dir=profile, headless=True,
                              executable_path=str(binary)) as browser:
        await browser.context.add_cookies(cookies)
        response = await browser.page.goto('https://www.linkedin.com/feed/',
                                           wait_until='domcontentloaded', timeout=45000)
        if response is None or response.status >= 400:
            raise RuntimeError('LinkedIn did not accept the saved session')
        await browser.page.wait_for_selector(
            'nav a[href*="/feed"], nav a[href*="/mynetwork"], nav a[href*="/messaging"]',
            timeout=20000)
        if not urlsplit(browser.page.url).path.startswith('/feed'):
            raise RuntimeError('LinkedIn redirected to sign-in or a challenge; configuration was not changed')
        if not await browser.export_cookies():
            raise RuntimeError('Could not save the authenticated MCP session')
        write_source_state(profile)
    if not browser.close_confirmed:
        raise RuntimeError('Browser cleanup was not confirmed; configuration was not changed')
    path = root / 'free_sources_mcp.json'
    existing = path.read_bytes()
    data = json.loads(existing.decode('utf-8-sig'))
    cfg = data['linkedin_mcp']
    old = cfg.get('args', [])
    args = []
    index = 0
    while index < len(old):
        item = old[index]
        if item in {'--chrome-path', '--user-data-dir', '--transport'}:
            index += 2
            continue
        if not item.startswith(('--chrome-path=', '--user-data-dir=', '--transport=')):
            args.append(item)
        index += 1
    args.extend(['--chrome-path', str(binary), '--user-data-dir', str(profile), '--transport', 'stdio'])
    if '--no-auto-import' not in args:
        args.append('--no-auto-import')
    cfg['args'] = args
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = path.with_name(path.name + '.before-cloak-' + stamp)
    shutil.copy2(path, backup)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2)
            output.write('\n')
        if path.read_bytes() != existing:
            raise RuntimeError('MCP configuration changed during setup; retry after reviewing it')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print('LinkedIn MCP configured with CloakBrowser; saved session verified on LinkedIn.')
    print('Private MCP profile:', profile)
    print('Configuration backup:', backup)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-dir', type=Path, default=Path.home() / '.applypilot')
    parser.add_argument('--binary', type=Path, required=True)
    parser.add_argument('--session', type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(configure(args.app_dir.resolve(), args.binary.resolve(), args.session.resolve()))

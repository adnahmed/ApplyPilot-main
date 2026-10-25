#!/usr/bin/env python3
"""Create an operator-owned linkedin_scraper v3 session after manual sign-in.

Run with the isolated linkedin_scraper interpreter; never stores a password.
"""
import argparse
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit


async def login(destination):
    from linkedin_scraper import BrowserManager
    if destination.exists():
        raise SystemExit("Session already exists; move it aside explicitly before replacing it")
    if os.name != "nt":
        os.umask(0o077)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    async with BrowserManager(headless=False) as browser:
        await browser.page.goto("https://www.linkedin.com/login")
        print("Sign in manually in the opened browser. Complete any required verification yourself.")
        await asyncio.to_thread(input, "After your LinkedIn feed is visible, press Enter here: ")
        current = urlsplit(browser.page.url)
        if current.scheme != "https" or current.hostname not in {"linkedin.com", "www.linkedin.com"} or not current.path.startswith("/feed"):
            raise SystemExit("A logged-in feed was not confirmed; no session was saved")
        await browser.save_session(str(destination))
    if os.name != "nt":
        destination.chmod(0o600)
    print("Session saved. Keep it private and do not commit it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    asyncio.run(login(args.destination.expanduser()))

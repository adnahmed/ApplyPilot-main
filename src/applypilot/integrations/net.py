"""Public-web fetching with redirect, size and address validation.

Configured loopback services are a separate trust boundary; their address is
never inferred from page content. No HTTP proxy or ambient credentials are used.
Browser workers additionally need an egress-restricted sandbox (see the guide).
"""
from __future__ import annotations

from applypilot import operations as ops

import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from .search_providers import retry_after

from .types import SourceError

USER_AGENT = "ApplyPilotResearch/1.0"
MAX_BYTES = 3_000_000
_TRACKING = {"fbclid", "gclid", "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content"}


def canonical_url(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192 or re.search(r"[\s\x00-\x1f\x7f]", value):
        raise SourceError("Invalid HTTP(S) URL", "schema")
    try:
        p = urlsplit(value)
        if p.scheme.lower() not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError
        port = p.port
        host = p.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        authority = f"[{host}]" if ":" in host else host
        if port and port != (443 if p.scheme == "https" else 80):
            authority += f":{port}"
        return urlunsplit((p.scheme.lower(), authority, p.path or "/",
                           urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                                      if k.lower() not in _TRACKING]), ""))
    except (ValueError, UnicodeError) as exc:
        raise SourceError("Invalid HTTP(S) URL", "schema") from exc


def public_url(value: str, *, resolve=True) -> str:
    url = canonical_url(value)
    p = urlsplit(url)
    host = p.hostname
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host and ":" not in host:
        raise SourceError("Private/local web targets are not allowed", "access_denied")
    if p.port not in (None, 80, 443):
        raise SourceError("Public crawling is restricted to HTTP(S) ports 80/443", "access_denied")
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        if not resolve:
            return url
        try:
            ips = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, p.port or 443, type=socket.SOCK_STREAM)]
        except OSError as exc:
            raise SourceError("Public hostname could not be resolved", "network") from exc
    if not ips or any(not ip.is_global for ip in ips):
        raise SourceError("Non-public IP addresses are not allowed", "access_denied")
    return url


def check_status(status, headers=None):
    if status >= 400:
        error = SourceError(f"Upstream HTTP {status}", "unknown",
                            retry_after((headers or {}).get("retry-after")))
        error.http_status = status
        raise error


@ops.observe('integrations', 'http_fetch')
def fetch(url: str, *, max_bytes=MAX_BYTES, timeout=25, before_request=None, on_redirect=None):
    """Follow at most four redirects, checking every destination, with no retries."""
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        for _ in range(5):
            url = public_url(url)
            if before_request:
                before_request()
            with client.stream("GET", url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/json"}) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    if not response.headers.get("location"):
                        raise SourceError("Redirect without a destination", "schema")
                    url = public_url(urljoin(url, response.headers["location"]))
                    if on_redirect:
                        on_redirect(url)
                    continue
                content = bytearray()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise SourceError("Response exceeds the configured size bound", "schema")
                from applypilot.browser.cloudflare import is_browser_challenge
                if response.status_code in {200, 403, 503} and is_browser_challenge(
                        content.decode("utf-8", errors="replace"), response.headers):
                    error = SourceError("Upstream returned a browser challenge", "unknown",
                                        retry_after(response.headers.get("retry-after")))
                    error.http_status = response.status_code
                    error.challenge_url = url
                    raise error
                check_status(response.status_code, response.headers)
                return url, bytes(content), response.headers.get("content-type", "")
        raise SourceError("Too many redirects", "access_denied")

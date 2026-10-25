"""Finite FlareSolverr operations; Temporal owns retries and waiting."""
from __future__ import annotations

import logging
import math
import os
import re
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from applypilot.browser.clearance import ClearanceCookie, ClearanceSolution

log = logging.getLogger(__name__)


def is_browser_challenge(html: str, headers=None) -> bool:
    """Observe a provider challenge, not an ordinary form containing Turnstile."""
    if any(str(key).lower() == "cf-mitigated" and str(value).lower() == "challenge"
           for key, value in (headers or {}).items()):
        return True
    return bool("/cdn-cgi/challenge-platform/" in html and (
        re.search(r"(?:window\.)?_cf_chl_opt\s*=", html)
        or re.search(r"<title>\s*Just a moment[.!\s]*</title>", html, re.I)))


def proxy_settings(proxy: str | None) -> dict | None:
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "socks4", "socks5"} or not parsed.hostname:
        raise ValueError("The browser proxy must be an HTTP, SOCKS4 or SOCKS5 URL")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    authority = host + (f":{parsed.port}" if parsed.port else "")
    result = {"url": urlunsplit((parsed.scheme, authority, "", "", ""))}
    if parsed.username is not None:
        result.update(username=unquote(parsed.username), password=unquote(parsed.password or ""))
    return result


class FlareSolverrClient:
    """One isolated provider session per operation, destroyed on completion."""

    def __init__(self, base_url: str | None = None, timeout: float = 60, proxy: str | None = None):
        self._base_url = (base_url or os.environ.get("FLARESOLVERR_URL", "http://127.0.0.1:8191")).rstrip("/")
        self._timeout = self._seconds(timeout)
        self._proxy = proxy_settings(proxy)

    @staticmethod
    def _seconds(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("Solver timeout must be a positive number of seconds")
        return float(value)

    def _post(self, client: httpx.Client, payload: dict, *, timeout: float) -> dict:
        response = client.post(self._base_url + "/v1", json=payload, timeout=timeout)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("FlareSolverr returned a non-object response")
        if value.get("status") != "ok":
            raise RuntimeError(str(value.get("message") or "FlareSolverr did not complete the request"))
        return value

    def is_available(self) -> bool:
        try:
            with httpx.Client(trust_env=False) as client:
                self._post(client, {"cmd": "sessions.list"}, timeout=3)
            return True
        except (httpx.HTTPError, ValueError, RuntimeError):
            return False

    def solve(self, url: str, *, return_html: bool = False, max_timeout: float | None = None,
              cookies: list[dict] | tuple[dict, ...] = ()) -> ClearanceSolution:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Solver requires an HTTP(S) page URL")
        seconds = self._seconds(self._timeout if max_timeout is None else max_timeout)
        session_id = "applypilot-" + uuid4().hex
        creation = {"cmd": "sessions.create", "session": session_id}
        if self._proxy:
            creation["proxy"] = self._proxy
        with httpx.Client(trust_env=False) as client:
            try:
                self._post(client, creation, timeout=seconds + 10)
                result = self._post(client, {
                    "cmd": "request.get", "url": url, "session": session_id,
                    "returnOnlyCookies": not return_html,
                    "maxTimeout": int(seconds * 1000),
                    "cookies": list(cookies),
                }, timeout=seconds + 10)
                data = result.get("solution")
                if not isinstance(data, dict):
                    raise ValueError("FlareSolverr returned no page solution")
                status = data.get("status")
                if status is not None and (type(status) is not int or not 200 <= status < 400):
                    raise RuntimeError(f"FlareSolverr page returned HTTP {status}")
                parsed_cookies = [ClearanceCookie(
                    name=cookie["name"], value=cookie["value"], domain=cookie["domain"],
                    path=cookie.get("path", "/"), secure=cookie.get("secure", True),
                    http_only=cookie.get("httpOnly", True), same_site=cookie.get("sameSite"),
                    expires=cookie.get("expires", cookie.get("expiry", -1)),
                ) for cookie in data.get("cookies", ())]
                return ClearanceSolution(
                    cookies=parsed_cookies, user_agent=data.get("userAgent", ""),
                    final_url=data.get("url") or url, solver_session_id=session_id,
                    html=data.get("response", "") if return_html else "", status=status,
                )
            finally:
                try:
                    self._post(client, {"cmd": "sessions.destroy", "session": session_id}, timeout=10)
                except Exception as exc:
                    # Preserve the operation's result/failure, including a failed create
                    # whose response was lost after the provider allocated its session.
                    log.warning("Solver session cleanup failed session=%s error=%s", session_id, type(exc).__name__)

    def solve_for_html(self, url: str, *, max_timeout: float | None = None,
                       cookies: list[dict] | tuple[dict, ...] = ()) -> tuple[ClearanceSolution, str]:
        solution = self.solve(url, return_html=True, max_timeout=max_timeout, cookies=cookies)
        if not solution.html:
            raise ValueError("FlareSolverr returned no rendered HTML")
        return solution, solution.html

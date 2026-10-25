"""Isolated 1.1.82 worker; every wire attempt needs a parent budget grant.

Only this child replaces JobSpy's session factories. It never patches requests
or tls_client classes, and cannot outlive the parent's snapshot deadline.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from importlib import import_module
from importlib.metadata import version
from urllib.parse import unquote, urlencode, urljoin, urlsplit

from applypilot.execution import ExecutionStopped, current_task
from applypilot.work_context import stage_context
from .models import FailureKind, FetchFailure
from .transport import browser_recovery_url, diagnostic_url, reserve_budget as check_request_access

VERSION = "1.1.82"
MAX_SECONDS = 120.0
MAX_MESSAGES = 20
_MAX_EVENT = 16 * 1024 * 1024
_CLASSES = {
    "indeed": ("indeed", "Indeed"), "linkedin": ("linkedin", "LinkedIn"),
    "glassdoor": ("glassdoor", "Glassdoor"), "google": ("google", "Google"),
    "zip_recruiter": ("ziprecruiter", "ZipRecruiter"), "naukri": ("naukri", "Naukri"),
    "bayt": ("bayt", "BaytScraper"), "bdjobs": ("bdjobs", "BDJobs"),
}


@dataclass(frozen=True)
class Snapshot:
    rows: tuple[dict, ...]
    failures: tuple[FetchFailure, ...]
    metadata: dict


def _google_response_diagnostics(body: str) -> dict:
    """Describe parser input without retaining provider HTML or search text."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    results = soup.select_one("#search, main, [role=main]")
    visible = results.get_text(" ", strip=True) if results else ""
    explicit_empty = bool(re.search(
        r"\b(?:no (?:matching )?jobs (?:found|available)|no jobs match(?:ed)?\b|"
        r"did not match any jobs|could(?:n['’]t| not) find any jobs)\b",
        visible, re.I,
    ))
    return {
        "response_chars": len(body),
        "response_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "job_payload_markers": body.count('520084652":'),
        "has_results_region": results is not None,
        "explicit_empty": explicit_empty,
    }


def scrape_snapshot(kwargs: dict, context, *, native_page=False, continuation=None) -> Snapshot:
    """Consume HTTP in the caller's ledger, not a copied subprocess context.

    Scheduler callbacks account for prepaid HTTP themselves. Legacy callers use
    the same transport fallback ledger as other adapters, keyed by cycle_id.
    A grant is counted immediately before the child sends, even if IO fails.
    """
    seconds = min(MAX_SECONDS, MAX_SECONDS)
    deadline = time.monotonic() + seconds
    metadata = {"transport": "isolated-jobspy-1.1.82", "http_attempts": 0,
                "detail_http_attempts": 0, "retry_http_attempts": 0,
                "redirect_http_attempts": 0, "listing_pages": 0,
                "deadline_seconds": seconds,
                "warnings": [], "method":"UNKNOWN"}
    rows, failures = [], []
    events = queue.Queue(maxsize=8)
    stop = threading.Event()
    process = None
    stop_events = (current_task().get("stop_event"),
                   stage_context().get("stop_event"))

    def read_events():
        try:
            while not stop.is_set():
                line = process.stdout.readline(_MAX_EVENT + 1)
                if not line:
                    event = {"type": "eof"}
                elif len(line) > _MAX_EVENT:
                    event = {"type": "protocol_error"}
                else:
                    event = json.loads(line)
                while not stop.is_set():
                    try:
                        events.put(event, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if event["type"] in {"eof", "protocol_error", "done"}:
                    return
        except (OSError, ValueError, KeyError):
            if not stop.is_set():
                try:
                    events.put({"type": "protocol_error"}, timeout=0.1)
                except queue.Full:
                    pass

    try:
        if any(event is not None and event.is_set() for event in stop_events):
            raise ExecutionStopped("Discovery stopped before JobSpy snapshot")
        process = subprocess.Popen(
            [sys.executable, "-u", "-m", "applypilot.discovery.jobspy_transport"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        reader = threading.Thread(target=read_events, daemon=True, name="jobspy-events")
        reader.start()
        payload = {"kwargs": kwargs, "seconds": seconds,
                   "native_page": native_page, "continuation": continuation,
                   "browser_recovery_url": browser_recovery_url()}
        process.stdin.write((json.dumps(payload, allow_nan=False) + "\n").encode())
        process.stdin.flush()
        while True:
            if any(event is not None and event.is_set() for event in stop_events):
                raise ExecutionStopped("Discovery stopped during JobSpy snapshot")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("JobSpy snapshot deadline reached")
            try:
                event = events.get(timeout=min(remaining, 0.2))
            except queue.Empty:
                continue
            kind = event["type"]
            if kind == "http":
                metadata.update(url=event.get('url'),method=event.get('method','UNKNOWN'),
                                recovery_eligible=event.get('recovery_eligible',False))
                check_request_access(context, "http", 1)
                metadata["http_attempts"] += 1
                for field in ("detail", "retry", "redirect"):
                    metadata[f"{field}_http_attempts"] += int(event.get(field, False))
                metadata["listing_pages"] = event["listing_pages"]
                process.stdin.write(b'{"allowed":true}\n')
                process.stdin.flush()
            elif kind == "row":
                if len(rows) < kwargs["results_wanted"]:
                    rows.append(event["row"])
            elif kind in {"failure", "warning"}:
                if event.get("challenge_url"):
                    from applypilot.integrations.types import SourceError
                    error = SourceError("JobSpy provider returned a browser challenge", "unknown",
                                        event.get("retry_after"))
                    error.challenge_url = event["challenge_url"]
                    error.http_status = event.get("http_status")
                    raise error
                url = event.get("url")
                if isinstance(url, str) and url.startswith("https://"):
                    metadata["recovery_url"] = url
                    metadata['url'] = url
                metadata['method'] = event.get('method',metadata['method'])
                for field in ('redirect_reason','redirect_target','exception_type','exception_detail','recovery_eligible',
                              'response_diagnostics'):
                    if field in event:
                        metadata[field] = event[field]
                failure = FetchFailure(FailureKind(event["kind"]), event["message"][:1000],
                                       event.get("http_status"), event.get("retry_after"),
                                       event.get("provider_field"))
                if kind == "warning":
                    if len(metadata["warnings"]) < MAX_MESSAGES:
                        metadata["warnings"].append(event)
                elif failure not in failures and len(failures) < MAX_MESSAGES:
                    failures.append(failure)
                if event.get("fatal"):
                    break
            elif kind == "done":
                if "response_diagnostics" in event:
                    metadata["response_diagnostics"] = event["response_diagnostics"]
                # The final response is the library's own offset/slice selection.
                if event.get("native_page"):
                    rows = event["rows"]  # Do not drop tail rows before advancing a native cursor.
                    metadata.update(native_page=True, next_native_cursor=event.get("next_cursor"))
                else:
                    rows = event["rows"][:kwargs["results_wanted"]]
                break
            else:
                raise RuntimeError("JobSpy worker exited without a valid snapshot")
    finally:
        stop.set()
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            process.stdin.close()
            # Killing the child closes stdout and releases the reader.
            reader.join(timeout=1)
            process.stdout.close()
    metadata["elapsed_seconds"] = round(seconds - (deadline - time.monotonic()), 3)
    return Snapshot(tuple(rows), tuple(failures), metadata)


class _Abort(BaseException):
    """Escapes library `except Exception`; fatal IPC also defeats bare except."""


def _worker():
    # Keep stdout exclusively for IPC, including when a dependency prints.
    wire = sys.stdout
    sys.stdout = sys.stderr
    payload = json.loads(sys.stdin.readline())
    kwargs = payload["kwargs"]
    board = kwargs["site_name"][0]
    deadline = time.monotonic() + payload["seconds"]
    lock = threading.RLock()
    stopped = False
    pages = 0
    messages = 0
    captured = []
    seen = set()
    processed = 0
    current_url = ""
    current_method = 'UNKNOWN'
    current_metadata = {}
    google_diagnostics = {}

    def emit(event):
        with lock:
            wire.write(json.dumps(event, allow_nan=False, ensure_ascii=True) + "\n")
            wire.flush()

    def fail(message, status=None, retry_after=None, **details):
        nonlocal stopped
        stopped = True
        emit({"type": "failure", "kind": FailureKind.UNKNOWN.value, "message": message[:1000],
              "http_status": status, "retry_after": retry_after, "fatal": True,
              "url": current_url or None, "method":current_method,
              **current_metadata, **details})
        raise _Abort()

    def check():
        if stopped:
            raise _Abort()
        if time.monotonic() >= deadline:
            fail("JobSpy snapshot deadline reached")

    def board_host(host):
        root = "ziprecruiter" if board == "zip_recruiter" else board
        return bool(host) and (host.startswith(root + ".") or ("." + root + ".") in host)

    from .jobspy import _failure, _jobpost_row
    from .transport import TransportResult, _response_error
    import requests
    import tls_client
    from jobspy.model import Country, ScraperInput, Site
    from jobspy.util import RotatingProxySession, get_enum_from_value

    class Capture(logging.Handler):
        def emit(self, record):
            nonlocal messages
            if record.levelno < logging.WARNING:
                return
            with lock:
                if messages >= MAX_MESSAGES or stopped:
                    return
                messages += 1
                # Do not persist provider bodies, URLs or proxy credentials from logs.
                emit({"type": "failure" if record.levelno >= logging.ERROR else "warning",
                      "kind": FailureKind.UNKNOWN.value,
                      "message": f"{board} provider logged {record.levelname.lower()}",
                      "provider_field": record.name})

    class Session:
        def __init__(self, *, proxies=None, ca_cert=None, is_tls=True,
                     has_retry=False, delay=1, clear_cookies=False):
            # Custom CA verification is supported by requests, not tls-client.
            self.tls = is_tls and not ca_cert
            self.inner = (tls_client.Session(client_identifier="chrome_120", catch_panics=True)
                          if self.tls else requests.Session())
            self.headers = self.inner.headers
            self.proxy_cycle = RotatingProxySession(proxies).proxy_cycle
            self.retries = 0
            self.delay = min(float(delay), 5)
            self.clear_cookies = clear_cookies
            self.ca_cert = ca_cert
            if not self.tls:
                self.inner.trust_env = False
                # No urllib3 retry can happen below our accounting boundary.
                self.inner.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
                self.inner.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def get(self, url, **options):
            return self.request("GET", url, **options)

        def post(self, url, **options):
            return self.request("POST", url, **options)

        def request(self, method, url, **options):
            nonlocal pages, current_url, current_method, current_metadata, google_diagnostics
            # Library detail pools share a session. Serialize the *wire* boundary
            # so no new request can start after a denial (or a refused grant).
            with lock:
                current_url = diagnostic_url(url)
                current_method = str(method).upper()
                current_metadata = {'recovery_eligible':False}
                check()
                host = urlsplit(url).hostname or ""
                root = "ziprecruiter" if board == "zip_recruiter" else board
                if not (host.startswith(root + ".") or ("." + root + ".") in host):
                    fail("JobSpy request left its board host")
                path = urlsplit(url).path.lower()
                body = str(options.get("data", options.get("json", "")))
                listing = (board == "indeed" or
                    board == "google" and path in {"/search", "/async/callback:550"} or
                    board == "linkedin" and "/search" in path or
                    board == "bayt" or board == "bdjobs" and path.endswith("jobsearch.asp") or
                    board == "naukri" and "/search" in path or
                    board == "zip_recruiter" and path.endswith("/jobs-app/jobs") or
                    board == "glassdoor" and "JobSearchResultsQuery" in body)
                detail = (board == "glassdoor" and "JobDetailQuery" in body or
                          board == "linkedin" and "/jobs/view/" in path or
                          board == "bdjobs" and "jobdetail" in path or
                          board == "zip_recruiter" and "/jobs/" in path)
                if listing:
                    pages += 1
                redirects = 0
                follow = options.pop("allow_redirects", True)
                requested_timeout = options.pop("timeout", options.pop("timeout_seconds", 20))
                if isinstance(requested_timeout, tuple):
                    requested_timeout = max(requested_timeout)
                # Never honor the library's verify=False.
                options.pop("verify", None)
                if self.clear_cookies:
                    self.inner.cookies.clear()
                if self.proxy_cycle:
                    proxy = next(self.proxy_cycle)
                    self.inner.proxies = {} if proxy["http"] == "http://localhost" else proxy
                while True:
                    query = urlencode(options.get('params') or {},doseq=True)
                    request_url = url + ('&' if '?' in url else '?') + query if query else url
                    current_url = diagnostic_url(request_url)
                    current_method = str(method).upper()
                    current_metadata = {'recovery_eligible':current_method == 'GET' and '[redacted]' not in unquote(current_url)
                                        and not urlsplit(request_url).username}
                    check()
                    emit({"type": "http", "detail": detail, "retry": False,
                          "redirect": redirects > 0, "listing_pages": pages,
                          "url":current_url,"method":current_method,**current_metadata})
                    if json.loads(sys.stdin.readline()).get("allowed") is not True:
                        fail("Provider operation was cancelled or is no longer authorized")
                    timeout = max(0.1, min(float(requested_timeout or 20), 20, deadline - time.monotonic()))
                    try:
                        from applypilot.integrations.net import public_url
                        public_request = public_url(request_url)
                        if method == "GET" and public_request == payload.get("browser_recovery_url"):
                            from applypilot.browser.cloudflare import FlareSolverrClient
                            import os
                            solution, html = FlareSolverrClient(
                                proxy=os.environ.get("APPLYPILOT_BROWSER_PROXY_URL")).solve_for_html(
                                    public_request, max_timeout=timeout)
                            response = requests.Response()
                            response.status_code = solution.status or 200
                            response.url = solution.final_url
                            response._content = html.encode("utf-8")
                            response.encoding = "utf-8"
                        elif self.tls:
                            response = self.inner.execute_request(method, url, allow_redirects=False,
                                timeout_seconds=max(1, math.ceil(timeout)), insecure_skip_verify=False, **options)
                            response.ok = 200 <= response.status_code < 400
                        else:
                            response = self.inner.request(method, url, allow_redirects=False,
                                timeout=timeout, verify=self.ca_cert or True, **options)
                    except Exception as exc:
                        failure = _failure(exc)
                        from applypilot.operations.runtime import sanitize
                        detail = sanitize(exc,700)
                        fail(f"{board} HTTP attempt failed: {type(exc).__name__}: {detail}",
                             status=failure.http_status, retry_after=failure.retry_after,
                             exception_type=type(exc).__name__,exception_detail=detail)
                    status = response.status_code
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    from applypilot.browser.cloudflare import is_browser_challenge
                    if is_browser_challenge(response.text, headers):
                        error = _response_error(TransportResult(response.text, status, headers, str(response.url), "jobspy"))
                        eligible = (current_metadata["recovery_eligible"]
                                    and public_request != payload.get("browser_recovery_url"))
                        fail(f"{board} provider returned a browser challenge", status, error.retry_after,
                             challenge_url=public_request if eligible else None)
                    if status in {301, 302, 303, 307, 308}:
                        target = urljoin(url, headers.get("location", ""))
                        reason = ('redirects_disabled' if not follow else 'redirect_limit' if redirects >= 3 else
                                  'missing_location' if not headers.get('location') else
                                  'outside_board_host' if not board_host(urlsplit(target).hostname or '') else
                                  'non_https_redirect' if urlsplit(target).scheme != 'https' else None)
                        if reason:
                            fail(f"{board} redirect refused: {reason}", status,
                                 redirect_reason=reason,redirect_target=diagnostic_url(target))
                        url = target
                        current_url = url
                        options.pop("params", None)
                        if status == 303 or status in {301, 302} and method == "POST":
                            method = "GET"
                            for key in ("data", "json", "files"):
                                options.pop(key, None)
                        redirects += 1
                        continue
                    if not 200 <= status < 300:
                        error = _response_error(TransportResult(response.text, status, headers, str(response.url), "jobspy"))
                        fail(f"{board} HTTP {status}", status, error.retry_after)
                    if board == "google" and urlsplit(url).path == "/search":
                        google_diagnostics = _google_response_diagnostics(response.text)
                        google_diagnostics["http_status"] = status
                    return response

    try:
        if version("python-jobspy") != VERSION:
            fail(f"Requires python-jobspy=={VERSION}")
        module_name, class_name = _CLASSES[board]
        module = import_module(f"jobspy.{module_name}")
        for name in tuple(logging.root.manager.loggerDict):
            if name.startswith("JobSpy:"):
                logger = logging.getLogger(name)
                logger.handlers = [Capture()]
                logger.setLevel(logging.WARNING)
                logger.propagate = False
        module.create_session = Session
        if board == "glassdoor":
            # 1.1.82 calls module.requests.post for descriptions instead of its
            # own session. A module-local facade keeps that request metered too.
            class RequestsFacade:
                exceptions = requests.exceptions

                @staticmethod
                def post(url, **options):
                    return scraper.session.post(url, **options)

            module.requests = RequestsFacade
        constructor = {key: kwargs[key] for key in ("proxies", "ca_cert", "user_agent") if key in kwargs}
        if board == "bdjobs":
            constructor.pop("user_agent", None)  # BDJobs lacks this constructor keyword.
        scraper = getattr(module, class_name)(**constructor)
        if kwargs.get("user_agent") and getattr(scraper, "session", None):
            scraper.session.headers["User-Agent"] = kwargs["user_agent"]
        method_name = {"bayt": "_extract_job_info", "google": "_parse_job"}.get(board, "_process_job")
        original = getattr(scraper, method_name)

        def capture_job(*args, **options):
            nonlocal processed
            check()
            job = original(*args, **options)
            if job is not None:
                with lock:
                    index = processed
                    processed += 1
                    skip = kwargs.get("offset", 0) if board in {"indeed", "google"} else 0
                    row = _jobpost_row(job, board, kwargs["country_indeed"])
                    key = (row.get("id"), row.get("job_url"))
                    if index >= skip and key not in seen and len(captured) < kwargs["results_wanted"]:
                        seen.add(key)
                        captured.append(row)
                        emit({"type": "row", "row": row})
            return job

        setattr(scraper, method_name, capture_job)
        inputs = {key: value for key, value in kwargs.items() if key in ScraperInput.model_fields}
        inputs.update(site_type=[Site(board)], country=Country.from_string(kwargs["country_indeed"]),
                      request_timeout=20)
        if kwargs.get("job_type"):
            inputs["job_type"] = get_enum_from_value(kwargs["job_type"])
        if payload.get("native_page"):
            # Use the pinned parser/session initialization, stopping at a real
            # provider page boundary. _Abort bypasses SDK catch-and-log loops;
            # it never denotes success unless a complete `done` envelope was sent.
            token = payload.get("continuation")
            if token is not None and (not isinstance(token, str) or len(token) > 65536):
                fail("Invalid native continuation")

            def finish_page(jobs, next_cursor):
                check()
                if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 65536):
                    fail("Invalid provider continuation")
                if board == "google" and token is None:
                    google_diagnostics["parsed_jobs"] = len(jobs)
                    if not jobs and (not google_diagnostics.get("explicit_empty")
                                     or google_diagnostics.get("job_payload_markers")):
                        fail("Google returned no parsed jobs without a verified empty-results response",
                             response_diagnostics=google_diagnostics)
                emit({"type": "done", "native_page": True, "next_cursor": next_cursor,
                      "rows": [_jobpost_row(job, board, kwargs["country_indeed"]) for job in jobs],
                      **({"response_diagnostics": google_diagnostics} if board == "google" else {})})
                raise _Abort()

            if board == "indeed":
                read_page = scraper._scrape_page

                def one_indeed_page(_cursor):
                    jobs, next_cursor = read_page(token)
                    finish_page(jobs, next_cursor)

                scraper._scrape_page = one_indeed_page
            elif board == "google":
                read_initial = scraper._get_initial_cursor_and_jobs
                read_next = scraper._get_jobs_next_page

                def one_google_page():
                    if token is None:
                        next_cursor, jobs = read_initial()
                    else:
                        jobs, next_cursor = read_next(token)
                    finish_page(jobs, next_cursor)

                scraper._get_initial_cursor_and_jobs = one_google_page
            else:
                fail("Native continuation unsupported for this board")
        response = scraper.scrape(ScraperInput(**inputs))
        check()
        emit({"type": "done", "rows": [_jobpost_row(job, board, kwargs["country_indeed"])
              for job in response.jobs[:kwargs["results_wanted"]]]})
    except _Abort:
        pass
    except Exception as exc:
        failure = _failure(exc)
        emit({"type": "failure", "kind": failure.kind.value,
              "message": f"{board} worker failed: {type(exc).__name__}", "fatal": True,
              **({"response_diagnostics": google_diagnostics} if google_diagnostics else {})})


if __name__ == "__main__":
    _worker()

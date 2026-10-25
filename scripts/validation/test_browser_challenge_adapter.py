"""Offline provider/browser contract checks; no applications or live providers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from applypilot.apply import cloudflare_tool
from applypilot.browser import cloudflare
from applypilot.browser.backends import BrowserOSBackend
from applypilot.browser.clearance import ClearanceCookie, ClearanceSolution


def transport(monkeypatch, failure=None, cleanup_failure=False):
    calls = []

    def request(request):
        payload = json.loads(request.content)
        calls.append((payload, request.extensions["timeout"]["read"]))
        if payload["cmd"] == "request.get":
            if failure is not None:
                raise failure
            return httpx.Response(200, json={"status": "ok", "solution": {
                "url": payload["url"], "status": 200, "userAgent": "solver-ua",
                "response": "<main>Actual vacancy</main>",
                "cookies": [{"name": "cf_clearance", "value": "private-clearance",
                             "domain": ".example.org", "httpOnly": True}],
            }})
        if payload["cmd"] == "sessions.destroy" and cleanup_failure:
            return httpx.Response(500, text="cleanup unavailable")
        return httpx.Response(200, json={"status": "ok", "session": payload.get("session")})

    real_client = httpx.Client
    monkeypatch.setattr(cloudflare.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(request), **kwargs))
    return calls


def test_solver_preserves_route_deadline_and_isolated_session_lifecycle(monkeypatch):
    calls = transport(monkeypatch)
    client = cloudflare.FlareSolverrClient(proxy="http://user:pa%24%24@proxy.example:8080")
    first = client.solve("https://jobs.example.org/posting", max_timeout=30)
    second, html = client.solve_for_html("https://jobs.example.org/posting", max_timeout=30)
    assert html == "<main>Actual vacancy</main>"
    assert first.solver_session_id != second.solver_session_id
    assert [value["cmd"] for value, _ in calls] == ["sessions.create", "request.get", "sessions.destroy"] * 2
    assert calls[0][0]["proxy"] == {"url": "http://proxy.example:8080", "username": "user", "password": "pa$$"}
    for offset in (0, 3):
        created, fetched, destroyed = [value for value, _ in calls[offset:offset + 3]]
        assert created["session"] == fetched["session"] == destroyed["session"]
        assert fetched["maxTimeout"] == 30_000
        assert calls[offset + 1][1] > 30
    assert calls[1][0]["returnOnlyCookies"] is True
    assert calls[4][0]["returnOnlyCookies"] is False
    assert "private-clearance" not in repr(first)
    assert "private-clearance" not in repr(first.cookies[0])


def test_original_network_failure_survives_cleanup_failure(monkeypatch):
    failure = httpx.ReadTimeout("observed socket timeout")
    calls = transport(monkeypatch, failure, cleanup_failure=True)
    with pytest.raises(httpx.ReadTimeout) as caught:
        cloudflare.FlareSolverrClient().solve("https://example.org/job")
    assert caught.value is failure
    assert calls[-1][0]["cmd"] == "sessions.destroy"


def test_create_response_loss_still_destroys_owned_session(monkeypatch):
    calls = []

    def post(self, client, payload, *, timeout):
        calls.append(payload)
        if payload["cmd"] == "sessions.create":
            raise httpx.ReadTimeout("create response lost")
        return {"status": "ok"}

    monkeypatch.setattr(cloudflare.FlareSolverrClient, "_post", post)
    with pytest.raises(httpx.ReadTimeout, match="create response lost"):
        cloudflare.FlareSolverrClient().solve("https://example.org/job")
    assert [value["cmd"] for value in calls] == ["sessions.create", "sessions.destroy"]
    assert calls[0]["session"] == calls[1]["session"]


def test_provider_failure_message_is_not_reclassified(monkeypatch):
    def request(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json={"status": "error", "message": "New provider failure 123"}
                              if payload["cmd"] == "request.get" else {"status": "ok"})

    real_client = httpx.Client
    monkeypatch.setattr(cloudflare.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(request), **kwargs))
    with pytest.raises(RuntimeError, match="New provider failure 123"):
        cloudflare.FlareSolverrClient().solve("https://example.org/job")


def test_owned_recovery_keeps_cookie_material_inside_adapter(monkeypatch):
    calls = transport(monkeypatch)
    scripts = []
    checked = []

    async def run(script):
        scripts.append(script)
        return [
            {"url": "https://jobs.example.org/posting", "cookies": [
                {"name": "session", "value": "private-session", "domain": ".example.org"},
                {"name": "other", "value": "unrelated", "domain": ".elsewhere.org"}]},
            {"applied": True},
            {"url": "https://jobs.example.org/posting", "title": "Engineer", "text": "Apply now"},
        ][len(scripts) - 1]

    async def check():
        checked.append(True)

    result = asyncio.run(cloudflare_tool.solve_owned_page(7, run, proxy="http://route:8080", check=check))
    assert checked == [True]
    assert result == {"page": 7, "clearance_applied": True, "url": "https://jobs.example.org/posting",
                      "title": "Engineer", "text": "Apply now"}
    assert calls[1][0]["cookies"] == [{"name": "session", "value": "private-session", "domain": ".example.org"}]
    assert calls[0][0]["proxy"] == {"url": "http://route:8080"}
    assert "private-clearance" in scripts[1]
    assert "private-" not in json.dumps(result)


@pytest.mark.parametrize("moved", [False, True])
def test_native_clearance_is_page_scoped_and_checks_navigation(moved):
    solution = ClearanceSolution(cookies=[ClearanceCookie("cf_clearance", "secret", ".example.org")],
                                 user_agent="agent's \\ quoted user agent")
    script = BrowserOSBackend().apply_clearance_script(7, solution, expected_url="https://example.org/job")
    harness = r"""
const input=JSON.parse(process.argv[1]),calls=[];
const browser={cdpJsonForPage:async(page,method,params)=>{
  calls.push({page,method,params:JSON.parse(params)});
  if(method==='Runtime.evaluate')return {result:{value:input.moved?'https://elsewhere.org/':'https://example.org/job'}};
  return {};
},nav:page=>({reload:async()=>calls.push({page,method:'reload'})})};
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
(async()=>{try{console.log(JSON.stringify({result:await new AsyncFunction('browser',input.script)(browser),calls}));}
catch(error){console.log(JSON.stringify({error:error.message,calls}));}})();
"""
    observed = json.loads(subprocess.run(["node", "-e", harness, json.dumps({"script": script, "moved": moved})],
                                        check=True, capture_output=True, text=True).stdout)
    if moved:
        assert "navigated" in observed["error"]
        assert len(observed["calls"]) == 1
    else:
        assert observed["result"] == {"applied": True}
        assert [call["method"] for call in observed["calls"]] == [
            "Runtime.evaluate", "Network.setCookies", "Emulation.setUserAgentOverride", "reload"]
        assert all(call["page"] == 7 for call in observed["calls"])


def test_strict_runtime_exposes_semantic_solver_without_arbitrary_javascript():
    from applypilot.apply.runtime import RuntimeSelection, allowed_tools

    browseros = allowed_tools(RuntimeSelection("browseros", "browseros", "fixture", "http://browser/mcp"))
    assert "mcp__browseros_neo__solve_browser_challenge" in browseros
    playwright = allowed_tools(RuntimeSelection("playwright", "playwright", "fixture", "http://browser/mcp"))
    assert "mcp__cloudflare__solve_browser_challenge" in playwright
    assert "mcp__playwright__browser_run_code" not in playwright
    assert "mcp__applypilot_control__require_verification" not in playwright
    assert "mcp__applypilot_control__record_application_observation" in playwright
    dry = allowed_tools(RuntimeSelection("browseros", "browseros", "fixture", "http://browser/mcp"), dry_run=True)
    assert not any("solve_browser_challenge" in name for name in dry)


def test_playwright_mcp_contract_accepts_only_owned_tab_index():
    server = cloudflare_tool.build_server(cdp_port=9222, attempt_id="isolated-attempt")
    tools = asyncio.run(server.list_tools())
    assert [tool.name for tool in tools] == ["solve_browser_challenge"]
    schema = tools[0].inputSchema
    assert set(schema["properties"]) == {"page_id"}
    assert schema["required"] == ["page_id"]


def test_cancelled_solve_finishes_cleanup_without_applying_result(monkeypatch):
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def solve(self, url, **kwargs):
        started.set()
        try:
            assert release.wait(5)
            return ClearanceSolution(cookies=[ClearanceCookie("cf_clearance", "private", ".example.org")],
                                     user_agent="ua")
        finally:
            finished.set()

    monkeypatch.setattr(cloudflare.FlareSolverrClient, "solve", solve)

    async def scenario():
        operation = asyncio.create_task(cloudflare_tool._solve("https://example.org/job", []))
        assert await asyncio.to_thread(started.wait, 5)
        operation.cancel()
        await asyncio.sleep(0)
        assert not operation.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await operation
        assert finished.is_set()

    asyncio.run(scenario())


@pytest.mark.parametrize("status,body,headers,challenge", [
    (403, "Access denied", {}, False),
    (429, "Rate limit", {"cf-mitigated": "challenge"}, False),
    (403, "Challenge", {"cf-mitigated": "challenge"}, True),
    (200, '<title>Just a moment...</title><script src="/cdn-cgi/challenge-platform/a.js"></script>', {}, True),
])
def test_http_exposes_observed_challenge_without_misclassifying_restrictions(monkeypatch, status, body, headers, challenge):
    from applypilot.integrations import net
    from applypilot.integrations.types import SourceError

    real_client = httpx.Client
    monkeypatch.setattr(net, "public_url", lambda url: url)
    monkeypatch.setattr(net.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body, headers=headers)), **kwargs))
    with pytest.raises(SourceError) as caught:
        net.fetch("https://example.org/job")
    assert bool(getattr(caught.value, "challenge_url", None)) is challenge


def test_embedded_turnstile_is_not_a_page_challenge():
    assert not cloudflare.is_browser_challenge('<form><div class="cf-turnstile"></div></form>')


def test_explicit_solver_reader_is_one_operation_and_retains_document(monkeypatch):
    from applypilot.integrations import crawl

    calls = []

    class State:
        def get(self, key):
            return None

        def put(self, *args, **kwargs):
            pass

    def read(self, url, **kwargs):
        calls.append(url)
        return ClearanceSolution(final_url=url, status=200), '<title>Engineer</title><main>Actual vacancy</main>'

    monkeypatch.setattr(cloudflare.FlareSolverrClient, "solve_for_html", read)
    monkeypatch.setattr(crawl, "public_url", lambda url, **kwargs: url)
    document = crawl.CrawlService("flaresolverr", state=State()).read("https://example.org/job", fresh=True)
    assert calls == ["https://example.org/job"]
    assert document.provider == "flaresolverr"
    assert document.title == "Engineer"
    assert "Actual vacancy" in document.markdown


def test_discovery_explicit_solver_does_not_fall_back_to_another_request(monkeypatch):
    from applypilot.discovery import transport as discovery_transport
    from applypilot.integrations import net

    calls = []

    def read(self, url, **kwargs):
        calls.append((url, kwargs))
        return ClearanceSolution(final_url=url, status=200, user_agent="solver-ua"), "<main>Jobs</main>"

    monkeypatch.setattr(cloudflare.FlareSolverrClient, "solve_for_html", read)
    monkeypatch.setattr(net, "public_url", lambda url: url)
    monkeypatch.setattr(discovery_transport, "_plain_request", lambda *args, **kwargs: pytest.fail("Hidden HTTP fallback"))
    result = discovery_transport.fetch_with_transport("https://example.org/jobs", force_layer="flaresolverr", timeout=45)
    assert result.layer == "flaresolverr"
    assert result.html == "<main>Jobs</main>"
    assert calls == [("https://example.org/jobs", {"max_timeout": 45})]


@pytest.mark.parametrize("challenge", [True, False])
def test_enrichment_recovers_observed_challenge_but_does_not_solve_generic403(monkeypatch, challenge):
    from applypilot.enrichment import browser, detail
    from applypilot.integrations import crawl, net
    from applypilot.integrations.types import Document

    readers = []
    candidate = {"status": "error", "error": "Browser challenge observed" if challenge else "HTTP 403"}
    if challenge:
        candidate["challenge_url"] = "https://example.org/job"
    monkeypatch.setattr(detail, "_scrape_browser_detail", lambda page, url: candidate)
    monkeypatch.setattr(browser, "check_running", lambda: None)
    monkeypatch.setattr(net, "public_url", lambda url: url)
    monkeypatch.setattr(detail, "extract_from_json_ld", lambda data: {
        "full_description": "Engineer role", "application_url": "https://example.org/apply"})

    class Reader:
        def __init__(self, backend, **kwargs):
            readers.append(backend)

        def read(self, url, **kwargs):
            return Document(url, "Engineer", "Engineer role", (), "flaresolverr", structured_data=({},))

    monkeypatch.setattr(crawl, "CrawlService", Reader)
    result = detail.scrape_detail_page(object(), "https://example.org/job")
    assert readers == (["flaresolverr"] if challenge else [])
    assert result["status"] == ("ok" if challenge else "error")

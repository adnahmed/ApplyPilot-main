"""Free-source diagnostics, collection, enrichment and existing-workflow bridges."""
from __future__ import annotations

from functools import wraps
import json
import os
from pathlib import Path
from typing import Optional

import typer

from .io import read_json, redact, write_json
from .types import SourceError

app = typer.Typer(help="Free/local sources, cited AI research and reviewed referral handoff.", no_args_is_help=True)


def command(name=None):
    def register(function):
        @wraps(function)
        def guarded(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (SourceError, ValueError, OSError) as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(2) from None
        app.command(name)(guarded)
        return guarded
    return register


@app.callback()
def initialize():
    from applypilot.config import load_env
    load_env()


def emit(data, output=None):
    if output is not None:
        write_json(output, data)
        typer.echo(str(output))
    else:
        typer.echo(json.dumps(data, ensure_ascii=False, indent=2, default=str))


@command('action-prepare')
def action_prepare(kind: str, arguments: Path = typer.Option(..., '--arguments', exists=True, dir_okay=False)):
    """Save one exact professional action as an unapproved draft."""
    from applypilot import config
    from applypilot.tracking.feedback_cli import connection
    from applypilot.unattended.settings import load
    from applypilot.orchestration.contracts import digest
    from .social_actions import prepare
    cfg = load()
    channel = 'linkedin' if kind == 'send_connection' else 'x'
    from applypilot.referrals.store import email_address
    account = digest({'channel': channel, 'sender': email_address(config.load_profile()['personal']['email']),
                      'account': cfg['accounts'][channel]})
    conn = connection(True)
    try:
        result = prepare(conn, kind, account, read_json(arguments))
        conn.commit()
        emit({**result, 'account_id': account})
    finally:
        conn.close()


@command('action-approve')
def action_approve(operation_id: str, expected_hash: str = typer.Option(..., '--expected-hash')):
    """Approve an exact saved action and submit its durable Workflow."""
    import asyncio
    from applypilot.tracking.feedback_cli import connection
    from applypilot.orchestration.integration_client import submit_professional_action
    from .social_actions import approve
    conn = connection(True)
    try:
        approve(conn, operation_id, expected_hash)
    finally:
        conn.close()
    emit(asyncio.run(submit_professional_action(operation_id)))


@command("doctor")
def doctor():
    """Local availability only: does not start a browser, contact a service or use an account."""
    import importlib.util
    import sys
    from .registry import SOURCES
    from .worker import PACKAGES, python_for
    from applypilot.config import APP_DIR
    tools = {}
    from .container_runtime import selected, runtime
    for provider in PACKAGES:
        try:
            if selected(provider):
                tools[provider] = "Docker source-tools available; use Connections to verify imports" if runtime() else "Docker source-tools not running"
            else:
                python_for(provider)
                tools[provider] = "local interpreter present; SDK/runtime health not checked"
        except SourceError:
            tools[provider] = "not installed"
    emit({"python_supported": (3, 11) <= sys.version_info[:2] < (3, 13), "python": sys.version.split()[0],
          "optional_workers": tools, "mcp_sdk_present": importlib.util.find_spec("mcp") is not None,
          "mcp_configuration_present": Path(os.environ.get("APPLYPILOT_MCP_CONFIG", APP_DIR / "free_sources_mcp.json")).is_file(),
          "search_backend": os.environ.get("APPLYPILOT_SEARCH_BACKEND", "auto"),
          "searxng_url": os.environ.get("SEARXNG_URL") or "http://127.0.0.1:8888", "llm_gateway_configured": bool(os.environ.get("LLM_URL")),
          "sources": [{"repository": r, "disposition": d, "functionality": f} for r, d, f in SOURCES],
          "live_connectivity_checked": False, "new_paid_data_services": [],
          "note": "Optional servers/SDKs require installation and live validation. Your own model/compute costs are separate."})


@command("search")
def search(query: str, backend: Optional[str] = None, limit: int = typer.Option(10, min=1, max=50),
           page: int = typer.Option(1, min=1, max=10), news: bool = False, output: Optional[Path] = None):
    from .search import SearchService
    emit([h.to_dict() for h in SearchService(backend).search(query, limit=limit, page=page,
                                                          category="news" if news else "general")], output)


@command("crawl")
def crawl(url: str, backend: Optional[str] = None, pages: int = typer.Option(1, min=1, max=20),
          depth: int = typer.Option(1, min=0, max=2), focus: Optional[str] = None, output: Optional[Path] = None):
    from .crawl import CrawlService
    data = CrawlService(backend).crawl(url, max_pages=pages, depth=depth, focus=focus)
    emit(data, output)
    if data["errors"]:
        raise typer.Exit(2)


@command("social")
def social(provider: str, operation: str = "posts", query: Optional[str] = None, identifier: Optional[str] = None,
           account: Optional[str] = None, start: int = typer.Option(0, min=0, max=5000),
           network: Optional[str] = None, current_company: Optional[str] = None, location: Optional[str] = None, session_file: Optional[Path] = None,
           limit: int = typer.Option(20, min=1, max=50), authorized: bool = False,
           normalize: bool = False, output: Optional[Path] = None):
    """Read your explicitly authorized session. Writes are not exposed."""
    from .social import SocialService, normalized_posts
    inputs = {"account": account, "start": start, "current_company": current_company, "location": location}
    if network is not None:
        inputs["network"] = [v.strip() for v in network.split(",")]
    if session_file is not None:
        inputs["session_file"] = str(session_file)
    data = SocialService(provider, inputs=inputs).read(operation, query=query, identifier=identifier,
                                                      limit=limit, authorized=authorized)
    if normalize:
        platform = "twitter" if provider in {"twikit", "xactions", "x_use"} else "linkedin_posts"
        data = normalized_posts(platform, data, limit=limit)
    emit(redact(data), output)


def _local_mcp_read(provider, tool=None, arguments=None, *, authorized=False):
    from applypilot.execution import browser_operation
    from .mcp import READ_TOOLS, read
    if authorized is not True or provider not in READ_TOOLS:
        raise SourceError("Choose a supported local MCP provider and explicitly authorize its session", "access_denied")
    # Browser capacity is still legacy here; the retired source database slot
    # must not be reacquired. This is not yet a native browser Activity.
    with browser_operation():
        return read(provider, tool, arguments, authorized=True)


@command("mcp-tools")
def mcp_tools(provider: str, authorized: bool = False):
    """List allowed read-tool names and schemas from an installed, trusted local server."""
    emit(_local_mcp_read(provider, authorized=authorized))


@command("mcp-read")
def mcp_read(provider: str, tool: str, arguments: str = "{}", authorized: bool = False, output: Optional[Path] = None):
    emit(redact(_local_mcp_read(provider, tool, json.loads(arguments), authorized=authorized)), output)


@command("company")
def company(target: str, fields: Optional[str] = None, queries: int = typer.Option(4, min=1, max=6),
            pages: int = typer.Option(5, min=0, max=8), search_backend: Optional[str] = None,
            crawler: Optional[str] = None, ai: bool = typer.Option(True, "--ai/--no-ai"), fresh: bool = False,
            output: Optional[Path] = None, contacts_output: Optional[Path] = None):
    from .research import FIELDS, Researcher, contacts_for_review
    report = Researcher(search=search_backend, crawler=crawler).company(
        target, fields=tuple(fields.split(",")) if fields else FIELDS, queries=queries, max_pages=pages, ai=ai, fresh=fresh)
    emit(report, output)
    if contacts_output:
        write_json(contacts_output, contacts_for_review(report))
    if report["status"] != "complete":
        raise typer.Exit(2)


@command("batch")
def batch(path: Path, output_dir: Path, limit: int = typer.Option(10, min=1, max=20),
          ai: bool = typer.Option(True, "--ai/--no-ai")):
    """Bounded resumable company batch. Input: JSON list of names/domains."""
    from .research import Researcher
    from .types import stable_id
    targets = read_json(path)
    if not isinstance(targets, list) or not targets or any(not isinstance(t, str) for t in targets):
        raise SourceError("Batch input must be a JSON list of company names/domains")
    results = []
    for target in list(dict.fromkeys(targets))[:limit]:
        dest = output_dir / (stable_id(target.casefold())[:24] + ".json")
        try:
            if dest.exists() and read_json(dest).get("status") == "complete":
                results.append({"target": target, "path": str(dest), "status": "already_complete"})
                continue
            report = Researcher().company(target, ai=ai)
            write_json(dest, report, overwrite=True)
            results.append({"target": target, "path": str(dest), "status": report["status"]})
        except SourceError as exc:
            results.append({"target": target, "status": "error", "error": str(exc)})
    emit({"results": results, "unprocessed": max(0, len(set(targets)) - limit), "sent": 0})
    if any(r["status"] in {"error", "partial"} for r in results):
        raise typer.Exit(2)


@command("enrich-file")
def enrich_file(path: Path, target: str, output: Optional[Path] = None,
                ai: bool = typer.Option(True, "--ai/--no-ai"), contacts_output: Optional[Path] = None):
    """Extract professional facts from an explicitly supplied social/company JSON export."""
    from .research import Researcher, _source, contacts_for_review
    data = redact(read_json(path))
    # No account-file auto-discovery and no input file paths forwarded to the LLM.
    text = json.dumps(data, ensure_ascii=False, indent=2)
    sources = [_source("export:" + path.name, target, text, "operator_export")]
    report = Researcher().enrich(target, sources, ai=ai)
    emit(report, output)
    if contacts_output:
        write_json(contacts_output, contacts_for_review(report))
    if report["status"] != "complete":
        raise typer.Exit(2)


@command("research-job")
def job(job_url: str, ai: bool = typer.Option(True, "--ai/--no-ai"), output: Optional[Path] = None):
    from .research import research_job
    report = research_job(job_url, ai=ai)
    emit(report, output)
    if report["status"] != "complete":
        raise typer.Exit(2)


@command("match")
def match(report_path: Path, job_url: str, output: Optional[Path] = None):
    from applypilot.config import load_profile
    from applypilot.database import get_connection
    from .research import explain_match
    job = get_connection().execute("SELECT url,title,company,full_description FROM jobs WHERE url=?", (job_url,)).fetchone()
    if not job:
        raise SourceError("Unknown job URL")
    emit(explain_match(read_json(report_path), dict(job), load_profile()), output)


@command("contacts")
def contacts(report_path: Path, output: Path):
    """Export candidates for the existing referrals import-contacts --reviewed command."""
    from .research import contacts_for_review
    write_json(output, contacts_for_review(read_json(report_path)))
    emit({"review_file": str(output), "imported": 0, "sent": 0,
          "next": "Review identity, employer, role and profile URL; then referrals import-contacts FILE --reviewed"})


@command("cache")
def cache(prune: bool = False, report_days: int = typer.Option(30, min=1, max=365)):
    from .state import SourceState
    state = SourceState()
    if prune:
        state.prune(report_days)
    emit(state.status())

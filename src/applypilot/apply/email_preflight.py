"""Read the reserved posting's application method before choosing a browser."""
from __future__ import annotations

import html
import json
import re
from datetime import UTC, datetime

from applypilot import operations as ops
from applypilot.apply.browser_controls import is_application_email, email_link_matches_posting
from applypilot.apply.routes import ApplicationRoute, parse_email_target, persist_route


def preflight_email_route(job: dict, *, dry_run: bool = False) -> dict:
    """Resolve explicit application links through the existing HTTP document reader.

    Both browser runtimes use this preflight. It never creates a browser, sends,
    or rewrites an already active reservation. JavaScript-only pages retain
    their browser route for the owned-page inspection.
    """
    if job.get("application_route") != "external" or job.get("_attempt_id"):
        return job
    from applypilot.apply.chrome import detect_ats
    target = job.get("application_url") or job["url"]
    # Recognized ATS form endpoints already have a concrete browser operation.
    if detect_ats(target):
        return job
    from applypilot.integrations.crawl import CrawlService
    from applypilot.integrations.types import SourceError
    try:
        document = CrawlService("http").read(target)
    except Exception as exc:
        kind = exc.kind if isinstance(exc, SourceError) else "reader_unavailable"
        ops.record(component_name="applications", operation="email_route_preflight", severity="info",
                   outcome="deferred" if kind == "rate_limit" else "fallback",
                   details={"provider": "http", "reason_code": kind})
        return job
    # The HTTP reader removes navigation/footer content before producing Markdown.
    text = document.markdown
    def normalize(value):
        return " ".join(re.findall(r"\w+", str(value).casefold()))
    title = normalize(job.get("title"))
    if not title or title not in normalize(document.title + " " + text):
        return job
    candidates = {}
    unsupported_candidate = False
    for match in re.finditer(r"\[([^\]\n]{1,300})\]\((mailto:[^\s)]+)\)", text, re.I):
        paragraph = text.rfind("\n\n", 0, match.start())
        start = paragraph + 2 if paragraph >= 0 else 0
        end = text.find("\n\n", match.end())
        control = {"href": html.unescape(match[2]), "label": match[1],
                   "context": text[start:end if end >= 0 else len(text)][:1500]}
        headings = re.findall(r"^#{1,3}\s+(.+)$", text[:match.start()], re.M)
        primary = re.findall(r"^#\s+(.+)$", text, re.M)
        control.update(page_title=document.title, local_heading=headings[-1] if headings else "",
                       page_heading=primary[0] if len(primary) == 1 else "")
        if is_application_email(control) and email_link_matches_posting(control, job.get("title", "")):
            try:
                parsed = parse_email_target(control["href"])
            except ValueError:
                # Do not silently drop required CC/BCC or poison another route.
                unsupported_candidate = True
                continue
            candidates[parsed["target"]] = control
    if unsupported_candidate or len(candidates) != 1:
        return job
    href, control = next(iter(candidates.items()))
    evidence = {"source": "http-application-link", "posting_url": job["url"],
                "page_url": document.url, "observed_href": href, "label": control["label"]}
    if dry_run:
        return job | {"application_route": "email", "application_url": href, "route_evidence": json.dumps(evidence)}
    from applypilot.apply.guard import _current_job, attempt_snapshot, find_application_conflict, submission_identity
    from applypilot.apply.queue import list_actionable_jobs
    from applypilot.database import get_connection
    from applypilot.discovery.repository import transaction
    conn = get_connection()
    with transaction(conn):
        current = _current_job(conn, job["url"], job.get("_doc_format", "pdf"))
        if attempt_snapshot(current) != job.get("_attempt_snapshot"):
            return job  # The normal reservation guard will reselect changed work.
        if find_application_conflict(conn, job_url=job["url"], target=target, identity=submission_identity(job)):
            return job
        persist_route(conn, job["url"], ApplicationRoute("email", href, json.dumps(evidence), datetime.now(UTC).isoformat()))
    policy = job.get("_queue_policy") or {}
    selected = list_actionable_jobs(conn, job_url=job["url"], limit=1,
        min_score=policy.get("min_score"), max_score=policy.get("max_score"),
        max_age_days=policy.get("max_age_days"), route_kind="email", doc_format=job.get("_doc_format", "pdf"))
    ops.record(component_name="applications", operation="email_route_preflight", severity="info", outcome="email",
               details={"provider": "http", "reason_code": "explicit_application_email"})
    return selected[0] if selected else job

"""Explicitly approved HTML only: structured postings and real career links.

Embedded boards are returned as inventory leads, never fetched recursively.
No guessed APIs, sitemap crawling, fabricated titles, browser or solver fallback.
"""
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import replace
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from applypilot.discovery.ats_common import failure_page
from applypilot.discovery.models import Coverage, FailureKind, FetchFailure
from applypilot.discovery.careers.base import (
    CareerParser, jsonld_record, public_url, record, records_page, request, task_settings,
)


def _job_link(url, parser):
    path = urlsplit(url).path
    if parser == "bamboohr":
        return bool(re.fullmatch(r"/careers/\d+/?", path) or
                    path == "/jobs/view.php" and parse_qs(urlsplit(url).query).get("id"))
    if parser == "jazzhr":
        return bool(re.match(r"/apply/[^/]+(?:/|$)", path))
    return bool(re.search(r"/(?:jobs?|careers?|positions?|openings?|postings?)/[^/]+", path)
                or re.match(r"/apply/[^/]+", path))



def extract_feed(text, *, parser, tenant, company, url):
    """Extract public XML/RSS/Atom/sitemap surfaces without recursive IO."""
    if re.search(r"<!DOCTYPE|<!ENTITY", text, re.I):
        raise ValueError("External entities are not accepted in career feeds")
    root = ET.fromstring(text)
    tag = root.tag.rsplit("}", 1)[-1].lower()
    rows, leads = [], []
    if tag in {"urlset", "sitemapindex"}:
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1].lower() != "loc" or not (node.text or "").strip():
                continue
            link = (node.text or "").strip()
            try:
                public_url(link)
            except ValueError:
                continue
            if _job_link(link, parser):
                title = urlsplit(link).path.rstrip("/").split("/")[-1].replace("-", " ")
                rows.append(record({"source": "sitemap"}, provider=parser, tenant=tenant, company=company,
                                   title=title or "Job opening", url=link))
            else:
                leads.append({"provider": "generic", "career_url": link, "name": company,
                              "origins": [f"sitemap:{url}"]})
        return rows, [], leads, None, not rows and not leads
    if tag == "workzag-jobs":
        host = urlsplit(url).hostname or ""
        for position in list(root):
            fields = {child.tag.rsplit("}", 1)[-1]: (child.text or "").strip() for child in position}
            title, posting_id = fields.get("name"), fields.get("id")
            if not title or not posting_id:
                continue
            link = f"https://{host}/job/{posting_id}?display=en"
            rows.append(record(fields, provider=parser, tenant=tenant, company=company, title=title,
                               url=link, posting_id=posting_id, locations=fields.get("office")))
        return rows, [], leads, None, not rows
    if tag in {"rss", "feed", "jobs"}:
        for item in root.iter():
            kind = item.tag.rsplit("}", 1)[-1].lower()
            if kind not in {"item", "entry", "job", "position"}:
                continue
            fields = {}
            for child in list(item):
                key = child.tag.rsplit("}", 1)[-1].lower()
                value = (child.text or "").strip()
                if key == "link" and not value:
                    value = child.attrib.get("href", "")
                if value:
                    fields[key] = value
            title = fields.get("title") or fields.get("name")
            link = fields.get("link") or fields.get("url") or fields.get("applyurl")
            if not title or not link:
                continue
            try:
                public_url(urljoin(url, link))
            except ValueError:
                continue
            rows.append(record(fields, provider=parser, tenant=tenant, company=company, title=title,
                               url=urljoin(url, link), posting_id=fields.get("id") or fields.get("guid"),
                               locations=fields.get("location"),
                               description=fields.get("description") or fields.get("content")))
        return rows, [], leads, None, not rows
    raise ValueError("Unrecognized structured career feed")

def extract_html(html, *, parser, tenant, company, url):
    """Pure extraction; preserve malformed postings as page failures."""
    if not isinstance(html, str):
        raise ValueError("Career HTML response must be text")
    stripped = html.lstrip()
    if stripped.startswith("<?xml") or re.match(r"<(?:urlset|sitemapindex|rss|feed|jobs|workzag-jobs)(?:\s|>)", stripped, re.I):
        return extract_feed(html, parser=parser, tenant=tenant, company=company, url=url)
    soup = BeautifulSoup(html, "html.parser")
    postings, failures, leads = [], [], []
    seen = set()

    def walk(value, *, job_list=False):
        if isinstance(value, list):
            for item in value:
                walk(item, job_list=job_list)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = types if isinstance(types, list) else [types]
            if "JobPosting" in types or "EmploymentPosting" in types:
                postings.append((value, "jsonld"))
            elif job_list and (value.get("title") or value.get("jobOpeningName") or value.get("name")):
                postings.append((value, "embedded"))
            else:
                for key, child in value.items():
                    if isinstance(child, (dict, list)):
                        walk(child, job_list=key in {"jobs", "jobOpenings", "jobListings", "postings"})

    for script in soup.find_all("script"):
        kind = (script.get("type") or "").lower()
        if kind not in {"application/ld+json", "application/json"} and script.get("id") != "__NEXT_DATA__":
            continue
        try:
            walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError) as error:
            failures.append(FetchFailure(FailureKind.SCHEMA, f"Malformed embedded career JSON: {error}"))

    rows = []
    for raw, shape in postings:
        try:
            if shape == "jsonld":
                item = jsonld_record(raw, provider=parser, tenant=tenant, company=company, base_url=url)
                if not item["url"] and _job_link(url, parser) and len(postings) == 1:
                    item["url"] = item["application_url"] = url
            else:
                link = raw.get("url") or raw.get("jobUrl") or raw.get("apply_url")
                posting_id = raw.get("id") or raw.get("jobOpeningId")
                if not link and parser == "bamboohr" and posting_id is not None and str(posting_id).isdigit():
                    link = "/careers/" + str(posting_id)
                item = record(raw, provider=parser, tenant=tenant, company=company,
                              title=raw.get("title") or raw.get("jobOpeningName") or raw.get("name"),
                              url=urljoin(url, link) if link else None, posting_id=posting_id,
                              locations=raw.get("location") or raw.get("jobLocation"),
                              description=raw.get("description"), work_mode=raw.get("workplaceType"),
                              employment_type=raw.get("employmentType"), salary=raw.get("salary"))
            if item.get("url") not in seen or not item.get("url"):
                rows.append(item)
                seen.add(item.get("url"))
        except (ValueError, TypeError, AttributeError) as error:
            failures.append(FetchFailure(FailureKind.SCHEMA, str(error)))

    from applypilot.discovery.careers import detect_provider
    for node in soup.select("a[href], link[href], iframe[src]"):
        href = node.get("href") or node.get("src")
        link = urljoin(url, href)
        try:
            public_url(link)
        except ValueError:
            continue
        detected = detect_provider(link)
        path = urlsplit(link).path.lower()
        feed_like = path.endswith((".xml", ".rss", ".atom")) or "sitemap" in path or "feed" in path
        if feed_like and urlsplit(link).hostname == urlsplit(url).hostname:
            leads.append({"provider": detected if detected != "generic" else "generic",
                          "career_url": link, "name": company, "origins": [f"observed:{url}"]})
            continue
        if detected != "generic" and (detected != parser or urlsplit(link).hostname != urlsplit(url).hostname):
            leads.append({"provider": detected, "career_url": link, "name": company,
                          "origins": [f"observed:{url}"]})
            continue
        title = node.get_text(" ", strip=True)
        if (node.name != "a" or not title or link in seen or not _job_link(link, parser)
                or title.lower() in {"apply", "apply now", "view job", "learn more", "careers", "jobs", "next"}
                or urlsplit(link).hostname != urlsplit(url).hostname):
            continue
        seen.add(link)
        posting_id = None
        if parser == "bamboohr":
            posting_id = (parse_qs(urlsplit(link).query).get("id") or [urlsplit(link).path.rstrip("/").split("/")[-1]])[0]
        elif parser == "jazzhr":
            posting_id = urlsplit(link).path.split("/")[2]
        rows.append(record({"html": str(node), "href": href, "title": title},
                           provider=parser, tenant=tenant, company=company, title=title,
                           url=link, posting_id=posting_id))

    if parser == "generic" and urlsplit(url).path in {"", "/"}:
        leads.append({"provider": "generic", "career_url": urljoin(url, "/sitemap.xml"),
                      "name": company, "origins": [f"observed:{url}"]})

    next_links = {urljoin(url, node["href"]) for node in soup.select('a[rel~="next"][href], link[rel~="next"][href]')}
    if len(next_links) > 1:
        raise ValueError("Career page declares conflicting next links")
    next_url = next(iter(next_links), None)
    empty = bool(re.search(r"\b(?:no (?:current |open |available )?(?:jobs|positions|openings)|"
                           r"there are (?:currently )?no (?:open )?(?:positions|jobs)|"
                           r"not (?:currently )?hiring)\b", soup.get_text(" ", strip=True), re.I))
    if not rows and not empty and not leads and not failures:
        raise ValueError("Career page has no recognizable postings or explicit empty state")
    return rows, failures, leads, next_url, empty


def execute_html_task(task, context, cursor=None, *, parser="generic"):
    metadata = {"parser": parser, "cursor": cursor}
    try:
        params, tenant, approved = task_settings(task, context, parser)
        base = urlsplit(approved)
        if parser == "bamboohr":
            if (base.hostname != f"{tenant}.bamboohr.com" or
                    not (base.path.rstrip("/") in {"/careers", "/jobs"}
                         or _job_link(approved, parser))):
                raise PermissionError("BambooHR requires this tenant's public careers URL, not an employer API")
        elif parser == "jazzhr":
            domains = (f"{tenant}.applytojob.com", f"{tenant}.theresumator.com",
                       f"{tenant}.jazz.co", f"{tenant}.jazzhr.com")
            if (base.hostname not in domains and not
                    (base.hostname == "jobs.jazz.co" and base.path.strip("/") == tenant)):
                raise PermissionError("JazzHR requires this tenant's public career site, not an API-key endpoint")
            if re.search(r"/(?:api|v1|login|admin)(?:/|$)", base.path, re.I):
                raise PermissionError("JazzHR private API/admin paths are not public careers")
        if any(params[key] != approved for key in ("url", "career_url", "endpoint") if params.get(key)):
            raise ValueError("HTML task must name one exact approved URL")
        url, visited = approved, []
        if cursor is not None:
            state = json.loads(cursor)
            if not isinstance(state, dict) or state.get("request_key") != task.target.request_key():
                raise ValueError("Foreign career HTML cursor")
            url, visited = state.get("url"), state.get("visited")
            if not isinstance(visited, list) or not visited or visited[0] != approved or any(not isinstance(x, str) for x in visited):
                raise ValueError("Invalid career HTML cursor history")
        public_url(url)
        if url in visited or urlsplit(url).hostname != base.hostname or urlsplit(url).path != base.path:
            raise PermissionError("HTML pagination must remain on the approved listing path and advance")
        if parser == "generic" and url not in context.runtime.get("native_targets", {}).get("ats_registry", ()):
            raise PermissionError("Generic continuation URL requires exact native URL approval")
        metadata.update(url=url, tenant=tenant, api_region="global", contract="observed public careers HTML")
        html = request(context, url, json_response=False)
        rows, failures, leads, next_url, empty = extract_html(
            html, parser=parser, tenant=tenant, company=params.get("name") or params.get("company") or tenant, url=url)
        next_cursor = None
        if next_url:
            public_url(next_url)
            if next_url in [*visited, url] or urlsplit(next_url).hostname != base.hostname or urlsplit(next_url).path != base.path:
                failures.append(FetchFailure(FailureKind.SCHEMA, "Unsafe or repeated HTML next link"))
            else:
                next_cursor = json.dumps({"request_key": task.target.request_key(), "url": next_url,
                                          "visited": [*visited, url]}, sort_keys=True)
        page = records_page(task, context, rows, lambda row: row, next_cursor=next_cursor,
                            authoritative=False, metadata={**metadata, "board_leads": leads, "explicit_empty": empty})
        if failures:
            return replace(page, failures=(*page.failures, *failures), complete=False, exhausted=False,
                           partial=bool(page.records), coverage=Coverage.PARTIAL if page.records else Coverage.UNKNOWN)
        # Parsed HTML is not proof of a complete authoritative employer inventory.
        return replace(page, coverage=Coverage.PARTIAL if rows or leads else Coverage.EMPTY)
    except Exception as error:
        return failure_page(task, error, metadata=metadata)


def execute_task(task, context, cursor=None):
    return execute_html_task(task, context, cursor, parser="generic")


class GenericCareerParser(CareerParser):
    provider = "generic"


def enrich_job_descriptions(jobs, max_pages=50, *, context=None):
    """Optional explicit detail work, using the same approval and HTTP budgets."""
    from applypilot.discovery.ats_adapter import fetch_company_jobs
    if context is None:
        raise ValueError("Description enrichment requires an explicit discovery context")
    for job in jobs[:max_pages]:
        if job.description or job.evidence.get("description"):
            continue
        rows = fetch_company_jobs(None, "detail", "generic", job.url, context=context)
        matched = next((row for row in rows if row["url"] == job.url), None)
        if matched and matched.get("description"):
            job.description = matched["description"]
            job.evidence.update(matched)
    return jobs

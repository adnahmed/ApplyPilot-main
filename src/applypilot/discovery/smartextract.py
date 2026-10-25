"""Extract observed structured postings from one authorized recovery snapshot.

No independent discovery universe, hidden location policy, stealth browser,
protection solver, or model-generated application URL.
"""

import json
import re
from urllib.parse import urlsplit


def extract_json(text: str):
    """Decode JSON/JSON fences for the existing enrichment consumer."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("model_empty_response: provider returned no usable JSON text")
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    match = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    content = (match[1] if match else text).strip()
    if not content:
        raise ValueError("model_empty_response: no answer after reasoning/fence removal")
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("model_invalid_json: response did not contain valid JSON") from exc


def extract_observation(payload, context):
    from .normalization import country_code, scalar_text

    records, seen = [], set()
    nodes = []
    for script in payload.get("json_ld", ()):
        try:
            value = json.loads(script)
        except (ValueError, TypeError):
            continue
        nodes.extend(value if isinstance(value, list) else [value])
    while nodes:
        item = nodes.pop(0)
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("@graph"), list):
            nodes.extend(item["@graph"])
        types = item.get("@type", [])
        if "JobPosting" not in (types if isinstance(types, list) else [types]):
            continue
        url = scalar_text(item.get("url"))
        identifier = item.get("identifier")
        identifier = identifier.get("value") if isinstance(identifier, dict) else identifier
        if not url and not identifier:
            continue
        if url and (urlsplit(url).scheme not in {"http", "https"} or urlsplit(url).username):
            continue
        organization = item.get("hiringOrganization")
        organization = organization if isinstance(organization, dict) else {}
        offices = item.get("jobLocation")
        offices = offices if isinstance(offices, list) else [offices] if offices else []
        names = []
        for office in offices:
            if isinstance(office, dict):
                address = office.get("address") or {}
                if isinstance(address, dict):
                    names.append(", ".join(str(address[key]) for key in
                        ("addressLocality", "addressRegion", "addressCountry") if address.get(key)))
        record = {"url": url, "application_url": url, "source_posting_id": scalar_text(identifier),
                  "title": scalar_text(item.get("title")), "company": scalar_text(organization.get("name")),
                  "description": scalar_text(item.get("description")), "raw_record": item,
                  "office_locations": [name for name in names if name],
                  "posted_at": scalar_text(item.get("datePosted")), "salary": item.get("baseSalary")}
        if item.get("jobLocationType") == "TELECOMMUTE":
            record["work_mode"] = "remote"
        requirements = item.get("applicantLocationRequirements")
        requirements = requirements if isinstance(requirements, list) else [requirements] if requirements else []
        codes = [country_code(value.get("name")) for value in requirements if isinstance(value, dict)]
        if requirements and len(codes) == len(requirements) and all(codes):
            record["work_arrangements"] = [{"id": "schema-role", "countries": codes,
                "eligible_scope": "restricted", "work_mode": record.get("work_mode", "unknown"),
                "office_locations": record["office_locations"]}]
        key = url or scalar_text(identifier)
        if key not in seen and record["title"]:
            seen.add(key)
            records.append(record)
    # Observed role links remain unresolved candidates. Never apply nearby
    # company/location/Easy Apply labels to unrelated links on the page.
    for link in payload.get("links", ()):
        url, title = link.get("url"), link.get("title")
        if (not isinstance(url, str) or not isinstance(title, str) or not title.strip()
                or len(title) > 300 or url in seen or urlsplit(url).scheme != "https"
                or urlsplit(url).username):
            continue
        if not any(word in title.casefold() for q in context.intent.active_queries()
                   for word in re.findall(r"[a-z0-9+#.]+", q.query.casefold()) if len(word) > 3):
            continue
        seen.add(url)
        records.append({"url": url, "title": title, "raw_record": {"observed_link": link,
                        "source_page": payload["url"]}})
    return records


def execute_task(task, context, cursor=None):
    from .recovery import execute_task as recover
    return recover(task, context, cursor, executor="smartextract")


def run_smart_extract(*, task=None, context=None, cursor=None, workers=1):
    """Finite exact-task compatibility seam; batch recovery migration is unfinished."""
    if task is None or context is None:
        raise ValueError('The local smartextract recovery scheduler is retired. '
                         'Supply one authorized task and its context to execute_task; '
                         'native recovery traversal remains part of the unfinished Part 2 scope.')
    if workers != 1:
        raise ValueError('Configure concurrency on the Temporal worker deployment')
    return execute_task(task, context, cursor)

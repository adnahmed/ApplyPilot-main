"""Bounded reads of published company catalogs; never import them as job postings."""
from __future__ import annotations

import csv
import io
import json
import re

from applypilot.integrations.net import fetch, public_url
from applypilot.integrations.types import Document, SourceError, now_iso

_JOBSEEK = "https://raw.githubusercontent.com/colophon-group/jobseek/main/apps/crawler/data/"
_STARTUPS = (
    "https://raw.githubusercontent.com/yigitmeteozcan/startups/main/data/",
    "https://cdn.jsdelivr.net/gh/yigitmeteozcan/startups@main/data/",
)


def _url(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return public_url(value.strip(), resolve=False)
    except SourceError:
        return None


def _csv(url, required, before_request):
    _, body, _ = fetch(url, max_bytes=5_000_000, before_request=before_request)
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")))
    if not required.issubset(reader.fieldnames or ()):
        raise SourceError("Company catalog CSV columns changed", "schema")
    return list(reader)


def load(source: dict, *, before_request, limit=8) -> tuple[Document, int | None] | None:
    """Return a company-evidence batch and its next catalog offset.

    The caller persists the offset only after processing the batch, and still
    verifies the original company/portfolio before promoting a watch target.
    Unknown URLs belong to the ordinary page crawler, not this catalog reader.
    """
    url = source["url"]
    jobseek = url == _JOBSEEK + "companies.csv"
    startups = any(url.startswith(base) and re.fullmatch(
        r"(?:current|by-year/[0-9]{4})\.json", url[len(base):]) for base in _STARTUPS)
    if not jobseek and not startups:
        return None
    if type(limit) is not int or not 1 <= limit <= 25:
        raise ValueError("Company catalog batch size must be in 1..25")
    evidence = source.get("evidence_json") or "{}"
    evidence = json.loads(evidence) if isinstance(evidence, str) else evidence
    offset = evidence.get("catalog_offset", 0) or 0
    if type(offset) is not int or offset < 0:
        raise SourceError("Invalid company catalog checkpoint", "schema")
    if jobseek:
        rows = _csv(url, {"slug", "name", "website"}, before_request)
    else:
        _, body, _ = fetch(url, max_bytes=5_000_000, before_request=before_request)
        rows = json.loads(body)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not row.get("name") for row in rows):
            raise SourceError("Startup Portfolios response must contain named company records", "schema")
    # A refreshed catalog can shrink. Restart its slice instead of getting stuck
    # permanently beyond the end; downstream company identity remains stable.
    if offset >= len(rows):
        offset = 0
    selected = rows[offset:offset + limit]
    next_offset = offset + len(selected) if offset + len(selected) < len(rows) else None
    links = []
    records = []
    boards = {}
    if jobseek and selected:
        slugs = {row["slug"] for row in selected}
        from applypilot.discovery.jobseek import normalize_board
        for board in _csv(_JOBSEEK + "boards.csv", {"company_slug", "board_slug", "board_url", "monitor_type",
                                                   "monitor_config", "scraper_type", "scraper_config"}, before_request):
            if board["company_slug"] in slugs and (board_url := _url(board["board_url"])):
                boards.setdefault(board["company_slug"], []).append(normalize_board({**board, "board_url": board_url}))
    for row in selected:
        website = _url(row.get("website"))
        if website:
            links.append(website)
        if jobseek:
            record = {key: row.get(key) for key in ("slug", "name", "founded_year")}
            record.update(website=website, industry_code=row.get("industry"),
                employee_count_range_code=row.get("employee_count_range"),
                boards=boards.get(row["slug"], []))
            links.extend(board["board_url"] for board in record["boards"])
        else:
            record = {key: value for key, value in row.items() if key != "logo"}
            record["website"] = website
            social = row.get("social")
            if isinstance(social, dict):
                links.extend(link for value in social.values() if (link := _url(value)))
        records.append(record)
    label = "Jobseek company and career-board catalog" if jobseek else "Startup Portfolios accelerator catalog"
    text = (f"{label}\nPublished dataset: {url}\nRetrieved: {now_iso()}\n"
            "Secondary company evidence. Catalog membership does not establish current hiring, "
            "a completed funding round, a cohort admission date, or applicant eligibility.\n"
            f"Catalog records {offset + 1 if records else 0}–{offset + len(records)} of {len(rows)}.\n")
    if jobseek:
        text += f"Career-board records joined by company_slug from {_JOBSEEK}boards.csv. "
        text += "Industry and employee-count values are publisher codes, not decoded facts.\n"
    # Keep quoted names and multiline descriptions verbatim for the caller's
    # exact-excerpt checks; JSON escaping belongs only in structured_data.
    for record in records:
        text += "\n"
        for key, value in record.items():
            if isinstance(value, str):
                rendered = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                rendered = ", ".join(value)
            else:
                rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            text += f"{key}: {rendered}\n"
    return Document(url=url, title=label, markdown=text, links=tuple(dict.fromkeys(links)),
        provider="company_catalog", structured_data=tuple(records)), next_offset


def attach_boards(value, document):
    """Join original catalog configurations without trusting model-rewritten settings."""
    website = _url(value.get("website"))
    careers = _url(value.get("careers_url"))
    matched = [record for record in document.structured_data if record.get("boards") and (
        website and _url(record.get("website")) == website or
        careers and any(board["board_url"] == careers for board in record["boards"]))]
    if len(matched) == 1:
        value["jobseek_boards"] = matched[0]["boards"]
    return value

"""Greenhouse posting normalization and one-page discovery adapter."""

from __future__ import annotations

import re
from copy import deepcopy
from html import unescape
from html.parser import HTMLParser

from applypilot.discovery.models import DiscoveryPage, SearchTask


class _HTMLStripper(HTMLParser):
    """Strip HTML tags, preserve text content and line breaks."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "li", "div", "tr", "h1", "h2", "h3", "h4"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        # Collapse whitespace
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _strip_html(html: str) -> str:
    if not html:
        return ""
    s = _HTMLStripper()
    try:
        s.feed(html)
    except Exception:
        return html  # fallback: return raw
    return s.text()


def parse_record(raw: dict, employer: dict | str) -> dict:
    """Preserve posting identity and office evidence, never infer eligibility."""
    emp = employer if isinstance(employer, dict) else {"name": employer}
    description = _strip_html(unescape(raw.get("content") or ""))
    location = (raw.get("location") or {}).get("name")
    return {
        "raw_record": deepcopy(raw),
        "title": raw.get("title"), "company": emp.get("name"),
        "employer_name": emp.get("name"), "employer_slug": emp.get("slug"),
        "description": description or None, "full_description": description or None,
        "url": raw.get("absolute_url"), "application_url": raw.get("absolute_url"),
        "source_posting_id": raw.get("id"),
        "source_internal_id": raw.get("internal_job_id"),
        "source_requisition_id": raw.get("requisition_id"),
        "location": location,
        "office_locations": deepcopy(raw.get("offices") or ([raw["location"]] if location else [])),
        "departments": deepcopy(raw.get("departments")),
        "work_mode": raw.get("workplace_type"),
        "compensation": deepcopy(raw.get("pay_input_ranges")),
        "posted_at": raw.get("first_published"), "updated_at": raw.get("updated_at"),
    }


def execute_task(task: SearchTask, context, cursor=None) -> DiscoveryPage:
    from applypilot.discovery.ats_common import execute_board_task
    return execute_board_task(task, context, cursor, provider="greenhouse",
                              parser=parse_record)

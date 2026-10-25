"""HiringCafe public, server-rendered search pages, one native page per task.

The old hiring.cafe POST search API is retired. Public search results now live
on hiringcafe.com and expose their pagination in __NEXT_DATA__. Company and
role classifications are the aggregator's claims, not ghost-job clearance.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from . import transport
from .builtin import _failure_page, _finish_page, _page_state, _query
from .countries import COUNTRY_CODES, COUNTRY_NAMES
from .normalization import scalar_text

_BASE = "https://hiringcafe.com"


def _request(task, cursor):
    params = transport.task_params(task, ("hiring_cafe",))
    if set(params) - {"query", "country", "work_modes"}:
        raise ValueError("Unsupported HiringCafe request parameters")
    country = params.get("country")
    if country not in COUNTRY_CODES and country != "global":
        raise ValueError("HiringCafe requires a country or global search target")
    modes = params.get("work_modes", ())
    if not isinstance(modes, (list, tuple)) or set(modes) - {"remote", "hybrid", "onsite"}:
        raise ValueError("Invalid HiringCafe workplace types")
    state = {"searchQuery": _query(params.get("query")), "locations": [],
             "defaultToUserLocation": False}
    if country != "global":
        name = next(name for name, code in COUNTRY_NAMES.items() if code == country).title()
        state["locations"] = [{"formatted_address": name, "types": ["country"],
            "id": country, "address_components": [{"long_name": name,
                "short_name": country, "types": ["country"]}],
            "options": {"flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]}}]
    if modes:
        state["workplaceTypes"] = [mode.title() for mode in modes]
    page, previous = _page_state(task, cursor, start=0)
    url = _BASE + "/?" + urlencode({"searchState": json.dumps(state, separators=(",", ":")), "page": page})
    return url, state, page, previous


def normalize_job(raw):
    info = raw.get("job_information") or {}
    details = raw.get("v5_processed_job_data") or {}
    company = raw.get("enriched_company_data") or raw.get("v5_processed_company_data") or {}
    if any(not isinstance(value, dict) for value in (info, details, company)):
        raise ValueError("Malformed HiringCafe job metadata")
    posting_id = scalar_text(raw.get("id") or raw.get("objectID"))
    title = scalar_text(info.get("title") or info.get("job_title_raw"))
    if not posting_id or not title:
        raise ValueError("HiringCafe job has no posting identity or title")
    application = scalar_text(raw.get("apply_url"))
    from applypilot.apply.routes import normalize_public_url
    application = normalize_public_url(application)
    listing = _BASE + "/?" + urlencode({"job": posting_id})
    description = info.get("description") or details.get("requirements_summary")
    mode = str(details.get("workplace_type") or "").lower()
    commitments = details.get("commitment") or []
    return {
        "provider": "hiring_cafe", "source": "hiring_cafe", "posting_id": posting_id,
        "source_posting_id": posting_id, "url": application or listing,
        "listing_url": listing, "application_url": application, "title": title,
        "company": scalar_text(details.get("company_name") or company.get("name")),
        "company_url": scalar_text(details.get("company_website") or company.get("homepage_uri")),
        "description": BeautifulSoup(description or "", "html.parser").get_text(" ", strip=True),
        "location": scalar_text(details.get("formatted_workplace_location")),
        "work_mode": mode if mode in {"remote", "hybrid", "onsite"} else None,
        "schedule": commitments[0].lower().replace(" ", "_") if isinstance(commitments, list)
                    and len(commitments) == 1 and isinstance(commitments[0], str) else None,
        "office_locations": details.get("workplace_cities") or [],
        "office_countries": details.get("workplace_countries") or [],
        "raw_record": raw,
    }


def page_from_data(task, context, cursor, data):
    url, expected_state, position, previous = _request(task, cursor)
    if not isinstance(data, dict) or data.get("ssrError"):
        raise ValueError("HiringCafe search failed to render")
    state = data.get("initialSearchState")
    if not isinstance(state, dict) or any(state.get(key) != value for key, value in expected_state.items()):
        raise ValueError("HiringCafe changed the planned search filters")
    if type(data.get("ssrPage")) is not int or data["ssrPage"] != position:
        raise ValueError("HiringCafe returned a different search page")
    rows = data.get("ssrHits")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("HiringCafe search has no job list")
    if type(data.get("ssrIsLastPage")) is not bool:
        raise ValueError("HiringCafe search omitted its continuation state")
    records = [normalize_job(row) for row in rows if row.get("is_expired") is not True]
    # Pagination advances by company groups, not the number of job hits.
    next_page = None if data["ssrIsLastPage"] else position + 1
    result = _finish_page(task, context, records, position, next_page, previous, -1,
        url=url, method="GET", total=data.get("ssrTotalCount"),
        native_page_size=data.get("ssrPageSize"))
    return result


def data_from_html(html):
    node = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
    if node is None:
        raise ValueError("HiringCafe response omitted public search data")
    return json.loads(node.get_text())["props"]["pageProps"]


def execute_task(task, context, cursor=None):
    metadata = {"method": "GET"}
    try:
        url, _, _, _ = _request(task, cursor)
        metadata["url"] = url
        if context.runtime["authorized_sources"].get("hiring_cafe") is False:
            raise transport.TaskDeferred("HiringCafe access explicitly disabled", transport.FailureKind.ACCESS_DENIED)
        transport.reserve_budget(context, "http")
        result = transport.fetch_with_transport(url, timeout=30)
        if isinstance(result, transport.TransportError):
            raise transport.TaskDeferred(result.error, result.kind, http_status=result.http_status,
                                         retry_after=result.retry_after)
        return page_from_data(task, context, cursor, data_from_html(result.html))
    except (transport.TaskDeferred, ValueError, TypeError, KeyError, OSError) as exc:
        return _failure_page(task, context, exc, cursor, **metadata)


async def read_browser_data(url):
    """Read the same public search in an owned recovery tab without model extraction."""
    from applypilot.unattended.browser import BrowserOS
    async with BrowserOS() as browser:
        page = await browser.page(url)
        try:
            header = await browser.evaluate(page, """
const node = document.getElementById('__NEXT_DATA__');
const data = node ? JSON.parse(node.textContent).props.pageProps : {};
return {url:location.href,text:document.body?.innerText?.slice(0,10000)||'',
  initialSearchState:data.initialSearchState,ssrPage:data.ssrPage,
  ssrIsLastPage:data.ssrIsLastPage,ssrError:data.ssrError,
  ssrTotalCount:data.ssrTotalCount,ssrPageSize:data.ssrPageSize,
  count:Array.isArray(data.ssrHits)?data.ssrHits.length:null};
""")
            if header.get("url") != url:
                raise transport.TaskDeferred("HiringCafe redirected outside the planned search", transport.FailureKind.ACCESS_DENIED)
            if transport.access_blocked(header.get("text", ""), url):
                return header
            count = header.get("count")
            if type(count) is not int or not 0 <= count <= 1000:
                raise ValueError("HiringCafe returned an invalid public page size")
            rows = []
            for offset in range(0, count, 10):
                part = await browser.evaluate(page,
                    "const data=JSON.parse(document.getElementById('__NEXT_DATA__').textContent).props.pageProps;"
                    f"return {{rows:data.ssrHits.slice({offset},{offset + 10})}};")
                if not isinstance(part.get("rows"), list):
                    raise ValueError("HiringCafe browser returned incomplete search data")
                rows.extend(part["rows"])
            return {**header, "ssrHits": rows}
        finally:
            await browser.release_page(page)

"""Decode linkedin-rs search clusters without treating their wrappers as posts."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .types import SourceError


def _at(value, *keys):
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _text(value):
    for _ in range(3):
        if isinstance(value, str):
            return value
        value = value.get("text") if isinstance(value, dict) else None
    return None


def _post(value):
    if not isinstance(value, dict):
        return value
    update = value.get("update", value)
    if not isinstance(update, dict):
        return None
    identity = None
    for candidate in (_at(update, "metadata", "backendUrn"), update.get("entityUrn"),
                      _at(update, "socialDetail", "entityUrn")):
        if isinstance(candidate, str):
            match = re.search(r"urn:li:activity:(\d+)(?=$|[,\s)])", candidate)
            if match:
                identity = match[1]
                break
    url = ("https://www.linkedin.com/feed/update/urn:li:activity:" + identity
           if identity else value.get("permalink"))
    actor = update.get("actor", {})
    author = {"name": _text(_at(actor, "name"))}
    profile = _at(actor, "navigationContext", "actionTarget") or _at(actor, "navigationUrl")
    if isinstance(profile, str):
        try:
            parts = urlsplit(profile)
            if (parts.scheme == "https" and parts.hostname in {"linkedin.com", "www.linkedin.com"}
                    and not parts.username and not parts.password and parts.path.startswith("/in/")):
                author["profile_url"] = profile
        except ValueError:
            pass
    return {"id": identity, "url": url, "text": _text(update.get("commentary")),
            "author": author}


def search_page(data, *, start):
    """Return actual post candidates and the provider's next result offset."""
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise SourceError("LinkedIn native search omitted its cluster collection", "schema")
    rows, ignored = [], 0
    for cluster in data["elements"]:
        items = cluster.get("items") if isinstance(cluster, dict) else None
        if not isinstance(items, list):
            ignored += 1
            continue
        for wrapper in items:
            item = wrapper.get("item") if isinstance(wrapper, dict) else None
            if not isinstance(item, dict) or "searchFeedUpdate" not in item:
                ignored += 1
                continue
            rows.append(_post(item["searchFeedUpdate"]))
    diagnostics = {"cluster_count": len(data["elements"]), "raw_post_count": len(rows),
                   "non_post_items": ignored}
    paging = data.get("paging")
    if not isinstance(paging, dict):
        return rows, None, False, diagnostics
    offset, count, total = paging.get("start"), paging.get("count"), paging.get("total")
    if type(offset) is not int or offset != start or type(count) is not int or count < 0:
        return rows, None, False, diagnostics
    diagnostics.update(page_start=offset, page_count=count)
    if total is not None and (type(total) is not int or total < 0):
        return rows, None, False, diagnostics
    next_offset = offset + count
    if total is not None:
        diagnostics["total_results"] = total
    finished = count == 0 or total is not None and next_offset >= total
    return rows, None if finished else str(next_offset), True, diagnostics

"""Exact professional identities, not fuzzy name/company matching."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit


def profile_key(value: str | None) -> str | None:
    if not value:
        return None
    from .store import public_url
    p = urlsplit(public_url(value))
    host = (p.hostname or '').lower().removeprefix('www.')
    path = unquote(p.path).rstrip('/')
    if host == 'linkedin.com' or host.endswith('.linkedin.com'):
        if not re.fullmatch(r'/in/[^/?#\s]+', path):
            raise ValueError('LinkedIn contact identity requires an /in/ profile, not a company or search page')
        return 'linkedin:' + path[4:].casefold()
    if host in {'x.com', 'twitter.com'}:
        if not re.fullmatch(r'/[A-Za-z0-9_]{1,15}', path) or path[1:].lower() in {
            'home', 'search', 'explore', 'intent', 'settings', 'messages', 'i'}:
            raise ValueError('X contact identity requires a profile, not a post or search page')
        return 'x:' + path[1:].casefold()
    # Unknown professional sites retain path/query identity; do not merge by name.
    return 'url:' + urlunsplit(('https', host, p.path.rstrip('/'), p.query, ''))

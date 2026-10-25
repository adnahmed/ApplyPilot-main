"""Operation-aware selection of existing, explicitly authorized social readers.

Availability never grants access. Provider ordering and explicit source modes
come from the existing discovery policy; BrowserOS remains a lazy fallback.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from urllib.parse import quote, urlsplit

from applypilot import config, operations as ops
from applypilot.referrals.identity import profile_key
from .social import PROVIDER_ORDER, READ_OPERATIONS, SocialService, available_readers
from .types import SourceError


@dataclass(frozen=True)
class ReadRoute:
    operation: str
    provider: str | None
    reason: str
    inputs: dict
    providers: tuple[str, ...] = ()


def resolve_read(channel, operation, cfg, *, runtime=None):
    """Inspect configuration only. No session use, tool invocation or new grant."""
    runtime = config.load_discovery_runtime() if runtime is None else runtime
    platform = {"linkedin": "linkedin_posts", "x": "twitter"}.get(channel)
    inputs = dict(runtime.get("source_inputs", {}).get(platform, {}))
    if channel not in cfg.get("channels", ()):
        return ReadRoute(operation, None, "channel_disabled", inputs)
    if runtime.get("authorized_sources", {}).get(platform) is not True:
        return ReadRoute(operation, None, "reader_not_authorized", inputs)
    if inputs.get("limit") == 0:
        return ReadRoute(operation, None, "reader_paused", inputs)
    mode = inputs.get("mode", "auto")
    if inputs.get("fallback", "auto") not in {"auto", "none"}:
        raise SourceError("Reader fallback must be auto or none", "configuration")
    if mode == "export":
        return ReadRoute(operation, None, "explicit_export", inputs)
    if mode == "auto" and (inputs.get("session_file") or inputs.get("session_env")):
        raise SourceError("Select an explicit reader for a custom session", "configuration")
    if mode != "auto":
        if mode not in PROVIDER_ORDER.get(platform, ()):
            raise SourceError("Unknown explicitly selected social reader", "configuration")
        if operation not in READ_OPERATIONS[mode]:
            if inputs.get("fallback") == "none":
                raise SourceError("Pinned reader does not support the operation", "unsupported")
            readers = ([] if inputs.get("session_file") or inputs.get("session_env") else
                       available_readers(platform, inputs, operation=operation))
            return ReadRoute(operation, readers[0] if readers else None,
                             "preferred_reader_unsupported", inputs, tuple(readers))
        # Explicit mode is preferred, not exclusive. A custom session remains
        # pinned to its provider; browser fallback uses its independently verified
        # owner account. fallback=none opts into exclusive operation.
        custom = bool(inputs.get("session_file") or inputs.get("session_env"))
        extra = ([] if custom or inputs.get("fallback") == "none" else
                 available_readers(platform, inputs, operation=operation))
        return ReadRoute(operation, mode, "preferred_reader", inputs,
                         tuple(dict.fromkeys([mode, *extra])))
    readers = available_readers(platform, inputs, operation=operation)
    return ReadRoute(operation, readers[0] if readers else None,
                     "authorized_supported_reader" if readers else "no_supported_reader", inputs, tuple(readers))


def record_route(route, outcome, *, reason=None):
    ops.record(component_name="integrations", operation=route.operation,
               outcome=outcome, severity="info", details={
                   "provider": route.provider or "browseros", "reason_code": reason or route.reason})


async def _finish_read(function, *args, **kwargs):
    """Keep the owning permit until a finite connector thread actually exits."""
    pending = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancelling to_thread does not stop its process or MCP session. Drain
        # its existing bounded timeout before the owner releases browser/work
        # capacity, including when shutdown cancels the owner more than once.
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not pending.cancelled():
            pending.exception()
        raise


def _profile(data, requested_url):
    """Use explicit returned identity and observed content; never synthesize facts."""
    if not isinstance(data, dict):
        raise SourceError("Profile response has no structured identity", "unsupported")
    row = data
    for key in ("profile", "data", "person"):
        if isinstance(row.get(key), dict):
            row = row[key]
    url = row.get("profile_url") or row.get("profileUrl") or row.get("linkedin_url")
    slug = row.get("publicIdentifier")
    handle = row.get("screen_name") or row.get("username")
    expected = profile_key(requested_url)
    if not url and slug and expected.startswith("linkedin:"):
        url = "https://www.linkedin.com/in/" + str(slug)
    if not url and handle and expected.startswith("x:"):
        url = "https://x.com/" + str(handle)
    if not url:
        candidate = row.get("url")
        if candidate:
            observed = profile_key(candidate)
            if observed and observed.split(":", 1)[0] == expected.split(":", 1)[0]:
                url = candidate
    if not url:
        raise SourceError("Profile response omitted its platform identity", "unsupported")
    if profile_key(url) != expected:
        raise SourceError("Reader returned a different profile identity", "account_mismatch")
    name = row.get("name") or row.get("full_name") or " ".join(
        str(row.get(k) or "") for k in ("firstName", "lastName")).strip()
    if not isinstance(name, str) or not name:
        raise SourceError("Profile response omitted the observed name", "unsupported")
    text = row.get("text") or row.get("markdown") or json.dumps(row, ensure_ascii=False)
    if not isinstance(text, str) or len(text) > 150000:
        raise SourceError("Profile response has unsupported content", "unsupported")
    return {"url": url, "name": name, "text": text, "links": []}


def _private(data, account_url, *, recipient_url=None):
    """Accept only a proven complete private envelope, not bare/aggregate rows.

    Unknown provider schemas remain unsupported. A contradictory observed
    account or participant is an identity failure and cannot fall back.
    """
    if not isinstance(data, dict):
        raise SourceError("Private reader omitted its identity envelope", "unsupported")
    account = data.get("account_url")
    if account and profile_key(account) != profile_key(account_url):
        raise SourceError("Private reader account changed", "account_mismatch")
    recipient = data.get("recipient_url")
    if recipient_url and recipient and profile_key(recipient) != profile_key(recipient_url):
        raise SourceError("Private reader recipient changed", "account_mismatch")
    if not account or data.get("read_complete") is not True:
        raise SourceError("Private reader omitted account/completeness proof", "unsupported")
    if recipient_url:
        if not recipient or not isinstance(data.get("messages"), list) or not data.get("url"):
            raise SourceError("Conversation reader omitted recipient/thread proof", "unsupported")
        parsed = urlsplit(data["url"])
        if (parsed.scheme != "https" or parsed.hostname not in {"linkedin.com", "www.linkedin.com"}
                or not parsed.path.startswith("/messaging/thread/") or parsed.username or parsed.password):
            raise SourceError("Conversation reader returned an invalid thread identity", "account_mismatch")
        participants = data.get("participants")
        if not isinstance(participants, list) or len(participants) != 2:
            raise SourceError("Conversation reader omitted direct participant identities", "unsupported")
        if {profile_key(url) for url in participants} != {profile_key(account_url), profile_key(recipient_url)}:
            raise SourceError("Conversation participants changed", "account_mismatch")
        if data.get("message_order") != "oldest_first":
            raise SourceError("Conversation reader omitted message ordering proof", "unsupported")
        ids = set()
        for message in data["messages"]:
            if (not isinstance(message, dict) or not isinstance(message.get("id"), str)
                    or not message["id"] or message["id"] in ids
                    or message.get("direction") not in {"in", "out"}
                    or not isinstance(message.get("text"), str)):
                raise SourceError("Conversation reader omitted ordered message identities", "unsupported")
            ids.add(message["id"])
        return dict(data, verified=True)
    return data


class ResearchReader:
    def __init__(self, cfg, *, browser=None, browser_factory=None, runtime=None):
        self.cfg, self.browser, self.browser_factory = cfg, browser, browser_factory
        self.runtime = config.load_discovery_runtime() if runtime is None else runtime
        self.failures = []

    async def get_browser(self):
        if self.browser is None and self.browser_factory is None:
            return None
        if self.browser is None:
            self.browser = await self.browser_factory()
        return self.browser

    async def native(self, channel, operation, *, query=None, identifier=None, limit=20, recipient_url=None, start=0):
        from temporalio import activity
        from .fallback import reader_error
        route = resolve_read(channel, operation, self.cfg, runtime=self.runtime)
        if route.provider is None:
            if route.inputs.get("fallback") == "none" and route.reason not in {"channel_disabled", "reader_not_authorized", "reader_paused", "explicit_export"}:
                raise SourceError("Exclusive reader is unavailable", "unsupported")
            record_route(route, "fallback")
            return None
        providers = route.providers or (route.provider,)
        if (route.inputs.get("fallback") != "none"
                and (self.browser is not None or self.browser_factory is not None)):
            providers = (*providers, None)
        attempt = activity.info().attempt if activity.in_activity() else 1
        provider = providers[(attempt - 1) % len(providers)]
        selected = ReadRoute(operation, provider, route.reason, route.inputs)
        if provider is None:
            record_route(selected, "fallback")
            return None
        record_route(selected, "selected")
        try:
            service = SocialService(provider, inputs=route.inputs | {"start": start})
            value = identifier
            if operation == "profile" and provider != "linkedin_scraper":
                value = profile_key(identifier).split(":", 1)[1]
            elif operation == "profile" and provider == "linkedin_scraper":
                identity = profile_key(identifier)
                if not identity or not identity.startswith('linkedin:'):
                    raise SourceError('LinkedIn reader requires a LinkedIn profile identity', 'account_mismatch')
                value = 'https://www.linkedin.com/in/' + quote(identity.split(':', 1)[1], safe='-._~')
            data = await _finish_read(service.read, operation, query=query, identifier=value,
                                      limit=limit, authorized=True, fresh=True)
            if isinstance(data, dict) and (data.get("error") or data.get("ok") is False or data.get("success") is False):
                raise reader_error(data)
            if operation == "profile":
                data = _profile(data, identifier)
            elif operation in {"connections", "inbox", "conversation"}:
                data = _private(data, self.cfg["accounts"][channel], recipient_url=recipient_url)
            record_route(selected, "success")
            return data, provider
        except Exception as exc:
            from applypilot.unattended.failures import record
            record(self.failures, exc, channel=channel, operation=operation + ':' + provider)
            record_route(selected, "failed")
            raise

    async def connections(self, *, page_number=0, limit=30):
        if 'linkedin' not in self.cfg.get('channels', ()):
            raise SourceError("LinkedIn channel is not authorized", "access_denied")
        value = await self.native("linkedin", "connections", limit=limit, start=page_number * limit)
        if value is not None:
            return value
        browser = await self.get_browser()
        if browser is None:
            raise SourceError("No supported network reader", "unsupported")
        return await browser.connections(page_number=page_number, limit=limit), "browseros:linkedin"

    async def conversation(self, profile_url, channel, account_url, *, thread_url=None):
        if channel not in self.cfg.get('channels', ()) or account_url != self.cfg.get('accounts', {}).get(channel):
            raise SourceError("Conversation channel/account is not authorized", "access_denied")
        # Only a previously observed provider thread identifier may be reused.
        if thread_url and channel == "linkedin":
            parsed = urlsplit(thread_url)
            if parsed.hostname in {"linkedin.com", "www.linkedin.com"} and parsed.path.startswith("/messaging/thread/"):
                identifier = parsed.path[len("/messaging/thread/"):].strip("/")
                value = await self.native(channel, "conversation", identifier=identifier,
                                          recipient_url=profile_url, limit=50)
                if value is not None:
                    if urlsplit(value[0]['url']).path.rstrip('/') != parsed.path.rstrip('/'):
                        raise SourceError("Conversation thread identity changed", "account_mismatch")
                    return None, value[0]
        browser = await self.get_browser()
        if browser is None:
            raise SourceError("No supported conversation reader", "unsupported")
        return await browser.conversation(profile_url, channel, account_url)

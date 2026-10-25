"""Gmail MCP client. Default tracking uses the pinned Shinzo JSON read API.

Legacy plain-text helpers remain for explicit compatibility commands; durable
feedback tracking uses FeedbackMailbox and never calls mailbox write tools.
"""

from applypilot import operations as ops

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

# MCP credentials directory
GMAIL_MCP_DIR = Path.home() / ".gmail-mcp"
OAUTH_KEYS_PATH = GMAIL_MCP_DIR / "gcp-oauth.keys.json"


def check_gmail_setup() -> tuple[bool, str]:
    """Verify Gmail MCP prerequisites are in place.

    Returns:
        (ok, message) — ok is True if setup is valid.
    """
    from applypilot.config import APP_DIR
    from applypilot.onboarding.gmail_state import status
    from applypilot.onboarding.resolution import application_values
    result = status(APP_DIR, application_values(APP_DIR)[0])
    return result["configured"], result["message"]


async def _create_mcp_client():
    """Create an MCP client connected to the Gmail server via stdio."""
    from applypilot.config import APP_DIR
    from applypilot.onboarding.resolution import application_values
    from .gmail_transport import open_transport
    return open_transport(APP_DIR, application_values(APP_DIR)[0])


async def _call_tool_raw(session, tool_name: str, arguments: dict) -> str:
    """Call an MCP tool and return the raw text response."""
    log.debug("MCP call: %s", tool_name)
    result = await asyncio.wait_for(
        session.call_tool(tool_name, arguments=arguments),
        timeout=120,
    )
    parts = []
    for block in result.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    text = "\n".join(parts)
    log.debug("MCP response received: %s", tool_name)
    return text


# Import ClientSession at module level for verify_connection
try:
    from mcp import ClientSession
except ImportError:
    ClientSession = None


async def _gmail_search(
    session: "ClientSession",
    query: str,
    max_results: int = 100,
) -> str:
    """Search Gmail using either the legacy or Shinzo MCP tool schema."""
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}

    if "search_emails" in names:
        return await _call_tool_raw(
            session,
            "search_emails",
            {"query": query, "max_results": max_results},
        )

    if "list_messages" in names:
        return await _call_tool_raw(
            session,
            "list_messages",
            {"q": query, "maxResults": max_results},
        )

    raise RuntimeError(
        f"No supported Gmail search tool found. Available: {sorted(names)}"
    )


async def _gmail_get_message(
    session: "ClientSession",
    message_id: str,
) -> str:
    """Read a Gmail message using either the legacy or Shinzo MCP tool schema."""
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}

    if "read_email" in names:
        return await _call_tool_raw(
            session,
            "read_email",
            {"message_id": message_id},
        )

    if "get_message" in names:
        return await _call_tool_raw(
            session,
            "get_message",
            {"id": message_id},
        )

    raise RuntimeError(
        f"No supported Gmail read tool found. Available: {sorted(names)}"
    )


async def verify_connection() -> bool:
    """Verify owner identity, required read tools and an actual read-only API call."""
    from applypilot import config
    try:
        profile = config.load_profile()
        owner = (profile.get("personal") or {}).get("email", "")
        if not owner:
            return False
        async with open_feedback_mailbox() as mailbox:
            await probe_mailbox(mailbox, expected_address=owner)
        return True
    except Exception as exc:
        log.warning("Gmail verification failed: %s", type(exc).__name__)
        return False


def _parse_search_results(text: str) -> list[dict]:
    """Parse the plain-text search_emails response into a list of email dicts.

    Format:
        ID: 19c9c2fc461f421f
        Subject: ...
        From: Name <email@example.com>
        Date: Thu, 26 Feb 2026 23:01:41 +0000 (UTC)

        ID: 19c9c1ecbca9ceb5
        ...
    """
    if text.startswith("Error:"):
        log.warning("Gmail search error: %s", text[:200])
        return []

    emails = []
    # Split on blank lines followed by "ID:"
    blocks = re.split(r'\n\n(?=ID:\s)', text.strip())

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        email = {}
        for line in block.split("\n"):
            if line.startswith("ID: "):
                email["id"] = line[4:].strip()
            elif line.startswith("Subject: "):
                email["subject"] = line[9:].strip()
            elif line.startswith("From: "):
                email["from"] = line[6:].strip()
            elif line.startswith("Date: "):
                email["date"] = line[6:].strip()
            elif line.startswith("To: "):
                email["to"] = line[4:].strip()
            elif line.startswith("Snippet: "):
                email["snippet"] = line[9:].strip()

        if email.get("id"):
            emails.append(email)

    return emails


def _parse_read_result(text: str, msg_id: str) -> dict:
    """Parse the plain-text read_email response into an email dict.

    Format:
        Thread ID: 19c9c1ecbca9ceb5
        Subject: ...
        From: Name <email@example.com>
        To: ...
        Date: ...

        Body text here...
    """
    if text.startswith("Error:"):
        log.warning("Gmail read error for %s: %s", msg_id, text[:200])
        return {"id": msg_id}

    email = {"id": msg_id}
    lines = text.split("\n")
    header_done = False
    body_lines = []

    for i, line in enumerate(lines):
        if not header_done:
            if line.startswith("Thread ID: "):
                email["thread_id"] = line[11:].strip()
            elif line.startswith("Subject: "):
                email["subject"] = line[9:].strip()
            elif line.startswith("From: "):
                email["from"] = line[6:].strip()
            elif line.startswith("To: "):
                email["to"] = line[4:].strip()
            elif line.startswith("Date: "):
                email["date"] = line[6:].strip()
            elif line == "" and email.get("subject"):
                # Blank line after headers = start of body
                header_done = True
        else:
            body_lines.append(line)

    email["body"] = "\n".join(body_lines).strip()
    return email


def _normalize_email(raw: dict) -> dict:
    """Normalize a parsed email dict into the standard format."""
    sender_raw = raw.get("from") or ""
    sender = sender_raw
    sender_name = ""
    if "<" in sender_raw:
        parts = sender_raw.split("<", 1)
        sender_name = parts[0].strip().strip('"')
        sender = parts[1].rstrip(">").strip()

    return {
        "id": raw.get("id", ""),
        "thread_id": raw.get("thread_id"),
        "subject": raw.get("subject") or "",
        "sender": sender,
        "sender_name": sender_name,
        "date": raw.get("date") or "",
        "snippet": raw.get("snippet") or "",
        "body": raw.get("body") or "",
        "to": raw.get("to") or "",
    }


async def search_application_emails(days: int = 14, limit: int = 100) -> list[dict]:
    """Search Gmail for application-related emails, returning metadata only.

    Runs the 3 search queries and returns normalized email dicts with
    subject, sender, date, snippet — but NO body text (no read_email calls).

    Args:
        days: Look-back period in days.
        limit: Maximum emails to return.

    Returns:
        List of normalized email dicts (body will be empty).
    """
    from mcp import ClientSession

    base = f"newer_than:{days}d"
    search_queries = [
        # ATS senders (most reliable signal)
        f"{base} {{from:noreply OR from:no-reply OR from:notifications OR from:talent OR from:careers}}",
        # Known ATS platforms
        f"{base} {{from:greenhouse.io OR from:lever.co OR from:icims.com OR from:myworkdayjobs.com OR from:jobvite.com OR from:smartrecruiters.com}}",
        # Subject-line keywords
        f"{base} {{subject:application OR subject:interview OR subject:candidate OR subject:\"next steps\" OR subject:\"your application\"}}",
        # Spam folder — job emails sometimes land here
        f"in:spam {base} {{subject:application OR subject:interview OR subject:offer OR subject:candidate}}",
    ]

    all_emails: dict[str, dict] = {}  # Deduplicate by ID

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                for query in search_queries:
                    try:
                        raw_text = await _gmail_search(session, query, limit)
                    except asyncio.TimeoutError:
                        log.warning("Gmail search timed out: %s", query[:60])
                        continue
                    except Exception as e:
                        log.warning("Gmail search failed: %s", e)
                        continue

                    results = _parse_search_results(raw_text)
                    log.info("Query returned %d results: %s", len(results), query[:80])

                    for msg in results:
                        msg_id = msg.get("id")
                        if not msg_id or msg_id in all_emails:
                            continue

                        normalized = _normalize_email(msg)
                        all_emails[msg_id] = normalized

                        if len(all_emails) >= limit:
                            break

    except Exception as e:
        log.error("Gmail MCP session failed: %s", e)
        raise

    return list(all_emails.values())


async def read_email_bodies(email_ids: list[str]) -> dict[str, dict]:
    """Batch-read full email bodies for a list of email IDs.

    Opens a single MCP session and reads all requested emails.

    Args:
        email_ids: List of Gmail message IDs to read.

    Returns:
        Dict mapping email ID to normalized email dict (with body).
    """
    if not email_ids:
        return {}

    from mcp import ClientSession

    results: dict[str, dict] = {}

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                for msg_id in email_ids:
                    try:
                        full_text = await _gmail_get_message(session, msg_id)
                        full = _parse_read_result(full_text, msg_id)
                        results[msg_id] = _normalize_email(full)
                    except Exception as e:
                        log.debug("Could not read email %s: %s", msg_id, e)

    except Exception as e:
        log.error("Gmail MCP session failed during body read: %s", e)
        raise

    log.info("Read %d/%d email bodies", len(results), len(email_ids))
    return results


async def _get_or_create_label(session, label_name: str) -> str | None:
    """Return the Gmail label ID for label_name, creating it if missing.

    Uses the MCP server's native get_or_create_label tool.
    Returns None if label operations fail.
    """
    try:
        raw = await _call_tool_raw(session, "get_or_create_label", {"name": label_name})
        if raw.startswith("Error:"):
            log.warning("Could not get/create Gmail label '%s': %s", label_name, raw[:100])
            return None
        m = re.search(r"Label_\w+", raw)
        if m:
            return m.group(0)
        log.warning("Could not parse label ID from response: %s", raw[:100])
        return None
    except Exception as e:
        log.warning("Gmail label operation failed: %s", e)
        return None


async def apply_label_to_emails(email_ids: list[str], label: str = "ap-track") -> int:
    """Apply a Gmail label to a batch of email IDs using batch_modify_emails.

    Best-effort: gracefully skips if label operations fail.

    Args:
        email_ids: Gmail message IDs to label.
        label: Label name to apply (created if it doesn't exist).

    Returns:
        Count of emails submitted for labeling (batch operation).
    """
    if not email_ids:
        return 0

    from mcp import ClientSession

    try:
        async with await _create_mcp_client() as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=30)

                label_id = await _get_or_create_label(session, label)
                if label_id is None:
                    return 0

                # Chunk to avoid MCP client TaskGroup errors with large payloads
                CHUNK = 50
                total_labeled = 0
                for i in range(0, len(email_ids), CHUNK):
                    chunk = email_ids[i:i + CHUNK]
                    raw = await _call_tool_raw(session, "batch_modify_emails", {
                        "messageIds": chunk,
                        "addLabelIds": [label_id],
                    })
                    if raw.startswith("Error:"):
                        log.warning("batch_modify_emails failed (chunk %d): %s", i // CHUNK, raw[:100])
                        continue
                    total_labeled += len(chunk)

                log.info("Applied '%s' label to %d emails", label, total_labeled)
                return total_labeled

    except Exception as e:
        log.warning("Gmail label session failed: %s", e)
        return 0


async def fetch_application_emails(days: int = 14, limit: int = 100) -> list[dict]:
    """Fetch application-related emails from Gmail via MCP.

    Backward-compatible wrapper that searches and reads bodies for all emails.

    Args:
        days: Look-back period in days.
        limit: Maximum emails to return.

    Returns:
        List of normalized email dicts with body text.
    """
    emails = await search_application_emails(days=days, limit=limit)
    if not emails:
        return emails

    # Read bodies for all emails
    ids = [e["id"] for e in emails]
    bodies = await read_email_bodies(ids)

    # Merge body text into metadata emails
    for email in emails:
        if email["id"] in bodies:
            full = bodies[email["id"]]
            email["body"] = full.get("body", "")
            email["thread_id"] = full.get("thread_id") or email.get("thread_id")

    return emails

# ---------------------------------------------------------------------------
# Durable feedback transport. Pinned Shinzo MCP uses Gmail JSON, not the legacy
# human-readable response format above. No send/modify/delete tool is exposed
# through this read-only adapter.
# ---------------------------------------------------------------------------
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parseaddr


class GmailReadError(RuntimeError):
    def __init__(self, kind='provider_error', details=None, *, status_code=None, provider_code=None):
        self.kind = kind
        self.details = details
        self.status_code = status_code
        self.provider_code = provider_code
        super().__init__((details or {}).get('error') or 'Gmail read failed: ' + kind)


def _decode_tool_json(result):
    """Decode MCP results while preserving actual provider failure evidence.

    Some servers use isError + text, the pinned server uses JSON {error: ...}.
    Neither may turn into an empty inbox or hide an expired OAuth grant.
    """
    structured = getattr(result, 'structuredContent', None)
    texts = [b.text for b in getattr(result, 'content', []) if isinstance(getattr(b, 'text', None), str)]
    text = '\n'.join(texts)
    if isinstance(structured, dict):
        data = structured
    else:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            if not getattr(result, 'isError', False):
                raise GmailReadError('invalid_json') from None
            data = {'error': text[:16000]}
    if getattr(result, 'isError', False) or (isinstance(data, dict) and data.get('error')):
        from applypilot.operations.runtime import sanitize
        error = data.get('error') if isinstance(data, dict) else None
        fields = error if isinstance(error, dict) else {}
        code = fields.get('code', data.get('code') if isinstance(data, dict) else None)
        if type(code) not in (str, int):
            code = None
        status = code if type(code) is int and 100 <= code <= 599 else None
        message = fields.get('message') or error or text or 'MCP tool execution failed'
        details = {'component': 'gmail', 'code': sanitize(str(code), 200) if code is not None else 'gmail_tool_error',
                   'error': sanitize(str(message), 4000), 'details_redacted': True}
        raise GmailReadError(details=details, status_code=status, provider_code=code)
    if not isinstance(data, dict):
        raise GmailReadError('invalid_schema')
    return data


def normalize_gmail_message(raw: dict, *, body_decoded: bool = True) -> dict:
    """Normalize a Shinzo-decoded message; raw REST fixtures may set False.

    Prefer Gmail internalDate to the sender's Date header. Do not fetch
    attachments, follow URLs, or execute HTML. Plain text wins over HTML.
    """
    import base64
    from applypilot.tracking.feedback_store import iso
    payload = raw.get('payload') or {}
    headers = {str(h.get('name','')).casefold(): str(h.get('value',''))
               for h in payload.get('headers', []) if isinstance(h, dict)}
    plain, html_parts = [], []

    def walk(part):
        if not isinstance(part, dict) or part.get('filename') or part.get('mimeType') == 'message/rfc822':
            return
        mime = part.get('mimeType','')
        data = (part.get('body') or {}).get('data')
        if isinstance(data, str) and mime in {'text/plain','text/html'}:
            if not body_decoded:
                try:
                    data = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', errors='replace')
                except (ValueError, TypeError) as exc:
                    raise GmailReadError('invalid_body_encoding') from exc
            (plain if mime == 'text/plain' else html_parts).append(data)
        for child in part.get('parts', []):
            walk(child)

    walk(payload)
    body = '\n'.join(plain)
    links=[]
    if html_parts:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('\n'.join(html_parts), 'html.parser')
        for tag in soup.select('script,style,blockquote,.gmail_quote,.yahoo_quoted'):
            tag.decompose()
        links=[str(a.get('href')) for a in soup.find_all('a',href=True) if str(a.get('href')).startswith(('https://','http://'))][:100]
        if not body:
            body = soup.get_text(' ', strip=True)
    name, sender = parseaddr(headers.get('from',''))
    try:
        when = datetime.fromtimestamp(int(raw['internalDate']) / 1000, timezone.utc).isoformat()
    except (KeyError, ValueError, TypeError, OverflowError):
        try:
            when = iso(headers.get('date')) if headers.get('date') else None
        except ValueError:
            when = None
    return {'id': str(raw.get('id') or ''), 'thread_id': raw.get('threadId'),
            'sender': sender.casefold(), 'sender_name': name, 'subject': headers.get('subject',''),
            'to': headers.get('to',''), 'date': when, 'snippet': raw.get('snippet',''),
            'body': body[:24000], 'links': links, 'labels': list(raw.get('labelIds') or []),
            'rfc_message_id': headers.get('message-id',''),
            'in_reply_to': headers.get('in-reply-to',''), 'references': headers.get('references',''),
            'history_id': raw.get('historyId'),
            'auto_submitted': headers.get('auto-submitted',''),
            'precedence': headers.get('precedence',''),
            'reply_to': parseaddr(headers.get('reply-to',''))[1].casefold()}


class FeedbackMailbox:
    """One initialized MCP session per sync, with tools discovered once."""
    def __init__(self, session, tools):
        self.session = session
        self.tools = set(tools)
        self.address = ''
        self.mailbox_key = ''

    @ops.observe('gmail', 'read')
    async def _read(self, name, args):
        if name not in {'get_profile','list_messages','get_message'} or name not in self.tools:
            raise GmailReadError('required_read_capability_missing:' + name)
        result = await asyncio.wait_for(self.session.call_tool(name, arguments=args), timeout=120)
        return _decode_tool_json(result)

    async def identify(self):
        from applypilot.tracking.feedback_store import digest
        raw = await self._read('get_profile', {})
        address = str(raw.get('emailAddress') or '').strip().casefold()
        if '@' not in address:
            raise GmailReadError('missing_mailbox_identity')
        self.address = address
        self.mailbox_key = digest(address)
        return self.mailbox_key

    async def list_page(self, after: int, before: int, token=None, limit=100):
        args = {'q': f'after:{max(0,after-1)} before:{before} -in:sent -in:drafts',
                'includeSpamTrash': True, 'maxResults': max(1,min(500,limit))}
        if token:
            args['pageToken'] = token
        data = await self._read('list_messages', args)
        messages = data.get('messages', [])
        if not isinstance(messages, list) or any(not isinstance(m, dict) or not m.get('id') for m in messages):
            raise GmailReadError('invalid_message_list')
        if data.get('nextPageToken') and not isinstance(data['nextPageToken'], str):
            raise GmailReadError('invalid_page_token')
        return messages, data.get('nextPageToken')

    async def get(self, message_id):
        data = await self._read('get_message', {'id': message_id, 'includeBodyHtml': True})
        if str(data.get('id') or '') != message_id:
            raise GmailReadError('message_identity_mismatch')
        return normalize_gmail_message(data, body_decoded=True)


@asynccontextmanager
async def open_feedback_mailbox():
    from mcp import ClientSession
    async with await _create_mcp_client() as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)
            tools = await asyncio.wait_for(session.list_tools(), timeout=30)
            mailbox = FeedbackMailbox(session, [t.name for t in tools.tools])
            missing = {'get_profile', 'list_messages', 'get_message'} - mailbox.tools
            if missing:
                raise GmailReadError('required_read_capability_missing:' + ','.join(sorted(missing)))
            await mailbox.identify()
            yield mailbox


async def probe_mailbox(mailbox, *, expected_address: str, require_send: bool = False) -> dict:
    """Harmless identity + list probe. Tool names alone never prove OAuth scopes.

    This does not send a test email or claim that delivery/send scope was tested.
    """
    import time
    from applypilot.referrals.store import email_address
    owner = email_address(expected_address)
    if mailbox.address.casefold() != owner:
        raise GmailReadError('mailbox_identity_mismatch')
    required = {'get_profile', 'list_messages', 'get_message'}
    if require_send:
        required.add('send_message')
    missing = required - mailbox.tools
    if missing:
        raise GmailReadError('required_read_capability_missing:' + ','.join(sorted(missing)))
    now = int(time.time())
    await mailbox.list_page(now - 60, now + 1, limit=1)
    return {'identity_verified': True, 'read_verified': True,
            'send_tool_available': 'send_message' in mailbox.tools,
            'send_permission_verified': False, 'messages_read': 0, 'sent': 0}

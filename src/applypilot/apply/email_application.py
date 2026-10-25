"""ApplicationAttempt-bound Gmail application delivery with durable, single-send intent.

A Message-ID is a reconciliation key, never an idempotency guarantee. Once a
delivery enters sending, subsequent calls only inspect Sent; they never resend.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from datetime import UTC, datetime
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import getaddresses
from pathlib import Path
from uuid import uuid4

from applypilot import config, operations as ops
from applypilot.apply import guard
from applypilot.apply.routes import ApplicationRoute, parse_email_target, persist_route
from applypilot.database import close_connection, get_connection
from applypilot.discovery.repository import transaction
from applypilot.referrals.outreach import ReferralMailbox, owner_profile_hash
from applypilot.referrals.store import email_address
from applypilot.tracking.gmail_client import normalize_gmail_message, open_feedback_mailbox, probe_mailbox


def _now():
    return datetime.now(UTC).isoformat()


def _dump(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_PENDING_EMAIL_SQL = """SELECT d.* FROM application_email_deliveries d
    JOIN application_attempts a ON a.attempt_id=d.attempt_id
    WHERE a.channel='email'
      AND a.status IN ('side_effect_intent','verification_required','verification_quarantined')
      AND (d.state IN ('uncertain','sent') OR (d.state='sending'
        AND (ap_day(d.attempted_at)<=ap_day('now','-2 minutes')
             OR a.status IN ('verification_required','verification_quarantined'))))"""


def ensure_schema(conn):
    """Add the outbox in the caller's transaction; never commit or send."""
    conn.execute("""CREATE TABLE IF NOT EXISTS application_email_deliveries (
        attempt_id TEXT PRIMARY KEY REFERENCES application_attempts(attempt_id) ON DELETE CASCADE,
        route_id TEXT UNIQUE NOT NULL, job_url TEXT NOT NULL, href TEXT NOT NULL,
        recipient TEXT NOT NULL, evidence_json TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'registered'
            CHECK(state IN ('registered','prepared','sending','uncertain','sent')),
        payload_json TEXT, payload_hash TEXT, rfc_message_id TEXT UNIQUE NOT NULL,
        mailbox_key TEXT, dispatch_token TEXT, attempted_at TEXT, sent_at TEXT,
        message_id TEXT, thread_id TEXT, error_kind TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    columns = {r[1] for r in conn.table_info('application_email_deliveries')}
    if "transport" not in columns:
        conn.execute("ALTER TABLE application_email_deliveries ADD COLUMN transport TEXT NOT NULL DEFAULT 'gmail_mcp'")


def _delivery(conn, attempt_id, route_id=None):
    row = conn.execute("SELECT * FROM application_email_deliveries WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    if not row or (route_id is not None and row["route_id"] != route_id):
        raise guard.GuardRejected("unknown email route for this reservation")
    return dict(row)


def _route_result(row):
    return {"route": "email", "route_id": row["route_id"], "recipient": row["recipient"],
            "posting_url": row["job_url"], "next_action": "Call applypilot_control.submit_email_application(route_id)",
            "status": row["state"], "committed": False}


def register_route(attempt_id, href, *, evidence, conn=None):
    """Transfer the owning, untouched reservation to an observed email route."""
    target = parse_email_target(href)
    conn = conn or get_connection()
    with transaction(conn):
        ensure_schema(conn)
        attempt = guard.attempt_status(attempt_id, conn=conn)
        existing = conn.execute("SELECT * FROM application_email_deliveries WHERE attempt_id=?",
                                (attempt_id,)).fetchone()
        if existing:
            if existing["href"] != target["target"]:
                raise guard.GuardRejected("this reservation already has a different email route")
            return _route_result(dict(existing))
        attempt = guard.validate_attempt(attempt_id, conn=conn)
        if (attempt.get("side_effect_intent_at") or attempt.get("browser_ambiguous_action_at")):
            raise guard.GuardRejected("email handoff requires an untouched active reservation")
        if not isinstance(evidence, dict):
            raise guard.GuardRejected("observed application email evidence required")
        source = evidence.get("source")
        if source == "stored-email-route":
            if attempt["channel"] != "email" or attempt["target"] != target["target"]:
                raise guard.GuardRejected("email target does not match the reserved stored route")
        elif source == "browseros-application-link":
            if (attempt["channel"] != "external" or attempt["browser_runtime"] != "browseros"
                    or evidence.get("attempt_id") != attempt_id
                    or evidence.get("posting_url") != attempt["job_url"]
                    or not evidence.get("page_url")
                    or evidence.get("observed_href") != href):
                raise guard.GuardRejected("email route is not bound to the owning browser observation")
        else:
            raise guard.GuardRejected("email route requires trusted application evidence")
        # preserve_route fences verified referral links; a mailto cannot discard one.
        persist_route(conn, attempt["job_url"], ApplicationRoute(
            "email", target["target"], _dump(evidence), _now()))
        route = conn.execute("SELECT * FROM application_routes WHERE job_url=?",
                             (attempt["job_url"],)).fetchone()
        if route["route"] != "email" or route["target"] != target["target"]:
            raise guard.GuardRejected("protected route prevented email handoff")
        current = guard._current_job(conn, attempt["job_url"], attempt["doc_format"] or "pdf")
        changed = conn.execute("""UPDATE application_attempts SET channel='email', target=?, submission_identity=?,
            browser_runtime='gmail', route_version=?, snapshot_json=?, updated_at=?
            WHERE attempt_id=? AND status='prepared' AND side_effect_intent_at IS NULL
            AND browser_ambiguous_action_at IS NULL""",
            (target["target"], guard.submission_identity(current), route["version"], guard.attempt_snapshot(current), _now(), attempt_id))
        if changed.rowcount != 1:
            raise guard.GuardRejected("reservation changed during email handoff")
        rid, now = uuid4().hex, _now()
        conn.execute("""INSERT INTO application_email_deliveries
            (attempt_id,route_id,job_url,href,recipient,evidence_json,rfc_message_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (attempt_id, rid, attempt["job_url"], target["target"],
            target["recipient"], _dump(evidence), f"<applypilot-application-{rid}@applications.invalid>", now, now))
        guard.validate_attempt(attempt_id, conn=conn)
        row = _delivery(conn, attempt_id)
    _event("email_route_registered", attempt_id)
    return _route_result(row)


def _event(operation, attempt_id, *, outcome=None):
    ops.record(component_name="applications", operation=operation, stage="applications", kind="signal",
               severity="info", outcome=outcome or "success", task_id=attempt_id,
               details={"attempt_id": attempt_id, "channel": "email", "outcome": outcome})


def _attachments(attempt):
    manifest = json.loads(attempt["artifact_hashes_json"] or "{}")
    copies = json.loads(attempt.get("uploaded_artifacts_json") or "{}")
    attachments = []
    for kind in ("resume", "cover_letter"):
        artifact = manifest.get(kind)
        if not artifact:
            continue
        expected = artifact["files"][artifact["path"]]
        # Browser workers already bind their copies. Keep those exact copies.
        path = next((p for p, digest in copies.items() if digest == expected), None)
        if not path:
            raise guard.GuardRejected("reserved application documents have not been bound")
        candidate = Path(path)
        data = candidate.read_bytes()
        suffix = candidate.suffix.lower()
        if (suffix not in {".pdf", ".docx"} or not 0 < len(data) <= 10_000_000
                or (suffix == ".pdf" and not data.startswith(b"%PDF"))
                or (suffix == ".docx" and not data.startswith(b"PK"))
                or hashlib.sha256(data).hexdigest() != expected):
            raise guard.GuardRejected("reserved email attachment is unavailable or changed")
        attachments.append({"kind": kind, "path": str(candidate.resolve()), "sha256": expected,
                            "name": ("Resume" if kind == "resume" else "Cover Letter") + suffix,
                            "size": len(data)})
    return attachments


def _bind_documents(attempt_id, conn):
    attempt = guard.validate_attempt(attempt_id, conn=conn)
    if not attempt.get("uploaded_artifacts_json"):
        manifest = json.loads(attempt["artifact_hashes_json"] or "{}")
        guard.bind_uploaded_artifacts(attempt_id,
            {kind: artifact["path"] if artifact else None for kind, artifact in manifest.items()}, conn=conn)


def _payload(attempt, row, profile, *, include_reference=True):
    personal = profile.get("personal") or {}
    sender, name = email_address(personal.get("email")), str(personal.get("full_name") or "").strip()
    if not name or any(ord(c) < 32 for c in name):
        raise guard.GuardRejected("profile sender name is missing or invalid")
    posting = json.loads(attempt["snapshot_json"])
    from applypilot.apply.application_contract import contract_for, missing_requirements, submission_content
    if missing := missing_requirements(posting):
        raise guard.GuardRejected("application_requirements: " + "; ".join(item["reason"] for item in missing))
    contract = contract_for(posting)
    target = parse_email_target(row["href"])
    subject = contract.get("required_subject") or target["subject"] or f"Application for {posting.get('title') or 'open role'} - {name}"
    if any(ord(c) < 32 for c in subject) or len(subject) > 998:
        raise guard.GuardRejected("application subject is invalid")
    attachments = _attachments(attempt)
    material = " and ".join(item["kind"].replace("_", " ") for item in attachments)
    # Posted body is immutable route context, not applicant facts or instructions
    # to synthesize claims. Application prose uses only the owner and posting.
    body = ("Hello,\n\n" + f"I am applying for {posting.get('title') or 'the open role'}"
            + (f" at {posting['company']}" if posting.get("company") else "") + ".\n"
            + (f"Please find my {material} attached for your review.\n\n" if material else "\n")
            + (submission_content(posting) + "\n\n" if submission_content(posting) else "")
            + f"Job posting: {row['job_url']}\n\nThank you for your consideration.\n{name}\n{sender}")
    reference = "applypilot-application-" + row["route_id"]
    if include_reference:
        body += "\n\nApplication reference: " + reference
    result = {"from": sender, "to": row["recipient"], "subject": subject, "body": body,
              "attachments": attachments, "owner_profile_hash": owner_profile_hash(profile),
              "job_url": row["job_url"], "route_id": row["route_id"],
              "application_contract": contract}
    if include_reference:
        result["delivery_reference"] = reference
    return result


def _validated_payload(attempt, row, *, profile=None):
    if not row["payload_json"] or _digest(row["payload_json"]) != row["payload_hash"]:
        raise guard.GuardRejected("email payload binding is missing or changed")
    payload = json.loads(row["payload_json"])
    if (attempt["channel"] != "email" or attempt["browser_runtime"] != "gmail"
            or attempt["target"] != row["href"] or attempt["job_url"] != row["job_url"]
            or payload["to"] != row["recipient"] or payload["route_id"] != row["route_id"]
            or row["rfc_message_id"] != f"<applypilot-application-{row['route_id']}@applications.invalid>"
            or row["mailbox_key"] != _digest(payload["from"])):
        raise guard.GuardRejected("email delivery identity changed")
    if (row.get("transport", "gmail_mcp") not in {"gmail_mcp", "gmail_browseros"}
            or ("delivery_reference" in payload and payload["delivery_reference"] != "applypilot-application-" + row["route_id"])):
        raise guard.GuardRejected("email transport or reconciliation identity changed")
    if profile is not None and _payload(dict(attempt), row, profile,
                                        include_reference="delivery_reference" in payload) != payload:
        raise guard.GuardRejected("sender facts or reserved application materials changed")
    return payload


def _raw_message(row, payload):
    msg = EmailMessage(policy=SMTP.clone(max_line_length=998))
    msg["From"], msg["To"], msg["Subject"] = payload["from"], payload["to"], payload["subject"]
    msg["Message-ID"] = row["rfc_message_id"]
    msg.set_content(payload["body"])
    for item in payload["attachments"]:
        data = Path(item["path"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise guard.GuardRejected("email attachment changed")
        subtype = "pdf" if item["name"].endswith(".pdf") else "vnd.openxmlformats-officedocument.wordprocessingml.document"
        msg.add_attachment(data, maintype="application", subtype=subtype, filename=item["name"])
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")


async def prepare_email_application(attempt_id, route_id, *, mailbox, conn=None, probed=False):
    """Validate account/capability and freeze the exact payload before intent."""
    conn = conn or get_connection()
    if conn.in_transaction:
        raise guard.GuardRejected("email preparation requires a durable independent transaction")
    _bind_documents(attempt_id, conn)
    profile = config.load_profile()
    if not probed:
        await _probe_application_mailbox(mailbox, require_send=True)
    with transaction(conn):
        attempt = guard.validate_attempt(attempt_id, conn=conn)
        row = _delivery(conn, attempt_id, route_id)
        if row["state"] not in {"registered", "prepared"}:
            raise guard.GuardRejected("email delivery already attempted; reconcile Sent only")
        payload = _payload(attempt, row, profile)
        if payload["from"] != mailbox.address or mailbox.mailbox_key != _digest(payload["from"]):
            raise guard.GuardRejected("connected Gmail account differs from the application sender")
        if row["state"] == "prepared":
            previous = _validated_payload(attempt, row, profile=profile)
            # Legacy prepared rows have no UI reconciliation marker. Migration is
            # allowed only before durable intent, after revalidating their bytes.
            if row.get("transport") != "gmail_browseros" or previous.get("delivery_reference"):
                payload = previous
        serialized = _dump(payload)
        conn.execute("""UPDATE application_email_deliveries SET state='prepared',payload_json=?,
            payload_hash=?,mailbox_key=?,updated_at=? WHERE attempt_id=? AND state IN ('registered','prepared')""",
            (serialized, _digest(serialized), mailbox.mailbox_key, _now(), attempt_id))
        row = _delivery(conn, attempt_id, route_id)
    _event("email_application_prepared", attempt_id)
    return row


def validate_intent(attempt, conn, *, expected_payload_hash=None, expected_transport=None):
    """Guard hook: claim delivery in the SAME transaction as submission intent."""
    if not conn.in_transaction:
        raise guard.GuardRejected("email intent requires the guard transaction")
    row = _delivery(conn, attempt["attempt_id"])
    if row["state"] != "prepared":
        raise guard.GuardRejected("email payload is not prepared or delivery was already attempted")
    if not expected_payload_hash or row["payload_hash"] != expected_payload_hash or row.get("transport", "gmail_mcp") != expected_transport:
        raise guard.GuardRejected("prepared email payload or transport changed before send intent")
    _validated_payload(attempt, row, profile=config.load_profile())
    if attempt["browser_ambiguous_action_at"] or attempt["side_effect_intent_at"]:
        raise guard.GuardRejected("an earlier browser or email side effect requires verification")
    changed = conn.execute("""UPDATE application_email_deliveries SET state='sending',
        dispatch_token=?,attempted_at=?,updated_at=? WHERE attempt_id=? AND state='prepared'""",
        (uuid4().hex, _now(), _now(), attempt["attempt_id"]))
    if changed.rowcount != 1:
        raise guard.GuardRejected("another worker already claimed this email")


def validate_delivery_proof(attempt, proof_kind, proof, conn):
    """Guard hook accepting only the transport's persisted matching Gmail ID."""
    row = _delivery(conn, attempt["attempt_id"])
    _validated_payload(attempt, row)
    if (proof_kind != "gmail-message" or row["state"] != "sent" or row["message_id"] != proof
            or not row["dispatch_token"] or not row["attempted_at"] or not row["sent_at"]
            or not attempt["side_effect_intent_at"]):
        raise guard.GuardRejected("email submission requires persisted Gmail delivery evidence")
    return proof


class ApplicationMailbox(ReferralMailbox):
    """Reuse the explicit Gmail sender; keep feedback's read allowlist intact."""

    async def find_sent(self, rfc_id, *, reference=None):
        if not re.fullmatch(r"<applypilot-application-[0-9a-f]{32}@applications\.invalid>", rfc_id):
            raise ValueError("invalid application reconciliation Message-ID")
        if reference is not None and not re.fullmatch(r"applypilot-application-[0-9a-f]{32}", reference):
            raise ValueError("invalid application reconciliation reference")
        query = "in:sent " + ('"' + reference + '"' if reference else "rfc822msgid:" + rfc_id[1:-1])
        result = await self.mailbox._read("list_messages", {"q": query, "maxResults": 10})
        if result.get("nextPageToken"):
            raise ValueError("ambiguous application reconciliation")
        messages = []
        for item in result.get("messages", []):
            raw = await self.mailbox._read("get_message", {"id": item["id"], "includeBodyHtml": True})
            if raw.get("id") != item["id"]:
                raise ValueError("Sent message identity mismatch")
            message = normalize_gmail_message(raw, body_decoded=True)
            headers = {str(h.get("name", "")).casefold(): str(h.get("value", ""))
                       for h in (raw.get("payload") or {}).get("headers", [])}
            message["extra_recipients"] = bool(headers.get("cc") or headers.get("bcc"))
            message["attachments"] = []
            def collect(part):
                if part.get("filename"):
                    body = part.get("body") or {}
                    entry = {"name": part["filename"], "size": body.get("size")}
                    # REST usually supplies attachmentId + size. If bytes are
                    # present, compare their digest too; never fetch attachments.
                    data = body.get("data")
                    if isinstance(data, str) and data:
                        try:
                            decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
                            entry["sha256"] = hashlib.sha256(decoded).hexdigest()
                        except (ValueError, TypeError):
                            entry["sha256"] = "invalid"
                    message["attachments"].append(entry)
                for child in part.get("parts", []):
                    collect(child)
            collect(raw.get("payload") or {})
            messages.append(message)
        return messages


def _confirm(conn, row, response):
    if not isinstance(response, dict) or not isinstance(response.get("id"), str) or not response["id"].strip():
        raise ValueError("Gmail did not confirm delivery")
    with transaction(conn):
        changed = conn.execute("""UPDATE application_email_deliveries SET state='sent',message_id=?,
            thread_id=?,sent_at=?,error_kind=NULL,updated_at=? WHERE attempt_id=?
            AND dispatch_token=? AND state IN ('sending','uncertain')""",
            (response["id"], response.get("threadId"), _now(), _now(), row["attempt_id"], row["dispatch_token"]))
        if changed.rowcount != 1:
            current = _delivery(conn, row["attempt_id"])
            if current["state"] != "sent" or current["message_id"] != response["id"]:
                raise guard.GuardRejected("email send ownership changed")


def _commit_sent(conn, row):
    guard.mark_submitted(row["attempt_id"], proof_kind="gmail-message", proof=row["message_id"], conn=conn)
    _event("email_application_sent", row["attempt_id"], outcome="sent")
    return {"status": "submitted", "committed": True, "reason": None, "route_id": row["route_id"]}


def _uncertain(conn, row, error_kind):
    with transaction(conn):
        conn.execute("""UPDATE application_email_deliveries SET state='uncertain',error_kind=?,updated_at=?
            WHERE attempt_id=? AND dispatch_token=? AND state='sending'""",
            (error_kind, _now(), row["attempt_id"], row["dispatch_token"]))
    attempt = guard.require_verification(row["attempt_id"],
        reason="Gmail application delivery requires Sent verification", conn=conn)
    if attempt["status"] == "submitted":
        return {"status": "sent", "committed": True, "reason": None, "route_id": row["route_id"]}
    _event("email_application_uncertain", row["attempt_id"], outcome=error_kind)
    return {"status": "verification_required", "committed": False,
            "reason": "gmail_delivery_uncertain", "route_id": row["route_id"]}


async def _reconcile(conn, mailbox, row, *, before_publish=None):
    attempt = guard.attempt_status(row["attempt_id"], conn=conn)
    payload = _validated_payload(attempt, row)
    if mailbox.address != payload["from"] or mailbox.mailbox_key != row["mailbox_key"]:
        raise guard.GuardRejected("reconcile using the original application sender mailbox")
    if row["state"] == "sent":
        if before_publish:
            before_publish()
        return _commit_sent(conn, row)
    if row["state"] not in {"sending", "uncertain"}:
        raise guard.GuardRejected("no attempted email delivery to reconcile")
    reference = payload.get("delivery_reference") if row.get("transport") == "gmail_browseros" else None
    if getattr(mailbox, "transport", None) == "gmail_browseros":
        matches = await mailbox.find_sent_application(row["rfc_message_id"], reference=reference)
    else:
        sender = ApplicationMailbox(mailbox, allow_send=False)
        matches = await sender.find_sent(row["rfc_message_id"], reference=reference)
    def normalized(text):
        return str(text or "").replace("\r\n", "\n").replace("\u00a0", " ").rstrip("\n")
    def materials_match(message):
        expected = {item["name"]: item for item in payload["attachments"]}
        actual = message.get("attachments", [])
        if len(actual) != len(expected) or {item.get("name") for item in actual} != set(expected):
            return False
        return all(item.get("size") == expected[item["name"]]["size"]
                   and (not item.get("sha256") or item["sha256"] == expected[item["name"]]["sha256"])
                   for item in actual)
    matches = [message for message in matches
               if message.get("sender") == payload["from"]
               and [address.casefold() for _, address in getaddresses([message.get("to", "")])] == [payload["to"]]
               and not message.get("extra_recipients")
               and (message.get("rfc_message_id", "").strip() == row["rfc_message_id"]
                    if row.get("transport") != "gmail_browseros" else
                    bool(reference and reference in message.get("body", "")
                         and re.fullmatch(r"<[^\s<>]+@[^\s<>]+>", message.get("rfc_message_id", ""))))
               and message.get("subject") == payload["subject"]
               and normalized(message.get("body")) == normalized(payload["body"])
               and materials_match(message)
               and "SENT" in message.get("labels", []) and message.get("id")]
    if before_publish:
        before_publish()
    if len(matches) != 1:
        return _uncertain(conn, row, "sent_evidence_not_unique")
    match = matches[0]
    _confirm(conn, row, {"id": match["id"], "threadId": match.get("thread_id")})
    return _commit_sent(conn, _delivery(conn, row["attempt_id"]))


async def reconcile_pending_email_applications(conn, mailbox, limit=3, *, before_publish=None):
    """Spend at most 30 seconds checking up to three old sends on this mailbox.

    This reuses the inbox's read session and never creates a sender or retries a
    write. Lease renewal/publication fencing belongs to the caller's callback.
    """
    stats = {"checked": 0, "sent": 0, "uncertain": 0, "errors": 0}
    if (not mailbox.mailbox_key or not mailbox.address or not isinstance(limit, int) or limit <= 0
            or not conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() "
                                "AND tablename='application_email_deliveries'").fetchone()):
        return stats
    rows = [dict(row) for row in conn.execute(_PENDING_EMAIL_SQL +
        " AND d.mailbox_key=? ORDER BY d.updated_at,d.attempt_id LIMIT ?", (mailbox.mailbox_key, min(3, limit)))]
    deadline = time.monotonic() + 30
    for row in rows:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            if before_publish:
                before_publish()
            stats["checked"] += 1
            async with asyncio.timeout(remaining):
                result = await _reconcile(conn, mailbox, row, before_publish=before_publish)
            stats["sent" if result["committed"] else "uncertain"] += 1
        except Exception as exc:
            stats["errors"] += 1
            try:
                if before_publish:
                    before_publish()
                _uncertain(conn, row, type(exc).__name__)
                stats["uncertain"] += 1
            except Exception as held_error:
                _event("email_reconciliation_deferred", row["attempt_id"], outcome=type(held_error).__name__)
        # Rotate unresolved records so a stale first item cannot starve later
        # sends. No frozen payload, send identity, or intent is changed.
        try:
            if before_publish:
                before_publish()
            with transaction(conn):
                conn.execute("UPDATE application_email_deliveries SET updated_at=? WHERE attempt_id=?",
                             (_now(), row["attempt_id"]))
        except Exception:
            stats["errors"] += 1
            break
    return stats


def reconcile_waiting_email_applications():
    """Removed batch recovery entrypoint; account cursors remain in native mailbox workflows."""
    raise RuntimeError("Use ApplicationWorkflow evidence reconciliation for an identified attempt")



async def reconcile_email_application(attempt_id, *, conn=None, browser=None):
    """Read-only network reconciliation; absence never enables a resend."""
    conn = conn or get_connection()
    row = _delivery(conn, attempt_id)
    if row["state"] not in {"sending", "uncertain", "sent"}:
        return {"status": "blocked", "committed": False, "reason": "email_not_attempted",
                "route_id": row["route_id"]}
    try:
        if row["state"] == "sent":
            return _commit_sent(conn, row)
        payload = _validated_payload(guard.attempt_status(attempt_id, conn=conn), row)
        async with _open_reconciliation_mailbox(payload["from"], browser=browser) as mailbox:
            return await _reconcile(conn, mailbox, row)
    except Exception as exc:
        _uncertain(conn, row, type(exc).__name__)
        raise


def _browser_fallback_allowed(exc):
    """Select the configured browser before send intent; preserve domain rejection."""
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(_browser_fallback_allowed(e) for e in exc.exceptions)
    return isinstance(exc, Exception) and not isinstance(exc, guard.GuardRejected)


async def _probe_application_mailbox(mailbox, *, require_send=False):
    address = email_address((config.load_profile().get("personal") or {}).get("email"))
    if mailbox.address != address or mailbox.mailbox_key != _digest(address):
        raise guard.GuardRejected("connected Gmail account differs from the application sender")
    if getattr(mailbox, "transport", None) == "gmail_browseros":
        return await mailbox.probe(require_send=require_send)
    return await probe_mailbox(mailbox, expected_address=address, require_send=require_send)


from contextlib import AsyncExitStack, asynccontextmanager


@asynccontextmanager
async def _open_reconciliation_mailbox(address, *, browser=None):
    # Exceptions thrown by the caller after yield are deliberately outside the
    # connection try/except. A failed operation must never silently change mode.
    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(45):
                mailbox = await stack.enter_async_context(open_feedback_mailbox())
                if mailbox.address != address or mailbox.mailbox_key != _digest(address):
                    raise guard.GuardRejected("reconcile using the original application sender mailbox")
                await probe_mailbox(mailbox, expected_address=address)
        except Exception as exc:
            if not _browser_fallback_allowed(exc):
                raise
        else:
            yield mailbox
            return
    from applypilot.tracking.gmail_browser import open_gmail_browser
    async with open_gmail_browser(address, browser=browser) as mailbox:
        yield mailbox


def _request_browser(conn, attempt_id, route_id):
    with transaction(conn):
        attempt = guard.validate_attempt(attempt_id, conn=conn)
        row = _delivery(conn, attempt_id, route_id)
        if (row["state"] not in {"registered", "prepared"} or attempt.get("side_effect_intent_at")
                or attempt.get("browser_ambiguous_action_at")):
            raise guard.GuardRejected("attempted delivery cannot switch send transports")
        conn.execute("UPDATE application_email_deliveries SET transport='gmail_browseros',updated_at=? WHERE attempt_id=?",
                     (_now(), attempt_id))
    _event("email_browser_fallback_selected", attempt_id)


async def submit_email_application(attempt_id, route_id, *, conn=None, browser=None):
    """Gmail MCP first; same-account browser fallback only before send intent."""
    conn = conn or get_connection()
    row = None
    phase = "route"

    async def send_using(mailbox):
        nonlocal row, phase
        phase = "gmail_probe" if getattr(mailbox, "transport", None) != "gmail_browseros" else "browser_probe"
        async with asyncio.timeout(45):
            await _probe_application_mailbox(mailbox, require_send=True)
        phase = "prepare"
        row = await prepare_email_application(attempt_id, route_id, mailbox=mailbox, conn=conn, probed=True)
        payload = json.loads(row["payload_json"])
        is_browser = getattr(mailbox, "transport", None) == "gmail_browseros"
        if is_browser:
            phase = "browser_prepare"
            await mailbox.prepare_application(row, payload)
        else:
            sender = ApplicationMailbox(mailbox)
            raw = _raw_message(row, payload)
        from applypilot.unattended.cancellation import require_running
        require_running()
        phase = "intent"
        guard.mark_side_effect_intent(attempt_id, conn=conn, email_payload_hash=row["payload_hash"],
                                      email_transport="gmail_browseros" if is_browser else "gmail_mcp")
        row = _delivery(conn, attempt_id, route_id)
        phase = "send"
        if is_browser:
            await mailbox.send_prepared(row, payload)
            async with asyncio.timeout(60):
                return await _reconcile(conn, mailbox, row)
        response = await sender.send(raw)
        _confirm(conn, row, response)
        return _commit_sent(conn, _delivery(conn, attempt_id))

    try:
        row = _delivery(conn, attempt_id, route_id)
        if row["state"] in {"sending", "uncertain", "sent"}:
            return await reconcile_email_application(attempt_id, conn=conn, browser=browser)
        if row.get("transport", "gmail_mcp") != "gmail_browseros":
            phase = "gmail_connection"
            try:
                async with AsyncExitStack() as stack:
                    async with asyncio.timeout(45):
                        mailbox = await stack.enter_async_context(open_feedback_mailbox())
                    return await send_using(mailbox)
            except Exception as exc:
                current = _delivery(conn, attempt_id, route_id)
                if (current["state"] not in {"registered", "prepared"}
                        or phase not in {"gmail_connection", "gmail_probe"}
                        or not _browser_fallback_allowed(exc)):
                    raise
                _request_browser(conn, attempt_id, route_id)
                row = _delivery(conn, attempt_id, route_id)
        # Reuse the application bridge instead of opening a second browser
        # session from the control MCP process.
        from applypilot.execution import current_task
        session_file = config.APP_DIR / "browseros-sessions" / (attempt_id + ".json")
        if browser is None and not current_task() and session_file.is_file():
            return {"status": "browser_fallback_required", "committed": False, "route_id": route_id,
                    "reason": "gmail_mcp_unavailable", "transport": "gmail_browseros",
                    "next_action": "Call browseros_neo.submit_email_application with this route_id. Do not open Gmail manually."}
        phase = "browser_connection"
        from applypilot.tracking.gmail_browser import open_gmail_browser
        address = email_address((config.load_profile().get("personal") or {}).get("email"))
        async with open_gmail_browser(address, browser=browser) as mailbox:
            return await send_using(mailbox)
    except BaseException as exc:
        current = _delivery(conn, attempt_id, route_id) if row else None
        if current and current["state"] in {"sending", "uncertain", "sent"}:
            if current["state"] == "sent":
                result = _commit_sent(conn, current)
            else:
                result = _uncertain(conn, current, type(exc).__name__)
        else:
            _event("email_application_blocked", attempt_id, outcome=type(exc).__name__)
            raise
        if not isinstance(exc, Exception):
            raise
        if result["committed"]:
            return result
        raise


def run_email_job(job, worker_id, attempt_id, dry_run=False):
    """Legacy launcher adapter; dry runs perform no DB or mailbox operations."""
    start = time.monotonic()
    if dry_run:
        try:
            parse_email_target(job.get("application_url") or job.get("target") or job.get("url"))
            _event("email_application_dry_run", attempt_id, outcome="route_valid")
            return "skipped", int((time.monotonic() - start) * 1000), []
        except ValueError:
            _event("email_application_dry_run", attempt_id, outcome="invalid_email_route")
            return "failed:invalid_email_route", int((time.monotonic() - start) * 1000), []
    conn = get_connection()
    try:
        attempt = guard.attempt_status(attempt_id, conn=conn)
        if attempt["job_url"] != job["url"]:
            raise guard.GuardRejected("email input does not match the identified application")
        row = conn.execute("SELECT * FROM application_email_deliveries WHERE attempt_id=?",
                           (attempt_id,)).fetchone()
        if not row:
            registered = register_route(attempt_id, attempt["target"],
                evidence={"source": "stored-email-route", "attempt_id": attempt_id,
                          "posting_url": attempt["job_url"]}, conn=conn)
            route_id = registered["route_id"]
        else:
            route_id = row["route_id"]
        result = asyncio.run(submit_email_application(attempt_id, route_id, conn=conn))
        status = ("guarded_applied" if result["committed"] else "guarded_verification"
                  if result["status"] == "verification_required" else "failed:" + result["reason"])
    finally:
        close_connection()
    return status, int((time.monotonic() - start) * 1000), []

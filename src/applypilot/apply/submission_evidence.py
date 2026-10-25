"""Forward-only completion evidence captured by the owning browser adapter."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

KIND = "browseros-owned-page-completion-v1"
COMPLETION = re.compile(
    r"(?:your application (?:to [^\n.!?]{1,180} )?(?:has been |was )?(?:successfully )?(?:submitted|received|sent)"
    r"|we(?: have|'ve) received your application|thank you for (?:your application|applying)"
    r"|application (?:successfully submitted|submitted successfully)|\bapplication submitted(?=[!.\n]|$)"
    r"|you(?: have|'ve) successfully applied|application status\s*[:\-]?\s*submitted"
    r"|wir haben (?:deine|ihre) bewerbung erhalten"
    r"|(?:deine|ihre) bewerbung wurde erfolgreich (?:gesendet|eingereicht)"
    r"|(?:tu|su) (?:solicitud|postulaci[oó]n) (?:ha sido |fue )?enviada(?: correctamente| con [eé]xito)?)",
    re.I,
)


def install_capture_schema(conn):
    """Provider receipts survive an Activity moving to another machine."""
    conn.execute("""CREATE TABLE IF NOT EXISTS application_submission_evidence (
        receipt_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL,
        kind TEXT NOT NULL, body_json TEXT NOT NULL, body_sha256 TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        FOREIGN KEY(attempt_id) REFERENCES application_attempts(attempt_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS application_capture_attempt "
                 "ON application_submission_evidence(attempt_id,captured_at)")


def _save_capture(value):
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import transaction
    from applypilot.apply.guard import GuardRejected, _load_attempt
    from applypilot.orchestration.contracts import digest
    try:
        conn = get_connection()
        with transaction(conn):
            attempt = _load_attempt(conn, value['attempt_id'])
            if (attempt['status'] not in {'side_effect_intent', 'verification_required', 'verification_quarantined'}
                    or attempt['target'] != value['target']
                    or attempt['browser_capture_invocation'] != value['invocation']
                    or attempt['side_effect_intent_at'] != value['intent_at']
                    or hashlib.sha256(attempt['snapshot_json'].encode()).hexdigest() != value['snapshot_sha256']):
                raise GuardRejected('Captured receipt does not match the durable submission intent')
            conn.execute("""INSERT INTO application_submission_evidence
                (receipt_id,attempt_id,kind,body_json,body_sha256,captured_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(receipt_id) DO NOTHING""",
                (value['token'],value['attempt_id'],value['kind'],json.dumps(value,sort_keys=True),
                 digest(value),value['captured_at']))
    finally:
        close_connection()


def _read_capture(attempt_id, *, proof=None, conn=None):
    from applypilot.database import get_connection
    from applypilot.orchestration.contracts import digest
    conn = conn or get_connection()
    sql = 'SELECT body_json,body_sha256 FROM application_submission_evidence WHERE attempt_id=?'
    params = [attempt_id]
    if proof:
        sql += ' AND receipt_id=?'
        params.append(proof)
    row = conn.execute(sql + ' ORDER BY captured_at DESC,receipt_id DESC LIMIT 1', params).fetchone()
    if row is not None:
        value = json.loads(row['body_json'])
        if digest(value) != row['body_sha256']:
            raise ValueError('Durable submission receipt content is inconsistent')
        return value
    # Legacy file captures remain useful evidence. Absence is never negative proof.
    from applypilot.apply.browseros_mcp import read_state
    return read_state(evidence_path(attempt_id))

def require_baseline(page, bound_pages, previous_completion, completion_baseline_at, target) -> datetime:
    from applypilot.apply.guard import GuardRejected
    if type(page) is not int or bound_pages.get(str(page)) != target:
        raise GuardRejected("page was not bound to the reserved target before submit intent")
    if previous_completion.get(str(page)) is not False:
        raise GuardRejected("completion baseline is missing or the page already showed success; submission is not permitted")
    try:
        baseline_at = datetime.fromisoformat(completion_baseline_at[str(page)])
        if baseline_at.tzinfo is None or baseline_at > datetime.now(UTC):
            raise ValueError("invalid baseline time")
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardRejected("completion baseline time is missing or invalid") from exc
    return baseline_at


def evidence_path(attempt_id):
    from applypilot import config
    return config.APP_DIR / "submission-evidence" / (attempt_id + ".json")


def origin(url):
    value = urlsplit(url)
    if value.scheme != "https" or not value.hostname or value.username or value.password:
        raise ValueError("completion evidence requires an HTTPS application origin")
    return value.scheme, value.hostname, value.port or 443


def _valid_completion_evidence(value: dict) -> bool:
    detector = value.get("detector")
    completion = value.get("completion")
    if detector is None:
        return isinstance(completion, str) and bool(COMPLETION.fullmatch(completion))
    return False


async def capture(attempt_id, page, bound_pages, previous_completion, completion_baseline_at,
                  owned_pages, run, invocation):
    from applypilot.apply.guard import attempt_status, GuardRejected
    from applypilot.apply.browseros_mcp import write_state
    attempt = await asyncio.to_thread(attempt_status, attempt_id)
    if attempt["status"] != "side_effect_intent":
        raise GuardRejected("capture requires recorded submit intent")
    baseline_at = require_baseline(page, bound_pages, previous_completion, completion_baseline_at, attempt["target"])
    try:
        intent_at = datetime.fromisoformat(attempt["side_effect_intent_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardRejected("submit intent time is missing or invalid") from exc
    if intent_at.tzinfo is None or baseline_at > intent_at:
        raise GuardRejected("completion baseline was not established before submit intent")
    pages = await owned_pages()
    if not any(p.get("pageId") == page for p in pages):
        raise GuardRejected("completion page is not owned by this browser connection")
    # Fixed adapter code, never a model-supplied URL, script, proof or boolean.
    result = await run('const info=await browser.pages.getInfo(' + json.dumps(page) + ');'
                       'const text=await browser.read(' + json.dumps(page) + ');return {info,text};')
    info = result.get("info") if isinstance(result, dict) else None
    text = result.get("text") if isinstance(result, dict) else None
    if not isinstance(info, dict) or not isinstance(text, str):
        raise GuardRejected("owned page completion observation is unavailable")
    origin(info.get("url", ""))  # A normal ATS completion redirect may change origin.

    match = COMPLETION.search(text)
    if not match:
        raise GuardRejected(
            "The browser has not captured application completion. Wait and capture again; "
            "if still unresolved, preserve the visible evidence and require verification. Never click again to obtain proof."
        )

    evidence = {"kind": KIND, "token": uuid4().hex, "attempt_id": attempt_id,
                "invocation": invocation, "page_id": page, "target": attempt["target"],
                "url": info["url"], "captured_at": datetime.now(UTC).isoformat(),
                "intent_at": attempt["side_effect_intent_at"],
                "snapshot_sha256": hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest(),
                "completion": match.group(0),
                "page_excerpt": text[max(0, match.start() - 500):match.end() + 1000],
                "page_text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    await asyncio.to_thread(write_state, evidence_path(attempt_id), evidence)
    await asyncio.to_thread(_save_capture, evidence)
    return {"proof_kind": KIND, "proof": evidence["token"]}


def validate_existing_capture(attempt, proof_kind, proof, *, conn=None):
    """Validate a recorded outcome against its reservation, intent and browser invocation."""
    from applypilot.apply.guard import GuardRejected
    from applypilot.apply.browseros_mcp import read_state
    from applypilot import config
    if proof_kind != KIND or not re.fullmatch(r"[a-f0-9]{32}", proof):
        raise GuardRejected("adapter-captured completion evidence is required; model prose is not proof")
    value = _read_capture(attempt["attempt_id"], proof=proof, conn=conn)
    bridge_file = config.APP_DIR / "browseros-sessions" / (attempt["attempt_id"] + ".json")
    bridge = read_state(bridge_file)
    try:
        now = datetime.now(UTC)
        captured = datetime.fromisoformat(value["captured_at"])
        intent = datetime.fromisoformat(attempt["side_effect_intent_at"])
        checks = {
            "proof token mismatch": value["kind"] == KIND and value["token"] == proof,
            "reservation mismatch": value["attempt_id"] == attempt["attempt_id"],
            "target mismatch": value["target"] == attempt["target"],
            "submit intent mismatch": value["intent_at"] == attempt["side_effect_intent_at"],
            "snapshot mismatch": value["snapshot_sha256"] == hashlib.sha256(attempt["snapshot_json"].encode()).hexdigest(),
            "browser invocation mismatch": bool(value["invocation"]) and value["invocation"] == (
                dict(attempt).get("browser_capture_invocation") or bridge.get("invocation")),
            "capture outside submission interval": intent <= captured <= now,
            "invalid completion message": _valid_completion_evidence(value),
        }
        origin(value["url"])
        failed = [reason for reason, valid in checks.items() if not valid]
        if failed:
            raise ValueError("; ".join(failed))
    except (KeyError, TypeError, ValueError) as exc:
        raise GuardRejected(f"completion capture rejected: {exc}") from exc
    return json.dumps(value, sort_keys=True)


def reconcile_captured_submission(attempt_id: str, *, conn=None) -> dict:
    """Consume only existing trusted capture; never navigate, click or send."""
    from applypilot.apply.guard import attempt_status, mark_submitted, GuardRejected
    attempt = attempt_status(attempt_id, conn=conn)
    if attempt["status"] == "submitted":
        return {"disposition": "confirmed", "attempt": attempt, "evidence_refs": []}
    if attempt.get("browser_runtime") != "browseros" or attempt["status"] not in {
        "side_effect_intent", "verification_required", "verification_quarantined",
    }:
        return {"disposition": "unknown", "evidence_refs": []}
    try:
        value = _read_capture(attempt_id, conn=conn)
        result = mark_submitted(attempt_id, proof_kind=value["kind"], proof=value["token"], conn=conn)
    except (GuardRejected, KeyError, TypeError, ValueError):
        return {"disposition": "unknown", "evidence_refs": []}
    return {"disposition": "confirmed", "attempt": result,
            "evidence_refs": ["application_attempts:" + attempt_id]}

"""Parse application evidence and record diagnostic history."""
from __future__ import annotations

from applypilot import operations as ops

import json
import logging
import re
import time


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result/output parsers
# ---------------------------------------------------------------------------

def _submission_report_from_output(output: str) -> dict | None:
    """Recover an affirmative completion statement from the agent's final message."""
    from applypilot.apply.submission_evidence import COMPLETION

    final = output.strip()
    # Quoted examples and instructions are not assertions by the agent.
    prose = re.sub(
        r'"[^"]*"|“[^”]*”|‘[^’]*’|`+[^`]*`+'
        r"|(?<!\w)'[^'\n]*'(?!\w)",
        "<quoted>", final,
    )
    assertion = (
        r"(?:(?:the|this|your|my) )?application (?:has been |was |is )?"
        r"(?:successfully )?(?:submitted|sent|received)(?: successfully)?"
        r"|I (?:have )?(?:successfully )?(?:submitted|sent) (?:the|your|this) application(?: successfully)?"
        r"|successfully submitted|RESULT:(?:APPLIED|SUCCESS)"
    )
    for statement in re.split(r"[.!;\n]+|\s+[—–]\s+", prose):
        statement = statement.strip(" \t*")
        if COMPLETION.fullmatch(statement) or re.fullmatch(assertion, statement, re.I):
            return {"status": "submitted", "reason": final, "proof": statement, "current_url": ""}
    return None


def _parse_account_created(output: str, job_url: str | None = None) -> None:
    """Parse ACCOUNT_CREATED lines from agent output and save to DB."""
    from applypilot.database import get_connection, store_account
    for line in output.split("\n"):
        if "ACCOUNT_CREATED:" not in line:
            continue
        try:
            json_str = line.split("ACCOUNT_CREATED:", 1)[1].strip()
            account = json.loads(json_str)
            conn = get_connection()
            store_account(conn, account, job_url=job_url)
            logger.info("Saved new account: %s @ %s",
                        account.get("email"), account.get("domain"))
        except Exception as e:
            logger.warning("Failed to parse ACCOUNT_CREATED line: %s", e)


def _parse_qa_lines(output: str, job_url: str | None = None,
                    ats_slug: str | None = None) -> int:
    """Parse QA: lines from agent output and store in qa_knowledge DB.

    Format: QA:{question}|{answer}|{field_type}

    Returns count of Q&A pairs stored.
    """
    from applypilot.database import store_qa
    count = 0
    for line in output.split("\n"):
        if not line.strip().startswith("QA:"):
            continue
        try:
            payload = line.split("QA:", 1)[1].strip()
            parts = payload.split("|")
            if len(parts) < 2:
                continue
            question = parts[0].strip()
            answer = parts[1].strip()
            field_type = parts[2].strip() if len(parts) > 2 else None
            if question and answer:
                store_qa(question, answer, source="agent",
                         field_type=field_type, ats_slug=ats_slug,
                         job_url=job_url)
                count += 1
        except Exception as e:
            logger.debug("Failed to parse QA line: %s", e)
    if count:
        logger.info("Stored %d Q&A pair(s) from agent output", count)
    return count


# ---------------------------------------------------------------------------
# Worker history (depends on launcher's shared _worker_state)
# ---------------------------------------------------------------------------

def _record_job_history(worker_id: int, job: dict, result: str,
                        duration_ms: int) -> None:
    """Append a completed job entry to the worker's in-memory history list.

    Shown on the per-worker homepage (http://localhost:{7380+worker_id}/).
    Keeps the last 50 entries; oldest are dropped automatically.
    """
    import hashlib
    text_result = str(result).lower()
    outcome = ("skipped" if any(x in text_result for x in ("already_applied", "expired", "ineligible"))
               else "success" if "applied" in text_result else "failed")
    ops.record(component_name="applications", operation="attempt_result", stage="applications", kind="finish",
               outcome=outcome, severity="error" if outcome == "failed" else "info",
               message=result, duration=max(0, duration_ms or 0) / 1000,
               task_id=job.get("_attempt_id") or "",
               job_title=job.get("title") or "", job_company=job.get("company") or "",
               details={"attempt_id": job.get("_attempt_id") or "", "worker_id": worker_id,
                        "origin": "apply.result_handlers._record_job_history"},
               job_id=hashlib.sha256(str(job.get("url", "")).encode()).hexdigest()[:24])
    # Lazy import to avoid circular dependency: launcher.py owns the
    # _worker_state dict (also touched by the always-on HTTP server and by
    # the orchestrator), and launcher.py re-exports this function.
    from applypilot.apply import launcher
    with launcher._worker_state_lock:
        ws = launcher._worker_state.get(worker_id)
    if ws is None:
        return
    history: list = ws.setdefault("history", [])
    # Classify result into a display category
    if "applied" in result.lower():
        outcome = "applied"
    elif "expired" in result.lower():
        outcome = "expired"
    elif "already_applied" in result.lower():
        outcome = "already_applied"
    else:
        outcome = "failed"
    history.append({
        "ts": time.time(),
        "title": job.get("title", ""),
        "company": job.get("company") or job.get("site", ""),
        "url": job.get("application_url") or job.get("url", ""),
        "score": job.get("fit_score", 0),
        "result": result,
        "outcome": outcome,
        "duration_s": round(duration_ms / 1000) if duration_ms else 0,
    })
    if len(history) > 50:
        del history[:-50]

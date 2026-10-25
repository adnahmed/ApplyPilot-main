"""Exact-artifact employer context for document learning, never outcome labels.

No mailbox access or sends. Reads committed, non-retracted inbox events and the
immutable submission snapshot. Missing historical bindings are left unknown.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import logging
from pathlib import Path
from applypilot import postgres
import time

from .learning_store import capturing, database, dumps, last_example

log = logging.getLogger(__name__)


def bind_document(task: str, job_url: str, text: str, *, layout=None) -> None:
    """Bind only the exact successful inference in this thread/context."""
    captured = last_example.get()
    if not capturing() or not job_url or not captured or captured["task"] != task:
        return
    try:
        with database() as conn:
            row = conn.execute("SELECT output FROM examples WHERE id=? AND task=?", (captured["id"], task)).fetchone()
            # Repeated identical prompts are deduplicated by the corpus. Do not
            # bind a new/different response to the old retained response.
            if row is None or row[0] != captured["output"]:
                return
            conn.execute('INSERT INTO document_examples VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                (captured["id"], task, hashlib.sha256(job_url.encode()).hexdigest(),
                 hashlib.sha256(text.encode("utf-8")).hexdigest(), dumps(layout or {}), time.time()))
    except Exception as exc:
        log.warning("Document learning attribution unavailable (%s)", type(exc).__name__)


def contexts_for(rows: list[dict], *, main_path: Path | None = None) -> tuple[dict, dict]:
    """Snapshot a bounded set of quote-backed events for this offline round.

    Source/quote grounding is performed by feedback_inbox. We preserve its trust
    and event identity; we do not reinterpret feedback as verified applicant facts.
    Retraction takes effect on the next round because events are never cached here.
    """
    from applypilot import config
    from applypilot.tracking.feedback_store import obj, utc
    wanted = {row["id"] for row in rows if row["task"] in {"tailor_resume", "cover_letter"}}
    if not wanted:
        return {}, {"state": "not_applicable", "linked_examples": 0}
    with database() as conn:
        bindings = [dict(row) for row in conn.execute(
            "SELECT d.* FROM document_examples d JOIN examples e ON e.id=d.example_id WHERE e.consumed=0")
            if row["example_id"] in wanted]
    by_artifact = defaultdict(list)
    for row in bindings:
        by_artifact[(row["job_key"], row["artifact_sha256"])].append(row)
    if not by_artifact:
        return {}, {"state": "no_exact_artifact_bindings", "linked_examples": 0}
    path = Path(main_path or config.DB_PATH)
    contexts = defaultdict(list)
    conn = None
    try:
        conn = postgres.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
        conn.row_factory = postgres.Row
        conn.raw.execute("SET default_transaction_read_only=on")
        conn.execute("BEGIN")
        events = conn.execute("""SELECT e.event_key,e.event_type,e.occurred_at,e.payload_json,e.trust,
                  s.job_url,s.submitted_at,s.features_json,s.snapshot_origin
            FROM application_outcomes e JOIN feedback_submissions s
              ON s.submission_id=e.submission_identifier AND s.job_url=e.job_url
            WHERE e.voided_at IS NULL AND e.source='email' AND e.trust IN ('automatic-v1','manual')
              AND s.snapshot_origin='guarded'
              AND ap_day(e.occurred_at)>=ap_day('now','-30 days')
            ORDER BY e.id DESC LIMIT 1000""").fetchall()
        for event in events:
            payload, features = obj(event["payload_json"]), obj(event["features_json"])
            if not isinstance(payload, dict) or not isinstance(features, dict):
                continue
            feedback = payload.get("feedback")
            if not isinstance(feedback, list):
                continue
            quotes = [{"theme": str(item.get("theme") or "unspecified")[:100], "quote": item["quote"][:600]}
                      for item in feedback if isinstance(item, dict)
                      and isinstance(item.get("quote"), str) and item["quote"].strip()][:3]
            if not quotes:
                continue  # Rejection alone is not feedback on a document.
            submitted = utc(event["submitted_at"]).timestamp()
            if utc(event["occurred_at"]).timestamp() < submitted:
                continue
            job_key = hashlib.sha256(event["job_url"].encode()).hexdigest()
            artifacts = features.get("artifact_hashes", {})
            if not isinstance(artifacts, dict):
                continue
            for kind, task in (("resume", "tailor_resume"), ("cover_letter", "cover_letter")):
                artifact = artifacts.get(kind) or {}
                files = artifact.get("files", {}) if isinstance(artifact, dict) else {}
                if not isinstance(files, dict):
                    continue
                for digest in set(value for value in files.values() if isinstance(value, str)):
                    for binding in by_artifact.get((job_key, digest), []):
                        target = contexts[binding["example_id"]]
                        if (binding["task"] != task or binding["created"] > submitted or len(target) >= 3
                                or any(item["event_id"] == event["event_key"] for item in target)):
                            continue
                        target.append({"event_id": event["event_key"], "event_type": event["event_type"],
                            "occurred_at": event["occurred_at"], "trust": event["trust"], "quotes": quotes,
                            "artifact_sha256": digest, "layout": obj(binding["layout_json"]),
                            "scope": "employer_statement_not_verified_fact; document causation unknown"})
        return dict(contexts), {"state": "linked" if contexts else "no_quote_backed_matches", "linked_examples": len(contexts)}
    except (OSError, postgres.Error, ValueError, TypeError):
        return {}, {"state": "feedback_snapshot_unavailable", "linked_examples": 0}
    finally:
        if conn is not None:
            conn.close()

"""Persist generated application answers without changing canonical applicant facts."""

from __future__ import annotations

from datetime import UTC, datetime

from applypilot.database import get_connection, question_key, write_with_retry
from applypilot.apply.answer_context import (
    is_placeholder_answer,
)


def _persist(job_url: str, question: str, answer: str, provenance: str, conn) -> dict:
    now = datetime.now(UTC).isoformat()
    key = question_key(question)
    conn.execute(
        """
        INSERT INTO application_answers
            (job_url, question_key, question_text, answer_text, provenance,
             created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_url, question_key) DO UPDATE SET
            question_text=excluded.question_text,
            answer_text=excluded.answer_text,
            provenance=excluded.provenance,
            updated_at=excluded.updated_at
        """,
        (job_url, key, question, answer, provenance, now, now),
    )
    return {
        "question": question,
        "answer": answer,
        "provenance": provenance,
        "question_key": key,
    }


def save_generated_answer(job_url: str, question: str, answer: str) -> dict:
    """Persist one live completion-first answer without altering profile data."""
    if not question.strip() or is_placeholder_answer(answer):
        raise ValueError("Supply the question and one concrete generated answer")
    conn = get_connection()
    result = {}
    def persist():
        result.update(_persist(job_url, question.strip(), answer.strip(), "generated", conn))
    write_with_retry(conn, persist)
    return result

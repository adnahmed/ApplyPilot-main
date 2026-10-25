"""Metadata-only learning history. Temporal owns execution visibility."""
from __future__ import annotations

import json
import logging

from .learning_store import database

log = logging.getLogger(__name__)


def saved_status() -> dict:
    with database() as conn:
        row = conn.execute("SELECT report FROM rounds ORDER BY started DESC LIMIT 1").fetchone()
    if row is None:
        return {"state": "not_observed", "execution": "temporal"}
    report = json.loads(row["report"])
    keys = ("state", "task", "learning_run_id", "dataset_id", "program_id", "optimizer_run_id",
            "phase", "optimizer", "error_type", "reason_code")
    return {"execution": "temporal", **{key: report[key] for key in keys if key in report}}


def publish(result: dict) -> None:
    log.info("Learning %s: %s", result.get("learning_run_id", ""), result.get("state", "unknown"))

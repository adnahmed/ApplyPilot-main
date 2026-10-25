"""Portable instruction state, without corpus, jobs, clients, pickle or executable code.

Deployed revisions retain schema/model/routing compatibility and rollback lineage.
Trusting an import is an owner decision, NOT a new held-out evaluation. GitHub
checkpoints are readable only through the private backup repository.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
from applypilot import postgres

from .settings import DSPY_TASKS

FORMAT = "applypilot-policy-v1"
CHECKPOINT = "learning/policies.json"
MAX_BYTES = 5_000_000
MAX_REVISIONS = 5000
REVISION = re.compile(r"[0-9a-f]{24}")
DIGEST = re.compile(r"[0-9a-f]{64}")
STATES = {"candidate", "active", "retired", "rolled_back"}


class PolicyError(ValueError):
    """Safe, bounded owner-facing diagnostic; never includes prompt content."""


def _directory(workspace: Path | None = None) -> Path:
    from applypilot import config
    from .learning_store import root
    if workspace is None or Path(workspace).resolve() == config.APP_DIR:
        return root()
    return Path(workspace) / "learning"


def _canonical(value) -> bytes:
    from .learning_store import dumps
    return dumps(value).encode("utf-8")


def bundle_fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _read_connection(directory: Path):
    path = directory / "learning.pgstore"
    if path.is_symlink():
        raise PolicyError("The learning database must not be a symlink")
    conn = postgres.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    conn.row_factory = postgres.Row
    conn.execute("BEGIN")
    return conn


def export_bundle(*, workspace: Path | None = None, task: str | None = None) -> dict:
    """Snapshot policy tables only; do not create an empty corpus merely to export."""
    from .settings import artifact_path
    if task is not None and task not in DSPY_TASKS:
        raise PolicyError("Choose a registered DSPy task")
    selected = {task} if task else DSPY_TASKS
    directory = _directory(workspace)
    revisions, artifacts = [], {}
    if (directory / "learning.pgstore").exists():
        conn = _read_connection(directory)
        try:
            tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname=current_schema()")}
            if "revisions" in tables:
                # Include deployed history and its ancestors, but not unrelated
                # candidates, raw observations, holdout rows or optimizer logs.
                rows = {r["revision"]: dict(r) for r in conn.execute("SELECT * FROM revisions") if r["task"] in selected}
                keep = {rid for rid, row in rows.items() if row["state"] != "candidate"}
                pending = list(keep)
                while pending:
                    parent = rows[pending.pop()]["parent"]
                    if parent and parent not in keep:
                        if parent not in rows:
                            raise PolicyError("Policy rollback history is incomplete")
                        keep.add(parent)
                        pending.append(parent)
                revisions = [rows[rid] | {"evaluation": json.loads(rows[rid]["evaluation"])} for rid in sorted(keep)]
            if "policy_artifacts" in tables:
                artifacts = {r["task"]: dict(r) for r in conn.execute("SELECT * FROM policy_artifacts") if r["task"] in selected}
        finally:
            conn.close()
    for name in sorted(selected):
        if name in artifacts:
            continue
        from applypilot import config
        path = (Path(workspace) / "ai-programs" / f"{name}.json"
                if workspace is not None and Path(workspace).resolve() != config.APP_DIR else artifact_path(name))
        if not path.exists():
            continue
        if path.is_symlink() or path.stat().st_size > MAX_BYTES:
            raise PolicyError("Unsafe or oversized legacy instruction artifact")
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") != 1 or data.get("task_kind") != name:
            raise PolicyError("Unsupported legacy instruction artifact")
        artifacts[name] = {"task": name, "schema_hash": data.get("schema_sha256"), "instructions": data.get("instructions")}
    return validate({"format": FORMAT, "revisions": revisions, "artifacts": [artifacts[k] for k in sorted(artifacts)]})


def _instruction(row: dict) -> None:
    if row.get("task") not in DSPY_TASKS:
        raise PolicyError("Policy contains an unsupported task")
    if not isinstance(row.get("schema_hash"), str) or not DIGEST.fullmatch(row["schema_hash"]):
        raise PolicyError("Policy contains an invalid schema digest")
    text = row.get("instructions")
    if not isinstance(text, str) or not text.strip() or len(text) > 24000 or "\x00" in text:
        raise PolicyError("Instructions must be nonempty text of at most 24000 characters")


def validate(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"format", "revisions", "artifacts"} or value["format"] != FORMAT:
        raise PolicyError("Unsupported policy bundle; use an ApplyPilot policy JSON export")
    if not isinstance(value["revisions"], list) or not isinstance(value["artifacts"], list):
        raise PolicyError("Invalid policy collection")
    if len(value["revisions"]) > MAX_REVISIONS or len(value["artifacts"]) > len(DSPY_TASKS):
        raise PolicyError("Too many policies; export a single task instead")
    rows, active, tasks = {}, set(), set()
    for row in value["revisions"]:
        if not isinstance(row, dict) or set(row) != {"revision", "task", "schema_hash", "instructions", "evaluation", "state", "parent", "created"}:
            raise PolicyError("Invalid policy revision fields")
        _instruction(row)
        rid = row["revision"]
        if not isinstance(rid, str) or not REVISION.fullmatch(rid) or rid in rows:
            raise PolicyError("Invalid or duplicate policy revision")
        if row["state"] not in STATES or not isinstance(row["evaluation"], dict):
            raise PolicyError("Invalid revision state or evaluation metadata")
        if row["parent"] is not None and (not isinstance(row["parent"], str) or not REVISION.fullmatch(row["parent"])):
            raise PolicyError("Invalid rollback parent")
        if type(row["created"]) not in (int, float) or not math.isfinite(row["created"]) or row["created"] < 0:
            raise PolicyError("Invalid policy timestamp")
        # This is integrity checking, not authenticity: a trusted owner can
        # change content and recalculate its ID. Imports still need --trust.
        expected = hashlib.sha256((row["task"] + row["schema_hash"] + row["instructions"]).encode() + _canonical(row["evaluation"])).hexdigest()[:24]
        if rid != expected:
            raise PolicyError("Policy content does not match its revision digest")
        scope = (row["task"], row["schema_hash"])
        if row["state"] == "active":
            if scope in active:
                raise PolicyError("More than one active revision for a task/schema")
            if row["evaluation"].get("approved") is not True or row["evaluation"].get("critical_ok") is not True:
                raise PolicyError("An active revision lacks its recorded passing evaluation")
            active.add(scope)
        rows[rid] = row
    complete = set()
    for rid in rows:
        chain = set()
        current = rid
        while current and current not in complete:
            if current in chain or current not in rows:
                raise PolicyError("Cyclic or incomplete policy rollback history")
            chain.add(current)
            row = rows[current]
            parent = row["parent"]
            if parent in rows and (rows[parent]["task"], rows[parent]["schema_hash"]) != (row["task"], row["schema_hash"]):
                raise PolicyError("Rollback parent belongs to a different task/schema")
            current = parent
        complete.update(chain)
    for row in value["artifacts"]:
        if not isinstance(row, dict) or set(row) != {"task", "schema_hash", "instructions"}:
            raise PolicyError("Invalid legacy instruction fields")
        _instruction(row)
        if row["task"] in tasks:
            raise PolicyError("Duplicate legacy instruction task")
        tasks.add(row["task"])
    try:
        size = len(_canonical(value))
    except (ValueError, OverflowError, RecursionError):
        raise PolicyError("Policy contains invalid JSON values") from None
    if size + 1 > MAX_BYTES:
        raise PolicyError("Policy bundle is too large; export a single task instead")
    return value


def load(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise PolicyError("Choose a regular policy JSON file no larger than 5 MB")
    try:
        return validate(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise PolicyError("Policy file is not valid UTF-8 JSON") from None


def export_file(path: Path, *, task: str | None = None, force: bool = False) -> dict:
    from .source_files import SourceFiles
    data = export_bundle(task=task)
    if path.is_symlink() or (path.exists() and not force):
        raise PolicyError("Output already exists; choose a new path or use --force")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if len(text.encode("utf-8")) > MAX_BYTES:
        text = _canonical(data).decode("utf-8") + "\n"
    SourceFiles._write(path, text)
    return {"exported": len(data["revisions"]), "artifacts": len(data["artifacts"]), "output": str(path),
            "corpus_included": False, "note": "Plaintext instructions may contain private facts; keep this file private."}


def install_bundle(value: dict, *, trust: bool = False, replace: bool = False,
                   workspace: Path | None = None, restore: bool = False,
                   expected_fingerprint: str | None = None) -> dict:
    from applypilot import config
    from applypilot.backup.common import lock, BackupBusyError
    if restore:
        from applypilot.onboarding.storage import read_json
        if workspace is None or read_json(Path(workspace) / "github_sync" / "restore-active.json", {}).get("format") != "applypilot-restore-account-v1":
            raise PolicyError("Recovery policy installation requires an active workspace restore journal")
        # Recovery owns the locks and has captured the learning store for rollback.
        return _install_bundle(value, trust=trust, replace=replace, workspace=workspace, restore=True)
    owning_root = Path(workspace) if workspace is not None else config.APP_DIR
    try:
        with lock(owning_root, "lifecycle"), lock(owning_root, "policies"):
            if (owning_root / "github_sync" / "restore-active.json").exists():
                raise PolicyError("Finish or roll back recovery before importing policies")
            if expected_fingerprint is not None and expected_fingerprint != bundle_fingerprint(export_bundle(workspace=workspace)):
                raise PolicyError("Installed policies changed since this preview. Preview the file again")
            return _install_bundle(value, trust=trust, replace=replace, workspace=workspace)
    except BackupBusyError:
        raise PolicyError("Optimization or recovery is active. Pause optimization and let the current round stop before importing.") from None


def _install_bundle(value: dict, *, trust: bool = False, replace: bool = False,
                    workspace: Path | None = None, restore: bool = False) -> dict:
    """Import only policy tables in one transaction; unrelated live data is untouched."""
    from applypilot import config
    from .learning_store import database, dumps
    if not trust:
        raise PolicyError("Review the file and pass --trust to accept its instructions and recorded deployment decisions")
    data = validate(value)
    directory = _directory(workspace)
    scopes = {(row["task"], row["schema_hash"]) for row in data["revisions"]}
    winners = {(row["task"], row["schema_hash"]): row["revision"] for row in data["revisions"] if row["state"] == "active"}
    with database(directory) as conn:
        conn.execute("BEGIN")
        conn.advisory_lock("learning-policy")
        for row in data["revisions"]:
            old = conn.execute("SELECT * FROM revisions WHERE revision=?", (row["revision"],)).fetchone()
            if old and (any(old[k] != row[k] for k in ("task", "schema_hash", "instructions", "parent"))
                        or dumps(json.loads(old["evaluation"])) != dumps(row["evaluation"])):
                raise PolicyError("A revision ID conflicts with existing policy content")
        for task, schema in scopes:
            old = conn.execute("SELECT revision FROM revisions WHERE task=? AND schema_hash=? AND state='active'", (task, schema)).fetchone()
            if old and old[0] != winners.get((task, schema)) and not replace:
                raise PolicyError("A different policy is active; use --replace to replace selected task deployments")
        for row in data["artifacts"]:
            old = conn.execute("SELECT * FROM policy_artifacts WHERE task=?", (row["task"],)).fetchone()
            if old and dict(old) != row and not replace:
                raise PolicyError("A different imported artifact exists; use --replace to replace it")
            if not old and not replace:
                from .settings import artifact_path
                path = (Path(workspace) / "ai-programs" / f"{row['task']}.json"
                        if workspace is not None and Path(workspace).resolve() != config.APP_DIR else artifact_path(row["task"]))
                if path.exists():
                    if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                        raise PolicyError("Unsafe or oversized existing instruction artifact")
                    legacy = json.loads(path.read_text(encoding="utf-8"))
                    if legacy.get("schema_sha256") != row["schema_hash"] or legacy.get("instructions") != row["instructions"]:
                        raise PolicyError("A different disk artifact exists; use --replace to replace its effective policy")
        if restore:
            conn.execute("DELETE FROM policy_artifacts")
            conn.execute("UPDATE revisions SET state='retired' WHERE state='active'")
        else:
            for task, schema in scopes:
                conn.execute("UPDATE revisions SET state='retired' WHERE task=? AND schema_hash=? AND state='active'", (task, schema))
        for row in data["revisions"]:
            conn.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(revision) DO UPDATE SET state=excluded.state",
                         (row["revision"], row["task"], row["schema_hash"], row["instructions"], dumps(row["evaluation"]),
                          row["state"], row["parent"], row["created"]))
        for row in data["artifacts"]:
            conn.execute('INSERT INTO policy_artifacts VALUES (?,?,?) ON CONFLICT (task) DO UPDATE SET schema_hash=excluded.schema_hash, instructions=excluded.instructions', (row["task"], row["schema_hash"], row["instructions"]))
    if restore:
        # The checkpoint is authoritative for legacy fallback instructions too.
        # They now live in the transactional table; do not keep stale disk fallbacks.
        for task in DSPY_TASKS:
            (Path(workspace) / "ai-programs" / f"{task}.json").unlink(missing_ok=True)
    else:
        changed()
    return {"imported": len(data["revisions"]), "active": len(winners), "artifacts": len(data["artifacts"]),
            "corpus_changed": False, "note": "Recorded policies restored, not reevaluated. Schema/model/routing compatibility is still enforced."}


def imported_artifact(task: str, schema: dict) -> str | None:
    from .artifacts import schema_digest
    directory = _directory()
    if not (directory / "learning.pgstore").exists():
        return None
    conn = _read_connection(directory)
    try:
        if not conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename='policy_artifacts'").fetchone():
            return None
        row = conn.execute("SELECT instructions FROM policy_artifacts WHERE task=? AND schema_hash=?", (task, schema_digest(schema))).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def changed() -> None:
    """Deployment stays successful if the network is offline; scheduler retries."""
    from applypilot import config
    from applypilot.backup.runtime import request_checkpoint
    request_checkpoint(config.APP_DIR)

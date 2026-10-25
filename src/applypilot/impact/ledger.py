"""Impact ledger repository.

Interface plus a JSON-backed adapter with crash-safe persistence and an
explicit corruption/self-healing protocol:

Atomic write sequence (every mutation):
  1. validate(new_data) — never persist something that can't load back
  2. write to ``impact_ledger.json.<pid>.tmp`` to stay process-local
  3. flush() + os.fsync() on the tmp fd
  4. copy current primary -> ``impact_ledger.json.bak`` (only if primary exists)
     then os.fsync() the bak fd
  5. os.replace(tmp, primary) — atomic rename
  6. os.fsync() the parent directory so the rename itself is durable

Corruption protocol (every load):
  - file missing                        -> empty ledger, read-write
  - primary corrupt, .bak valid         -> serve .bak + surface a warning
  - primary + .bak both corrupt          -> EMPTY READ-ONLY state; the corrupt
    primary is preserved as ``impact_ledger.json.corrupt-<timestamp>`` and
    every destructive mutation refuses until ``repo.recover()``/``repo.reset()``.
  - pipeline consumers must fail open: ``list_records()`` returns ``[]``
    (never raises) so applyjobs keep flowing while the ledger is broken.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from io import TextIOWrapper
from pathlib import Path

from applypilot import config
from applypilot.impact.models import (
    ImpactRecord,
    _now_iso,
)

SCHEMA_VERSION = 1
_LEDGER_FILENAME = "impact_ledger.json"


class LedgerCorruptionError(IOError):
    """Raised when a destructive mutation is attempted in read-only recovery
    state (both primary and backup corrupt)."""


class LedgerValidationError(ValueError):
    """Raised when ``save`` receives a record that fails model validation."""


@dataclass
class LedgerStatus:
    path: Path
    exists: bool
    state: str                # ok | recovered_from_bak | readonly
    records: int
    warning: str = ""
    readonly_reason: str = ""


def _validate_ledger(data: dict) -> list[str]:
    """Schema-level validation of a whole ledger dict; returns error strings."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["ledger root is not an object"]
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append(
            f"unsupported schema_version {data.get('schema_version')!r} "
            f"(expected {SCHEMA_VERSION})"
        )
    if not isinstance(data.get("career_context", {}), dict):
        errors.append("career_context must be an object")
    records = data.get("records")
    if not isinstance(records, list):
        errors.append("ledger.records is not a list")
        return errors
    seen: set[str] = set()
    for i, raw in enumerate(records):
        if not isinstance(raw, dict):
            errors.append(f"ledger.records[{i}] is not an object")
            continue
        try:
            record = ImpactRecord.from_dict(raw)
            record_errors = record.validate()
        except ValueError as exc:
            errors.append(f"ledger.records[{i}]: {exc}")
            continue
        errors += [f"ledger.records[{i}].{e}" for e in record_errors]
        if record.id in seen:
            errors.append(f"duplicate record id {record.id!r}")
        seen.add(record.id)
    return errors


class ImpactLedgerRepository:

    def list_records(self) -> list[ImpactRecord]:
        raise NotImplementedError

    def get(self, record_id: str) -> ImpactRecord | None:
        raise NotImplementedError

    def save(self, record: ImpactRecord) -> None:
        raise NotImplementedError

    def delete(self, record_id: str) -> bool:
        raise NotImplementedError

    def replace_all(self, records: list[ImpactRecord]) -> None:
        raise NotImplementedError


def _serialized_mutation(method):
    """Coordinate CLI, setup and diary writes to prevent lost ledger updates."""
    from functools import wraps
    @wraps(method)
    def wrapped(self,*args,**kwargs):
        from applypilot.backup.common import lock
        with lock(self.path.parent,"impact-ledger"):
            self._cache=None
            return method(self,*args,**kwargs)
    return wrapped


class JsonImpactLedgerRepository(ImpactLedgerRepository):
    """JSON-backed adapter. Persistence JSON exists only here.

    Reads are cached keyed by (mtime, size) so the pipeline's per-job matcher
    does not re-parse the file every call, while still converging on disk
    edits before the next mutation.
    """

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else Path(config.IMPACT_LEDGER_PATH)
        self._readonly_reason: str = ""
        self.warning: str = ""
        self._career_context: dict = {}
        self._cache: tuple[int, int, dict] | None = None  # (mtime_ns, size, data)

    # ── Load ───────────────────────────────────────────────────────────────

    def _load(self, *, warn: bool = False) -> list[dict]:
        """Return raw record dicts, applying the corruption protocol.

        Never raises for missing/corrupt files; returns [] in those cases and
        records the state so mutations can refuse.
        """
        if not self.path.exists():
            self._career_context = {}
            self._readonly_reason = ""
            self.warning = ""
            return []
        try:
            stat = self.path.stat()
            if self._cache and self._cache[0] == stat.st_mtime_ns and self._cache[1] == stat.st_size:
                data = self._cache[2]
            else:
                raw = self.path.read_text(encoding="utf-8")
                data = json.loads(raw)
                self._cache = (stat.st_mtime_ns, stat.st_size, data)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            data = self._fallback_to_bak()
            if data is None:
                self._enter_readonly(str(exc))
                return []
            self.warning = "Ledger primary corrupt; serving impact_ledger.json.bak"
            if warn:
                print(f"[impact] {self.warning}")
            return data

        errors = _validate_ledger(data)
        if errors:
            # A file that fails schema validation is equally "corrupt".
            data = self._fallback_to_bak()
            if data is None:
                self._enter_readonly("; ".join(errors))
                return []
            self.warning = f"Ledger primary failed validation ({errors[0]}); serving .bak"
            if warn:
                print(f"[impact] {self.warning}")
            return data

        self._readonly_reason = ""
        self._career_context = dict(data.get("career_context") or {})
        return data.get("records") or []

    def _fallback_to_bak(self) -> list[dict] | None:
        bak = self._bak_path()
        if not bak.exists():
            return None
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            if _validate_ledger(data):
                return None
            self._career_context = dict(data.get("career_context") or {})
            return data.get("records") or []
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _enter_readonly(self, reason: str) -> None:
        self._readonly_reason = reason
        self.warning = f"Ledger unreadable ({reason}); pipeline fails open with empty records"
        # Preserve the corrupt primary so it is never silently overwritten.
        if self.path.exists():
            ts = time.strftime("%Y%m%d_%H%M%S")
            self._preserve_corrupt(ts)

    def _preserve_corrupt(self, ts: str) -> Path:
        dest = self.path.with_name(f"impact_ledger.json.corrupt-{ts}")
        if not dest.exists():
            try:
                shutil.copy2(self.path, dest)
            except OSError:
                pass
        return dest

    # ── Mutations ───────────────────────────────────────────────────────────

    @_serialized_mutation
    def save(self, record: ImpactRecord) -> None:
        record_errors = record.validate()
        if record_errors:
            raise LedgerValidationError(
                f"record {record.id or '<unset>'}: " + "; ".join(record_errors)
            )
        records = self._load()
        self._raise_if_readonly()
        next_records = [r for r in records if r.get("id") != record.id]
        next_records.append(record.to_dict())
        self._write(next_records)

    @_serialized_mutation
    def delete(self, record_id: str) -> bool:
        records = self._load()
        self._raise_if_readonly()
        if not any(r.get("id") == record_id for r in records):
            return False
        next_records = [r for r in records if r.get("id") != record_id]
        self._write(next_records)
        return True

    @_serialized_mutation
    def replace_all(self, records: list[ImpactRecord]) -> None:
        self._load()
        for record in records:
            record_errors = record.validate()
            if record_errors:
                raise LedgerValidationError(
                    f"record {record.id or '<unset>'}: " + "; ".join(record_errors)
                )
        self._raise_if_readonly()
        self._write([r.to_dict() for r in records])

    def career_context(self) -> dict:
        """Private source entities; callers must use the outbound projection for models/recruiters."""
        import copy
        self._load()
        return copy.deepcopy(self._career_context)

    @_serialized_mutation
    def merge_career(self, records: list[ImpactRecord], context: dict) -> None:
        """Atomic import. Never discard permanent rejections or override a local edit silently."""
        import copy
        existing = self._load()
        self._raise_if_readonly()
        by_id = {row["id"]: row for row in existing}
        for record in records:
            errors = record.validate()
            if errors:
                raise LedgerValidationError("; ".join(errors))
            previous = by_id.get(record.id)
            if previous:
                old = previous.get("career_detail") or {}
                new = record.career_detail or {}
                if old.get("source_revision") == new.get("source_revision"):
                    continue  # idempotent reimport; retains local confirmation/rejection
                raise LedgerValidationError(f"{record.id}: source changed; review it explicitly instead of overwriting local history")
            by_id[record.id] = record.to_dict()
        merged = copy.deepcopy(self._career_context)
        for key, value in context.items():
            if isinstance(value, list):
                previous = {item["id"]: item for item in merged.get(key, []) if isinstance(item, dict) and item.get("id")}
                for item in value:
                    if item.get("id") in previous and previous[item["id"]] != item:
                        raise LedgerValidationError(f"{key}/{item['id']}: conflicting source entity requires review")
                    previous[item["id"]] = copy.deepcopy(item)
                merged[key] = list(previous.values())
            elif key not in merged:
                merged[key] = copy.deepcopy(value)
            elif merged[key] != value and key not in {"positioning"}:
                raise LedgerValidationError(f"{key}: cannot combine different people's corpora")
        self._write(list(by_id.values()), career_context=merged)

    @_serialized_mutation
    def update_career(self, record_id: str, expected_revision: str, update) -> ImpactRecord:
        """Optimistic reviewed mutation under the ledger's cross-process lock."""
        from applypilot.impact.models import record_revision
        rows = self._load()
        self._raise_if_readonly()
        previous = next((r for r in rows if r.get("id") == record_id), None)
        if previous is None:
            raise KeyError(record_id)
        record = ImpactRecord.from_dict(previous)
        if record_revision(record) != expected_revision:
            raise LedgerValidationError("Record changed during review; reload before confirming")
        update(record, self._career_context)
        errors = record.validate()
        if errors:
            raise LedgerValidationError("; ".join(errors))
        self._write([record.to_dict() if row.get("id") == record_id else row for row in rows])
        return record

    def _raise_if_readonly(self) -> None:
        if self._readonly_reason:
            raise LedgerCorruptionError(
                "Ledger is in read-only recovery state (both primary and .bak "
                f"corrupt: {self._readonly_reason}). Run `applypilot impact recover` "
                "or `applypilot impact reset` to restore writes."
            )

    def _write(self, records: list[dict], *, career_context: dict | None = None) -> None:
        from applypilot.backup.safety import assert_start_allowed
        assert_start_allowed(self.path.parent)
        data = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now_iso(),
            "records": records,
            "career_context": self._career_context if career_context is None else career_context,
        }
        errors = _validate_ledger(data)
        if errors:
            raise LedgerValidationError("refusing to persist invalid ledger: " + "; ".join(errors))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(
            f"{_LEDGER_FILENAME}.{os.getpid()}.{uuid.uuid4().hex[:6]}.tmp"
        )
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with TextIOWrapper(os.fdopen(fd, "wb"), encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            # Snapshot the previous good primary into .bak before the swap.
            if self.path.exists():
                bak = self._bak_path()
                with os.fdopen(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as bak_fd:
                    os.chmod(bak, 0o600)
                    with open(self.path, "rb") as src:
                        shutil.copyfileobj(src, bak_fd)
                    bak_fd.flush()
                    os.fsync(bak_fd.fileno())
            os.replace(tmp, self.path)
            self._fsync_dir(self.path.parent)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
        self._cache = None
        from applypilot.backup.runtime import request_checkpoint
        request_checkpoint(self.path.parent)

    def _bak_path(self) -> Path:
        return self.path.with_name(f"{_LEDGER_FILENAME}.bak")

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        if not directory.exists():
            return
        try:
            fd = os.open(str(directory), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # ── Reads ───────────────────────────────────────────────────────────────

    def list_records(self, *, warn: bool = False) -> list[ImpactRecord]:
        raw = self._load(warn=warn)
        records: list[ImpactRecord] = []
        for item in raw:
            try:
                records.append(ImpactRecord.from_dict(item))
            except ValueError:
                continue
        return records

    def get(self, record_id: str) -> ImpactRecord | None:
        for record in self.list_records():
            if record.id == record_id:
                return record
        return None

    # ── Recovery ────────────────────────────────────────────────────────────

    @_serialized_mutation
    def recover(self) -> LedgerStatus:
        """Re-probe the file; exit read-only state when a valid ledger is now
        loadable (e.g. the operator repaired it, or restored the .bak)."""
        records = self._load(warn=False)
        if records is not None and not self._readonly_reason:
            self.warning = ""
            return self.status()
        if self._readonly_reason:
            self.warning = (
                "No valid ledger on disk; ledger stays read-only. "
                "Use `applypilot impact reset` to start empty."
            )
        return self.status()

    @_serialized_mutation
    def reset(self) -> LedgerStatus:
        """Recreate an empty ledger, preserving any corrupt file first."""
        if self.path.exists():
            try:
                self.path.rename(
                    self.path.with_name(
                        f"impact_ledger.json.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
                    )
                )
            except OSError:
                pass
        self._readonly_reason = ""
        self.warning = ""
        self._cache = None
        self._write([])
        return self.status()

    def status(self) -> LedgerStatus:
        records = self.list_records()
        if self._readonly_reason:
            state = "readonly"
        elif self.warning:
            state = "recovered_from_bak"
        else:
            state = "ok"
        return LedgerStatus(
            path=self.path,
            exists=self.path.exists(),
            state=state,
            records=len(records),
            warning=self.warning,
            readonly_reason=self._readonly_reason,
        )


_default_repo: JsonImpactLedgerRepository | None = None


def default_repository() -> JsonImpactLedgerRepository:
    global _default_repo
    if _default_repo is None:
        _default_repo = JsonImpactLedgerRepository()
    return _default_repo


def repository_for_path(path: Path) -> JsonImpactLedgerRepository:
    """Fresh repository bound to an explicit path (used by tests)."""
    return JsonImpactLedgerRepository(path)


def reset_default_repository() -> None:
    """Clear the process singleton (tests swap config paths between cases)."""
    global _default_repo
    _default_repo = None
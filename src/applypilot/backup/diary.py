"""Save the owner's original words first. Optional extraction uses the existing impact pipeline."""
from __future__ import annotations
import json
from pathlib import Path
import re
import secrets
from applypilot.onboarding.storage import now, read_json, write_json
from .common import lock


def save_note(root: Path, data: dict):
    if set(data) - {"note", "id"}: raise ValueError("Unknown diary field")
    text = data.get("note", "")
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError("Write a note between 1 and 16,000 characters")
    identifier = data.get("id") or secrets.token_hex(12)
    if not re.fullmatch(r"[0-9a-f]{24}", identifier): raise ValueError("Invalid diary entry ID")
    with lock(root, "diary"):
        if (root / "github_sync" / "restore-active.json").exists():
            raise ValueError("Recovery is in progress")
        path = root / "diary" / (identifier + ".json")
        if path.exists():
            row = read_json(path)
            if row["note"] != text.strip(): raise ValueError("That diary request ID already has different content")
        else:
            row = {"id": identifier, "created_at": now(), "note": text.strip(), "impact_state": "not_requested"}
            write_json(path, row)
    from .runtime import request_checkpoint
    request_checkpoint(root)
    return row


def _record_text(record):
    parts=[]
    if record.get("problem"): parts.append("Problem: " + str(record["problem"]))
    intervention=record.get("intervention") or {}
    if isinstance(intervention,dict):
        summary=intervention.get("summary") or intervention.get("description")
        if summary: parts.append("What I did: " + str(summary))
    for item in record.get("measurements",[]):
        if isinstance(item,dict):
            text=item.get("quantity_text") or item.get("source_text") or item.get("description") or item.get("claim") or item.get("metric")
            if text: parts.append("Measurement: " + str(text))
    for item in record.get("outcomes",[]):
        if isinstance(item,dict) and (item.get("claim") or item.get("description")):
            parts.append("Outcome: " + str(item.get("claim") or item["description"]))
    return "\n".join(parts) or json.dumps(record, ensure_ascii=False)



def _note_references(record):
    """Extract diary IDs only from provenance fields, not incidental prose."""
    ids = set()
    source_keys = {"source_ref", "source_refs", "source_id", "source_ids", "provenance"}

    def walk(value, in_source=False):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, in_source or key in source_keys)
        elif isinstance(value, list):
            for child in value:
                walk(child, in_source)
        elif in_source and isinstance(value, str):
            ids.update(re.findall(r"(?<![\w-])diary[:-]([0-9a-f]{24})(?![0-9a-f])", value))

    walk(record)
    return ids


def view(root: Path, q="", page=1, *, page_size=30):
    """One timeline: old impact records are entries, not an apparently empty diary.

    Link notes to extracted records only by explicit source/record identity. Never
    deduplicate merely similar prose, which could hide a distinct accomplishment.
    Storage formats stay intact for the CLI, backups and evidence pipeline.
    """
    query = str(q).casefold()[:200]
    notes=[]
    for path in (root / "diary").glob("*.json"):
        row=read_json(path,{})
        if isinstance(row,dict) and row.get("id"): notes.append(row)
    ledger=read_json(root/"impact_ledger.json",{},limit=128*1024**2)
    records=ledger.get("records",[]) if isinstance(ledger,dict) else []
    entries={row["id"]:{"id":row["id"],"kind":"note","title":row.get("note","").split("\n")[0][:110],
             "text":row.get("note",""),"created_at":row.get("created_at",""),
             "state":row.get("impact_state","not_requested"),"records":[],"note_id":row["id"]} for row in notes}
    for index,record in enumerate(records):
        if not isinstance(record,dict): continue
        linked = sorted(_note_references(record) & entries.keys())
        if linked:
            for key in linked:
                entries[key]["records"].append(record)
        else:
            identifier="record:"+str(record.get("id") or index)
            entries[identifier]={"id":identifier,"kind":"impact","title":str(record.get("problem") or "Accomplishment")[:110],
                "text":_record_text(record),"created_at":record.get("created_at") or record.get("updated_at") or "",
                "recency":record.get("recency", ""),"state":record.get("review_status","recorded"),"records":[record],"note_id":None}
    timeline=[row for row in entries.values() if not query or query in json.dumps(row,ensure_ascii=False).casefold()]
    timeline.sort(key=lambda r:(str(r.get("created_at", "")),r["id"]),reverse=True)
    page_size = max(1, len(timeline)) if page_size is None else max(1, int(page_size))
    page=max(1,int(page));start=(page-1)*page_size
    # Legacy API keys retained; the web view now uses entries and one pagination cursor.
    filtered_notes=[r for r in notes if not query or query in r.get("note", "").casefold()]
    filtered_records=[r for r in records if not query or query in json.dumps(r,ensure_ascii=False).casefold()]
    return {"entries":timeline[start:start+page_size],"total":len(timeline),
            "notes":filtered_notes[start:start+page_size],"notes_total":len(filtered_notes),
            "records":filtered_records[start:start+page_size],"records_total":len(filtered_records),
            "page":page,"page_size":page_size,
            "note":"Your existing impact ledger and new notes appear in the same diary; original storage and claim status are preserved."}


def extract(root: Path, data: dict, progress=lambda **kw:None):
    identifier = data.get("id", "")
    if not isinstance(identifier,str) or not re.fullmatch(r"[0-9a-f]{24}",identifier): raise ValueError("Invalid diary ID")
    if data.get("confirm_model") is not True: raise ValueError("Confirm using the configured model on this note")
    with lock(root, "impact-extract"):
        path = root / "diary" / (identifier + ".json")
        row = read_json(path, {})
        if not row: raise ValueError("Save the note first")
        if row.get("impact_state") == "completed": return row.get("impact_result", {})
        # The raw note survives model/network/dependency failure.
        write_json(path,row|{"impact_state":"running"})
        try:
            from applypilot import config
            config.load_env()
            from applypilot.database import init_db
            init_db()
            from applypilot.impact.draft import draft_records_from_source
            progress(message="Extracting source-grounded impact records from your saved note")
            result = draft_records_from_source(row["note"], source_name="diary-"+identifier, skip_interview=False, ask_max=0)
            # JSON-safe result, no generated credentials or shell output.
            from dataclasses import asdict, is_dataclass
            clean = json.loads(json.dumps(result, default=lambda value: asdict(value) if is_dataclass(value) else str(value)))
            write_json(path,row|{"impact_state":"completed","impact_result":clean})
            from .runtime import request_checkpoint
            request_checkpoint(root)
            return clean
        except Exception as exc:
            from applypilot.operations.runtime import sanitize
            write_json(path,row|{"impact_state":"failed","impact_error":sanitize(str(exc), 2000),
                                 "impact_error_type":type(exc).__name__})
            raise

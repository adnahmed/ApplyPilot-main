#!/usr/bin/env python3
"""Read local response logs; no network, database access, or credentials needed.

Examples:
  python tools/inspect_llm_responses.py --last 3
  python tools/inspect_llm_responses.py --call-id <32-character-id>
  python tools/inspect_llm_responses.py --last 20 --summary

The output may contain personal model-generated content. Inspect before sharing.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
import os
from pathlib import Path
import re
import sys


def read_records(workspace: Path):
    directory = workspace / "logs" / "llm-responses"
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"No local response-log directory found: {directory}. Logs start after an updated LiteLLM call.")
    records, skipped = [], 0
    for path in sorted(directory.glob("llm-*.jsonl")):
        if path.is_symlink():
            continue
        try:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        if isinstance(value, dict) and re.fullmatch(r"[a-f0-9]{32}", str(value.get("call_id", ""))):
                            value["_log_file"] = path.name
                            records.append(value)
                    except (ValueError, TypeError):
                        skipped += 1  # A currently appended final line may be incomplete.
        except (OSError, UnicodeError):
            skipped += 1
    return records, skipped


def inspect_records(records, *, call_id=None, last=3, summary=False):
    grouped = defaultdict(list)
    for record in records:
        grouped[record["call_id"]].append(record)
    ids = [call_id] if call_id else sorted(grouped, key=lambda key: max(str(r.get("at", "")) for r in grouped[key]))[-last:]
    selected = []
    for key in ids:
        rows = sorted(grouped.get(key, []), key=lambda r: str(r.get("at", "")))
        if not rows:
            continue
        observed = {r.get("event") for r in rows}
        item = {"call_id": key, "stage": rows[0].get("stage"), "job_id": rows[0].get("job_id"),
                "model_requested": rows[0].get("model_requested"), "last_event": rows[-1].get("event"),
                "request_recorded": "request" in observed, "response_recorded": "response" in observed,
                "last_at": rows[-1].get("at")}
        if not summary:
            item["records"] = rows
        if "response" not in observed:
            item["note"] = "No response retained for this call. It may still be running, have failed/cancelled before a body arrived, or its evidence may be disabled/lost/pruned."
        selected.append(item)
    return selected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workspace", type=Path, default=Path(os.environ.get("APPLYPILOT_DIR", str(Path.home() / ".applypilot"))))
    parser.add_argument("--call-id", help="Exact call ID shown in Workers or the normal log")
    parser.add_argument("--last", type=int, default=3, help="Latest calls across all processes (default: 3)")
    parser.add_argument("--summary", action="store_true", help="Only call status/identifiers, not response bodies")
    args = parser.parse_args(argv)
    if args.last < 1 or (args.call_id and not re.fullmatch(r"[a-f0-9]{32}", args.call_id)):
        parser.error("--last must be positive; --call-id must contain exactly 32 lowercase hex characters")
    try:
        rows, skipped = read_records(args.workspace)
        result = inspect_records(rows, call_id=args.call_id, last=args.last, summary=args.summary)
        if not result:
            print("No matching retained model calls. Old responses cannot be reconstructed retroactively.", file=sys.stderr)
            return 1
        print(json.dumps({"calls": result, "unreadable_or_incomplete_records": skipped}, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

#!/usr/bin/env python3
"""Inspect shared allocations; --repair reconciles only after production stops.

No job is retried, no browser is killed, and no submission reservation or evidence
file is deleted. Existing conservative governor checks decide each release.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
from applypilot import postgres
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repair", action="store_true", help="Reconcile safe allocations; production must be stopped")
    parser.add_argument("--lease", action="append", help="Limit repair to this exact lease token; may be repeated")
    args = parser.parse_args(argv)
    from applypilot import config
    from applypilot.processes import process_guard, runtime_owned
    from applypilot.resource_governor import ResourceGovernor

    if not config.DB_PATH.is_file():
        parser.error("No existing workspace database; check APPLYPILOT_DIR")
    governor = ResourceGovernor(config.DB_PATH)
    with governor.reader() as conn:
        if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='resource_leases'").fetchone():
            print(json.dumps({"state":"resource_schema_not_initialized"}, indent=2))
            return 0
        rows = [dict(row) for row in conn.execute(
            "SELECT token,lane,owner_pid,runtime_id,execution_state,launch_phase,launch_started_at,"
            "children_json,CASE WHEN browser_session IS NOT NULL THEN 1 ELSE 0 END has_session,"
            "CASE WHEN reservation_id IS NOT NULL THEN 1 ELSE 0 END has_reservation "
            "FROM resource_leases")]
    print(json.dumps({"mode":"inspect", "allocations":rows}, indent=2))
    if not args.repair:
        return 0
    names = ("autopilot", "apply-runtime")
    if any(runtime_owned(config.APP_DIR, name) for name in names):
        parser.error("Stop Autopilot and the application runtime before --repair; no lease was modified")
    # Hold both startup guards for the complete repair. Their presence may make
    # legacy-runtime cleanup more conservative, which is intentional.
    with ExitStack() as stack:
        for name in names:
            stack.enter_context(process_guard(config.APP_DIR / (name+".guard")))
        result = governor.reconcile_inactive_leases(lease_tokens=args.lease)
    print(json.dumps({"mode":"repair", "reconciliation":result}, indent=2))
    return 2 if result.get("status") == "busy" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, postgres.Error, ValueError, RuntimeError) as error:
        print(f"Recovery did not complete: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(2)

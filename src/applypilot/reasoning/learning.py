"""Manage trace-driven DSPy learning without touching live browser actions.

Examples:
  python -m applypilot.reasoning.learning enable --auto-promote --allow-judged-promotion
  python -m applypilot.reasoning.learning import-run /path/to/debug.json
  python -m applypilot.reasoning.learning status
  python -m applypilot.reasoning.learning watch
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from .learning_store import (database, dumps, label_example, promote, prune,
                             rollback, save_settings, settings, status)

log = logging.getLogger(__name__)


def once(task=None):
    """Submit learning to Temporal; this client never executes an optimizer."""
    import asyncio
    from applypilot.orchestration.client import run_work
    return asyncio.run(run_work("learning", {"learning_task": task} if task else {}))


def resume_learning():
    import asyncio
    from applypilot.orchestration.client import change_schedules
    return asyncio.run(change_schedules("resume", names=["learning"]))


def main(argv=None):
    from applypilot.config import load_env
    load_env()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="Metadata only; no prompts or applicant data")
    enable = commands.add_parser("enable", help="Allow private captured evidence to be evaluated by the configured gateway")
    enable.add_argument("--auto-promote", action=argparse.BooleanOptionalAction, default=None)
    enable.add_argument("--allow-judged-promotion", action=argparse.BooleanOptionalAction, default=None,
                        help="Accept model-judged / next-action-proxy evaluations, not verified live success")
    commands.add_parser("defaults", help="Clear saved learning overrides; use environment defaults without deleting learned policies")
    policy_out = commands.add_parser("export-policy", help="Export portable policies only, not the private corpus")
    policy_out.add_argument("output", type=Path)
    policy_out.add_argument("--task")
    policy_out.add_argument("--force", action="store_true")
    policy_in = commands.add_parser("import-policy", help="Import trusted policies without replacing application data")
    policy_in.add_argument("path", type=Path)
    policy_in.add_argument("--trust", action="store_true")
    policy_in.add_argument("--replace", action="store_true")
    commands.add_parser("disable")
    execute = commands.add_parser("once")
    execute.add_argument("--task")
    scenarios = commands.add_parser("evaluate-browser", help="Compare policies in authored resettable scenarios; no live browser actions or promotion")
    scenarios.add_argument("path", type=Path, help="Versioned JSON scenario bundle")
    scenarios.add_argument("--revision", required=True, help="Existing browser policy revision to compare with the active policy")
    commands.add_parser("watch")
    imported = commands.add_parser("import-run")
    imported.add_argument("path", type=Path)
    label = commands.add_parser("label")
    label.add_argument("example_id")
    label.add_argument("checks", type=Path, help="JSON array of grounded owner checks")
    examples = commands.add_parser("export-examples")
    examples.add_argument("task")
    examples.add_argument("output", type=Path)
    deploy = commands.add_parser("promote")
    deploy.add_argument("revision")
    deploy.add_argument("--allow-judged", action="store_true")
    revert = commands.add_parser("rollback")
    revert.add_argument("task")
    revert.add_argument("schema_hash")
    args = parser.parse_args(argv)
    try:
        if args.command == "status":
            result = status()
        elif args.command == "enable":
            from .learning_store import mode_defaults
            defaults = mode_defaults()
            result = save_settings({"enabled": True,
                                    "auto_promote": defaults["auto_promote"] if args.auto_promote is None else args.auto_promote,
                                    "allow_judged_promotion": defaults["allow_judged_promotion"] if args.allow_judged_promotion is None else args.allow_judged_promotion})
        elif args.command == "defaults":
            from .learning_store import configure_mode
            result = configure_mode({"mode": "defaults", "confirm": True})
        elif args.command == "export-policy":
            from .policies import export_file
            result = export_file(args.output, task=args.task, force=args.force)
        elif args.command == "import-policy":
            from .policies import install_bundle, load
            result = install_bundle(load(args.path), trust=args.trust, replace=args.replace)
        elif args.command == "disable":
            result = save_settings({"enabled": False, "auto_promote": False})
        elif args.command == "import-run":
            from .trajectories import import_bundle
            result = import_bundle(args.path)
        elif args.command == "evaluate-browser":
            from .browser_scenarios import evaluate_file
            result = evaluate_file(args.path, args.revision)
        elif args.command == "label":
            label_example(args.example_id, json.loads(args.checks.read_text(encoding="utf-8")))
            result = {"labeled": args.example_id}
        elif args.command == "promote":
            promote(args.revision, allow_judged=args.allow_judged)
            result = {"promoted": args.revision}
        elif args.command == "rollback":
            result = {"restored": rollback(args.task, args.schema_hash)}
        elif args.command == "export-examples":
            if args.output.exists():
                raise ValueError("Use a new private output path")
            with database() as conn:
                rows = [dict(r) for r in conn.execute("SELECT * FROM examples WHERE task=? AND consumed=0", (args.task,))]
                from .settings import BROWSER_TASKS
                if args.task in BROWSER_TASKS:
                    from .trajectories import hydrate_history
                    for row in rows:
                        row["messages"] = dumps(hydrate_history(json.loads(row["messages"]), row["trace_id"], conn))
            from .source_files import SourceFiles
            args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            SourceFiles._write(args.output, "\n".join(dumps(row) for row in rows)+"\n")
            result = {"exported": len(rows), "private_data": True}
        elif args.command == "watch":
            result = resume_learning()
        else:
            result = once(args.task)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("state") == "failed" else 0
    except Exception as exc:
        # Provider/framework exceptions can contain applicant details. Local
        # trace metadata identifies the failed boundary without exporting them.
        from .policies import PolicyError
        error = {"state": "failed", "error_type": type(exc).__name__}
        if isinstance(exc, PolicyError):
            error["error"] = str(exc)
        print(json.dumps(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

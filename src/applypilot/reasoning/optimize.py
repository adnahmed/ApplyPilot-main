"""Explicit offline GEPA compilation; never invoked by the live pipeline.

python -m applypilot.reasoning.optimize --help
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile

from .artifacts import schema_digest
from .settings import DSPY_TASKS, WORKFLOW_TASKS, RLMSettings, rlm_enabled


def _json_file(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _dataset(path: Path):
    from .metrics import validate_checks

    rows = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict) or set(row) != {"group", "messages", "checks"}:
                raise ValueError("Dataset rows must contain group, messages, checks")
            if not isinstance(row["group"], str) or not row["group"].strip():
                raise ValueError("Each row needs a source/job group identifier")
            messages = row["messages"]
            if not isinstance(messages, list) or not messages:
                raise ValueError("Each row needs original text messages")
            if any(not isinstance(m, dict) or set(m) != {"role", "content"}
                   or m["role"] not in {"system", "developer", "user", "assistant"}
                   or not isinstance(m["content"], str) for m in messages):
                raise ValueError("Dataset messages must be text-only role/content pairs")
            validate_checks(row["checks"])
            rows.append(row)
    if len(rows) < 2:
        raise ValueError("Each split needs at least two labeled examples")
    return rows


def _disjoint(splits):
    seen_groups, seen_messages = set(), set()
    for rows in splits:
        groups = {r["group"] for r in rows}
        messages = {hashlib.sha256(json.dumps(r["messages"], sort_keys=True).encode()).hexdigest() for r in rows}
        if groups & seen_groups or messages & seen_messages:
            raise ValueError("Training, validation and holdout splits overlap; keep related jobs in one group")
        seen_groups.update(groups)
        seen_messages.update(messages)


def _write_json(path: Path, value):
    # Output directories should be private; candidate instructions may memorize
    # examples even though raw examples and traces are not explicitly exported.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".ai-candidate-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            raise ValueError("Refusing to overwrite an existing candidate/report")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    from .settings import BROWSER_TASKS
    parser.add_argument("--task", choices=sorted(DSPY_TASKS - BROWSER_TASKS), required=True)
    parser.add_argument("--schema", type=Path, required=True, help="Exact JSON Schema used by the live call site")
    parser.add_argument("--train", type=Path, required=True)
    parser.add_argument("--validation", type=Path, required=True, help="GEPA candidate-selection split")
    parser.add_argument("--holdout", type=Path, required=True, help="Separate split; never passed to GEPA")
    parser.add_argument("--output", type=Path, required=True, help="New private candidate path; not auto-deployed")
    parser.add_argument("--metric-calls", type=int, default=40)
    parser.add_argument("--max-tokens", type=int, default=4096, help="Match the live task's output limit")
    parser.add_argument("--temperature", type=float, default=0.0, help="Match the live task's sampling temperature")
    parser.add_argument("--min-improvement", type=float, default=0.01)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--compare-direct", action="store_true",
                        help="Also measure the same unoptimized instructions without RLM on holdout")
    args = parser.parse_args(argv)
    try:
        if (args.metric_calls < 1 or args.max_tokens < 1
                or not 0 <= args.temperature <= 2 or not 0 < args.min_improvement <= 1):
            raise ValueError("Invalid optimization budget/sampling/improvement limits")
        output = args.output.expanduser().resolve()
        report_path = output.with_suffix(".report.json")
        if output == report_path or output.exists() or report_path.exists():
            raise ValueError("Use a new candidate .json path (not a .report.json path)")
        schema = _json_file(args.schema)
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(schema)
        # Keep compilation schemas self-contained; never fetch remote references.
        def check_refs(node):
            if isinstance(node, dict):
                for key in ("$ref", "$dynamicRef"):
                    if key in node and not str(node[key]).startswith("#"):
                        raise ValueError("Optimization schemas must use only local references")
                for item in node.values():
                    check_refs(item)
            elif isinstance(node, list):
                for item in node:
                    check_refs(item)
        check_refs(schema)
        splits = [_dataset(p) for p in (args.train, args.validation, args.holdout)]
        _disjoint(splits)
        from .prompt_templates import instruction_seed, prompt_schema
        initial_messages = splits[0][0]["messages"]
        seed = instruction_seed(initial_messages)
        if seed is None:
            raise ValueError("Use captured examples with explicit task instruction slots")
        schema = prompt_schema(initial_messages, schema)
        for split in splits:
            for row in split:
                if instruction_seed(row["messages"]) != seed:
                    raise ValueError("Compile one instruction template at a time")
                prompt_schema(row["messages"], schema)
        if args.task in WORKFLOW_TASKS:
            from . import workflow_input
            if args.temperature != 0:
                raise ValueError("Live workflow tasks use temperature=0")
            for split in splits:
                for row in split:
                    workflow_input(row["messages"])
        if args.metric_calls <= len(splits[1]):
            raise ValueError("Metric-call allowance must exceed one full validation pass")
        from applypilot.config import load_env
        load_env()
        import asyncio
        from applypilot.orchestration.client import run_work
        from applypilot.orchestration.connection import connect
        from .learning_compile import artifact
        imported = {"task": args.task, "schema": schema,
            "splits": dict(zip(("train", "validation", "holdout"), splits)),
            "max_tokens": args.max_tokens, "temperature": args.temperature,
            "metric_calls": args.metric_calls, "min_improvement": args.min_improvement,
            "seed": args.seed, "compare_direct": args.compare_direct}

        async def execute():
            submitted = await run_work("learning", {"learning_import": imported})
            print(json.dumps(submitted), flush=True)
            client = await connect()
            return await client.get_workflow_handle(submitted["workflow_id"],
                run_id=submitted.get("run_id")).result()

        outcome = asyncio.run(execute())
        run_id = outcome.get("learning_run_id")
        if not run_id:
            raise ValueError("Learning workflow produced no evaluated dataset")
        report = artifact(run_id, "learning_publish")
        optimized = artifact(run_id, "learning_optimize")
        _write_json(report_path, report)
        if report.get("approved"):
            _write_json(output, {"version": 1, "task_kind": args.task,
                "schema_sha256": schema_digest(schema), "instructions": optimized["instructions"],
                "dspy_version": "3.3.1", "evaluation": report})
        print(json.dumps(report, indent=2))
        return 0 if report.get("approved") else 2
    except Exception as exc:
        # Framework/provider exception text can contain applicant data. Print
        # only the class here; inspect sensitive diagnostics locally as needed.
        logging.getLogger(__name__).error("GEPA compilation aborted (%s); no automatic deployment", type(exc).__name__)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

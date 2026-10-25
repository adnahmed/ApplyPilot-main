"""Learning business phases. Temporal alone retries and orders their execution."""
from __future__ import annotations

from .contracts import Observation, digest

PHASES = frozenset({"learning_collect", "learning_build_dataset", "learning_evaluate_current",
                    "learning_optimize", "learning_evaluate_candidate", "learning_publish"})


def _import_cohort(value):
    from applypilot.llm import get_client, LLMTask
    from applypilot.reasoning.learning_store import dumps
    from applypilot.reasoning.settings import routing_fingerprint, DSPY_TASKS, BROWSER_TASKS
    from applypilot.reasoning.artifacts import schema_digest
    from applypilot.reasoning.optimize import _disjoint
    from jsonschema import Draft202012Validator

    task, schema = value["task"], value["schema"]
    if task not in DSPY_TASKS - BROWSER_TASKS:
        raise ValueError("Unsupported imported learning task")
    Draft202012Validator.check_schema(schema)
    _disjoint(list(value["splits"].values()))
    client_task = {"score": "general", "signal_roles": "fast", "factual_review": "reasoning",
        "tailor_resume": "reasoning", "cover_letter": "general", "outcome_classify": "fast",
        "network_reply": "fast", "interview_brief": "reasoning", "browser_observation": "code"}.get(task, "general")
    params = {"client_task": client_task, "model": get_client(LLMTask(client_task)).model,
        "structured": False, "routing": routing_fingerprint(), "max_tokens": value["max_tokens"],
        "temperature": value["temperature"], "prompt_contract": True}
    splits = {name: [{"id": digest([task, schema, row]), "group_id": digest(["import", row["group"]]),
        "messages": dumps(row["messages"]), "schema_json": dumps(schema), "checks": dumps(row["checks"]),
        "trace_id": ""} for row in rows] for name, rows in value["splits"].items()}
    return {"task": task, "schema_hash": schema_digest(schema), "params": params, "splits": splits,
        "cohort_id": digest([task, schema, splits]), "optimizer": "gepa", "imported": True,
        "compare_direct": value.get("compare_direct", False),
        "settings": {"metric_calls": value["metric_calls"], "min_improvement": value["min_improvement"],
                     "seed": value.get("seed", 0)}}


def _observation(value, **extra):
    fields = ("learning_run_id", "dataset_id", "program_id", "optimizer_run_id",
              "candidate_program_id", "task", "optimizer", "revision", "approved")
    return Observation(value["state"], {key: value[key] for key in fields if key in value} | extra)


def execute(request, stop, heartbeat):
    from applypilot.database import get_connection
    from applypilot.reasoning import learning_compile as learning
    from applypilot.reasoning.learning_store import settings, prune
    from . import store

    conn = get_connection()
    value = store.get(conn, request.reference)
    if request.phase == "learning_collect":
        from applypilot.reasoning.trajectories import materialize_cases
        materialize_cases()
        prune()
        imported = value.get("options", {}).get("learning_import")
        if not settings()["enabled"] and not imported:
            return Observation("capture_only")
        task = value.get("options", {}).get("learning_task")
        cohorts = ([_import_cohort(imported)] if imported else
            [row for row in learning.cohort_rows() if task is None or row["task"] == task])
        selection = {"kind": "learning_collection", "cohorts": cohorts,
                     "learning_run_id": digest([request.options["workspace"], request.options["sweep"]])}
        reference = store.put(conn, selection)
        conn.commit()
        return Observation("collected", {"reference": reference, "cohorts": len(cohorts),
                                         "wait_for_result": bool(imported)})
    if request.phase == "learning_build_dataset":
        if value.get("kind") == "learning_collection":
            if request.sequence >= len(value["cohorts"]):
                return Observation("complete")
            cohort = value["cohorts"][request.sequence]
            run_id = digest([value["learning_run_id"], cohort["cohort_id"]])
            reference = store.put(conn, {"kind": "learning_cohort", "cohort": cohort,
                                         "learning_run_id": run_id})
            conn.commit()
            return Observation("cohort_selected", {"reference": reference,
                "program_scope": cohort["cohort_id"], "learning_run_id": run_id})
        if value.get("kind") != "learning_cohort":
            raise ValueError("Learning dataset requires an immutable collection")
        run_id = value["learning_run_id"]
        dataset = learning.build_dataset(value["cohort"], run_id)
        if dataset["state"] != "dataset_ready":
            return _observation(dataset)
        reference = store.put(conn, {"kind": "learning_run", "learning_run_id": run_id})
        conn.commit()
        return _observation(dataset, reference=reference)
    if request.phase not in PHASES or value.get("kind") != "learning_run":
        raise ValueError("Unsupported learning phase or input")
    heartbeat(request.phase)
    return _observation(learning.run_phase(value["learning_run_id"], request.phase, stop))

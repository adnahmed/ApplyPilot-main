"""Resettable, explicitly authored browser workflow simulations.

Only declared state transitions produce observations. Historical continuations
are never replayed after an action with unknown consequences. Simulation results
are diagnostic evidence and cannot promote a policy or verify a live submission.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from pathlib import Path

from jsonschema import Draft202012Validator

from .learning_store import ACTION_SCHEMA, dumps, offline
from .settings import BROWSER_TASKS


def _local_schema(schema):
    Draft202012Validator.check_schema(schema)

    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$ref", "$dynamicRef"} and (not isinstance(child, str) or not child.startswith("#")):
                    raise ValueError("Scenario schemas may reference only local definitions")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)

    check(schema)


def load_scenarios(path: Path):
    if path.stat().st_size > 10 * 1024**2:
        raise ValueError("Scenario bundle exceeds 10 MiB")
    bundle = json.loads(path.read_text(encoding="utf-8"))
    if bundle.get("version") != 1 or not isinstance(bundle.get("scenarios"), list):
        raise ValueError("Expected a version 1 scenario bundle")
    if not 1 <= len(bundle["scenarios"]) <= 20:
        raise ValueError("Use between 1 and 20 scenarios")
    identities = set()
    from .application_policy import replace_policy
    for scenario in bundle["scenarios"]:
        identity = scenario.get("id", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", identity) or identity in identities:
            raise ValueError("Scenario IDs must be unique non-personal identifiers")
        identities.add(identity)
        initial = scenario["initial_state"]
        task = initial.get("task_kind", "application_agent")
        if task not in BROWSER_TASKS or not isinstance(initial.get("history"), list):
            raise ValueError("Scenario requires a browser task and initial observed history")
        replace_policy(initial["prompt"], "Validate policy slot")
        if not isinstance(initial.get("model"), str) or not initial["model"]:
            raise ValueError("Scenario requires the captured execution model")
        if not isinstance(initial.get("system_prompt"), str):
            raise ValueError("Scenario requires the captured system prompt")
        if task != "application_assist" and not initial.get("skill_files"):
            raise ValueError("Scenario requires captured browser skills")
        if not isinstance(scenario.get("schema_hash"), str) or not isinstance(scenario.get("routing"), str):
            raise ValueError("Scenario requires captured schema and routing fingerprints")
        catalog = {f"mcp__{server}__{tool['name']}": tool
                   for server, tools in initial["tools"].items() for tool in tools}
        for tool in catalog.values():
            _local_schema(tool["inputSchema"])
        states = scenario["states"]
        if not isinstance(states, dict) or not 1 <= len(states) <= 200 or scenario["start"] not in states:
            raise ValueError("Scenario requires a valid start and 1 to 200 states")
        if any(not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", name) for name in states):
            raise ValueError("State names must be non-personal identifiers")
        if type(scenario.get("max_steps")) is not int or not 1 <= scenario["max_steps"] <= 40:
            raise ValueError("Scenario max_steps must be between 1 and 40")
        terminal_count = 0
        for node in states.values():
            if "outcome" in node:
                if node["outcome"] not in {"completed", "blocked", "failed"} or node.get("transitions"):
                    raise ValueError("Terminal states require a known outcome and no transitions")
                if type(node.get("critical_ok")) is not bool:
                    raise ValueError("Terminal states require an explicit critical_ok label")
                terminal_count += 1
            else:
                if not isinstance(node.get("transitions"), list) or not node["transitions"]:
                    raise ValueError("Nonterminal states require authored transitions")
                for transition in node["transitions"]:
                    if transition["tool"] not in catalog or transition["next"] not in states:
                        raise ValueError("Transition references an unknown tool or state")
                    _local_schema(transition["arguments_schema"])
                    if "observation" not in transition or type(transition.get("is_error", False)) is not bool:
                        raise ValueError("Transitions require an observation and a boolean error flag")
        if not terminal_count or "outcome" in states[scenario["start"]]:
            raise ValueError("Scenario must start before a decision and have a terminal state")
    return bundle["scenarios"]


def run_scenario(scenario, instructions, budget):
    from .replay import choose_action
    from .trajectories import diagnostics
    state = copy.deepcopy(scenario["initial_state"])
    node_id = scenario["start"]
    catalog = {f"mcp__{server}__{tool['name']}": (server, tool)
               for server, tools in state["tools"].items() for tool in tools}
    steps = []
    actions = 0

    def result(outcome, critical_ok=None):
        return {"outcome": outcome, "critical_ok": critical_ok, "steps": actions,
                "state_path": steps, "behavior_signals": diagnostics(state["history"])["signals"]}

    for _ in range(scenario["max_steps"]):
        budget.remaining()
        actions += 1
        action = json.loads(choose_action(state, instructions, budget=budget))
        if not Draft202012Validator(ACTION_SCHEMA).is_valid(action) or action["tool"] not in catalog:
            return result("invalid_action", False)
        server, tool = catalog[action["tool"]]
        if not Draft202012Validator(tool["inputSchema"]).is_valid(action["arguments"]):
            return result("invalid_action", False)
        matches = [transition for transition in scenario["states"][node_id]["transitions"]
                   if transition["tool"] == action["tool"]
                   and Draft202012Validator(transition["arguments_schema"]).is_valid(action["arguments"])]
        if len(matches) != 1:
            # An incomplete/ambiguous world model is not evidence of agent failure.
            return result("unmodeled_action" if not matches else "ambiguous_transition")
        transition = matches[0]
        state["history"].append({"server": server, "tool": tool["name"], "arguments": action["arguments"],
                                 "observation": copy.deepcopy(transition["observation"]),
                                 "is_error": transition.get("is_error", False)})
        node_id = transition["next"]
        steps.append(node_id)
        node = scenario["states"][node_id]
        if "outcome" in node:
            return result(node["outcome"], node["critical_ok"])
    return result("step_limit")


def evaluate_file(path: Path, revision: str):
    from .application_policy import contract_schema, selected
    from .artifacts import schema_digest
    from .budget import InferenceContext
    from .learning_store import compatible_revision, database, settings
    from .settings import routing_fingerprint
    from .telemetry import current_trace
    from applypilot.work_context import stage_context

    scenarios = load_scenarios(path)
    with database() as conn:
        row = conn.execute("SELECT * FROM revisions WHERE revision=?", (revision,)).fetchone()
    if row is None:
        raise ValueError("Select an existing browser policy revision")
    candidate = dict(row)
    baselines = []
    for scenario in scenarios:
        state = scenario["initial_state"]
        task = state.get("task_kind", "application_agent")
        profile = {"model": state["model"], "max_tokens": 4096, "temperature": 0., "routing": routing_fingerprint()}
        digest = schema_digest(contract_schema(task))
        if (task != candidate["task"] or scenario["schema_hash"] != digest or candidate["schema_hash"] != digest
                or scenario["routing"] != profile["routing"] or not compatible_revision(candidate, profile)):
            raise ValueError("Scenario, candidate and current runtime must have matching contracts and routing")
        baselines.append(selected(model=state["model"], task_kind=task))
    cfg = settings()
    budget = InferenceContext(cancel_event=stage_context().get("stop_event"))
    report = {"state": "evaluating", "evaluation_kind": "controlled_multistep_simulation",
              "candidate_revision": revision, "bundle_sha256": hashlib.sha256(dumps(scenarios).encode()).hexdigest(),
              "cases": [], "live_submission_evidence": False, "promotion_eligible": False}
    token = offline.set(True)
    trace_token = current_trace.set("scenario-" + str(time.time_ns()))
    try:
        for index, (scenario, (baseline, baseline_id)) in enumerate(zip(scenarios, baselines)):
            case = {"id": scenario["id"], "baseline_revision": baseline_id}
            # Reset the world for each policy and alternate execution order.
            policies = [("baseline", baseline), ("candidate", candidate["instructions"])]
            for name, instructions in policies[::1 if index % 2 == 0 else -1]:
                calls, tokens, started = budget.calls, budget.reserved_output_tokens, time.monotonic()
                case[name] = run_scenario(scenario, instructions, budget)
                case[name].update(gateway_and_native_root_reservations=budget.calls - calls,
                                  reserved_output_tokens=budget.reserved_output_tokens - tokens,
                                  elapsed_seconds=round(time.monotonic() - started, 3))
            report["cases"].append(case)
        complete = all(case[name]["outcome"] in {"completed", "blocked", "failed", "invalid_action", "step_limit"}
                       for case in report["cases"] for name in ("baseline", "candidate"))
        report["state"] = "evaluated" if complete else "incomplete_scenario_coverage"
        report["outcomes"] = {name: {outcome: sum(case[name]["outcome"] == outcome for case in report["cases"])
                             for outcome in {case[name]["outcome"] for case in report["cases"]}}
                             for name in ("baseline", "candidate")}
    except Exception as exc:
        report.update(state="failed", reason_code=budget.failure_reason or "scenario_execution_failed",
                      error_type=budget.failure_error_type or type(exc).__name__)
    finally:
        report.update(gateway_and_native_root_reservations=budget.calls,
                      reserved_output_tokens=budget.reserved_output_tokens)
        offline.reset(token)
        current_trace.reset(trace_token)
    return report

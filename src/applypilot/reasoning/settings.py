"""Reasoning defaults; context stays outside the model's working window."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

WORKFLOW_TASKS = frozenset({"outreach_draft", "network_reply", "outcome_classify", "outcome_link",
                            "interview_brief", "application_form", "decline_feedback", "agent_context",
                            "browser_observation", "application_requirements"})
TEXT_TASKS = frozenset(['backup_diary_edit_revise', 'evidence_draft_draft_capability_claims', 'evidence_draft_judge_entries_for_prune', 'impact_brief_generate_director_followups', 'impact_defense_llm_extract_signals', 'impact_defense_semantic_job_conditioning', 'impact_draft_gate_skeletons', 'impact_draft_llm_same_accomplishment', 'impact_draft_measurement_entailed', 'impact_draft_outcome_entailed', 'impact_draft_repair_measurement_sources', 'impact_draft_run_keyed_stage', 'impact_draft_skeleton_pass', 'impact_matcher_extract_impact_intent_cached', 'impact_matcher_semantic_match_records', 'onboarding_planning_propose', 'unattended_research_extract_person'])
BROWSER_TASKS = frozenset({"application_agent", "application_assist", "application_inspect"})
DSPY_TASKS = WORKFLOW_TASKS | TEXT_TASKS | BROWSER_TASKS | {"score", "factual_review", "signal_roles", "tailor_resume",
    "cover_letter", "detail_extract", "company_research"}

RLM_FIELDS = {
    "threshold": ("APPLYPILOT_RLM_THRESHOLD_CHARS", 20000, 1000, 1000000),
    "max_calls": ("APPLYPILOT_RLM_MAX_CALLS", 32, 1, 64),
    "max_iterations": ("APPLYPILOT_RLM_MAX_ITERATIONS", 24, 1, 64),
    "timeout": ("APPLYPILOT_RLM_TIMEOUT_SECONDS", 0, 0, None),
    "parallel_calls": ("APPLYPILOT_RLM_PARALLEL_CALLS", 2, 1, 4),
}


def flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"true", "1", "yes", "on"}


def integer(name: str, default: int, low: int = 0, high: int | None = None) -> int:
    value = int(os.getenv(name, str(default)))
    if value < low or (high is not None and value > high):
        raise ValueError(f"Invalid value for {name}")
    return value


def dspy_enabled(task_kind: str) -> bool:
    selected = os.getenv("APPLYPILOT_DSPY_TASKS", ",".join(sorted(DSPY_TASKS)))
    return flag("APPLYPILOT_DSPY_ENABLED", True) and (task_kind == "agent_context" or task_kind in {s.strip() for s in selected.split(",")})


def artifact_path(task_kind: str) -> Path:
    if task_kind not in DSPY_TASKS:
        raise ValueError("Unsupported DSPy task")
    from applypilot.config import APP_DIR
    return Path(os.getenv("APPLYPILOT_DSPY_ARTIFACT_DIR", str(APP_DIR / "ai-programs"))).expanduser() / f"{task_kind}.json"


@dataclass(frozen=True)
class RLMSettings:
    threshold: int
    max_calls: int
    max_iterations: int
    timeout: int
    parallel_calls: int = 2
    image: str = "python:3.11-slim"

    @classmethod
    def read(cls) -> RLMSettings:
        # Bound work by calls/turns, not time spent awaiting provider recovery.
        # Owners may opt into a wall-clock budget; zero leaves it disabled.
        return cls(**{name: integer(*definition) for name, definition in RLM_FIELDS.items()},
                   image=os.getenv("APPLYPILOT_RLM_DOCKER_IMAGE", "python:3.11-slim"))


def rlm_enabled() -> bool:
    return flag("APPLYPILOT_RLM_ENABLED", True)


def routing_fingerprint() -> str:
    """Invalidate evaluated policies when routing changes; persist only a digest.

    Alias names cannot reveal provider changes behind a remote gateway. Such
    changes still require an operator-initiated reevaluation.
    """
    import hashlib
    import json
    keys = ("LLM_URL", "APPLYPILOT_AGENT_BASE_URL", "APPLYPILOT_REQUIRE_LLM_PROXY",
            "LLM_MODEL", "LLM_MODEL_FAST", "LLM_MODEL_REASONING", "LLM_MODEL_CODE",
            "APPLYPILOT_RLM_ENABLED", "APPLYPILOT_RLM_THRESHOLD_CHARS",
            "APPLYPILOT_CONTEXT_RESULT_CHARS", "APPLYPILOT_RLM_DOCKER_IMAGE")
    values = {key: os.getenv(key, "") for key in keys}
    values["APPLYPILOT_RLM_ENABLED"] = os.getenv("APPLYPILOT_RLM_ENABLED", "true")
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

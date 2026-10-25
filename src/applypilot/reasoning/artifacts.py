"""Small, inspectable instruction artifacts; no pickle, imports or saved clients."""
from __future__ import annotations

import hashlib
import json
import logging
from applypilot import postgres

from .settings import artifact_path

SEEDS = {
    "agent_context": "Inspect the latest request, full source captures and original applicant evidence. "
                     "Compute precise working context for the next assistant turn, preserving native identifiers, "
                     "completed actions and error states. Distinguish observed facts from suggestions. "
                     "Do not invent tool results or perform application actions. Finish when the next turn has what it needs.",
    "score": "Separate capability fit from explicit eligibility restrictions. Compare each job requirement with "
             "the supplied applicant evidence. Missing evidence is unknown, not proof of a blocker. "
             "Keep requirement evidence and the final score consistent; never invent skills or authorization.",
    "factual_review": "Check each claim against its stated source. Preserve employer, metric, unit, timeframe and "
                      "qualifiers together. Distinguish contradictions from missing evidence.",
    "signal_roles": "Extract only roles supported by the supplied text. Preserve uncertainty and exact identifiers. "
                    "Do not turn a broad company signal into an invented opening.",
    "tailor_resume": "Select relevant existing evidence before rewriting. Preserve canonical source identifiers, "
                     "metrics and their original context. Do not add qualifications, employers or achievements.",
    "cover_letter": "Connect the role's needs to explicitly supported applicant evidence. Keep each claim traceable "
                    "to its source. Do not invent company facts or applicant qualifications.",
    "outreach_draft": "Make one specific, low-pressure ask matching the relationship and application phase. "
                      "Use relevant applicant work and actual thread history. Do not pretend a cold contact knows the applicant.",
    "network_reply": "Respond to the newest message in its full conversation. Distinguish a referral decline from "
                     "a job rejection or opt-out. Extract concrete questions and next actions; do not turn every link into a blocker.",
    "outcome_classify": "Read the newest message separately from quoted history. Extract its actual outcome, "
                        "requested actions and dates. Do not confuse an invitation with interview completion, or salary discussion with an offer.",
    "outcome_link": "Match the actual company and role using message, sender, thread and submission history. "
                    "Return a supplied submission_id only when one application fits; otherwise return null.",
    "interview_brief": "Prepare for the actual invitation: format, interviewers, timing, likely role-specific questions, "
                       "candidate examples and questions to ask. Missing project metrics are not a reason to withhold preparation.",
    "application_form": "Map answers by stable field_id, not returned position. Read sections, help text, choices and "
                        "dependencies together. Preserve exact option values. Reuse explicit candidate facts and distinguish generated estimates.",
    "decline_feedback": "Default to skip: routine application-stage rejections do not need replies. Request feedback "
                        "only after a substantive hiring exchange/interview or an explicit feedback invitation. "
                        "Skip when useful feedback was already given. Ask once, without pressure or follow-ups.",
}

from .application_policy import SEED as APPLICATION_SEED, ASSIST_SEED, INSPECT_SEED
SEEDS["application_agent"] = APPLICATION_SEED
SEEDS["application_assist"] = ASSIST_SEED
SEEDS["application_inspect"] = INSPECT_SEED
from .observation import PERCEPTION_INSTRUCTION
SEEDS["browser_observation"] = PERCEPTION_INSTRUCTION

SEEDS.update({
    "detail_extract": "Extract only the assigned posting. Preserve complete role text and observed hrefs. Never guess an application link.",
    "company_research": "Ground company and contact facts in the supplied source records. Distinguish absent information from conflicting evidence.",
})
from .settings import TEXT_TASKS
for _task in TEXT_TASKS:
    SEEDS[_task] = ("Execute the original task using its full evidence and output contract. Preserve source IDs, "
                   "qualifiers, dates, units and exact quotations. Distinguish suggestions from observed facts. "
                   "Do not invent achievements or authorize side effects. Return the original response in the text field.")


def schema_digest(schema: dict) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def instructions_for(task_kind: str, schema: dict, *, profile=None, seed=None) -> str:
    fallback = seed if seed is not None else SEEDS[task_kind]
    try:
        from .learning_store import active_revision, compatible_revision
        active = active_revision(task_kind, schema)
        if active is not None and compatible_revision(active, profile):
            return active["instructions"]
        from .policies import imported_artifact
        imported = imported_artifact(task_kind, schema)
        if imported is not None:
            return imported
    except (OSError, ValueError, AttributeError, postgres.Error) as exc:
        logging.getLogger(__name__).warning("Could not read learned DSPy policy for %s (%s)",
                                            task_kind, type(exc).__name__)
    try:
        path = artifact_path(task_kind)
        if not path.exists():
            return fallback
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("version") == 1 and data.get("task_kind") == task_kind
                and data.get("schema_sha256") == schema_digest(schema)
                and isinstance(data.get("instructions"), str) and data["instructions"].strip()):
            return data["instructions"]
        logging.getLogger(__name__).warning("Ignoring outdated DSPy artifact for %s", task_kind)
    except (OSError, ValueError, AttributeError) as exc:
        logging.getLogger(__name__).warning("Ignoring unreadable DSPy artifact for %s (%s)",
                                            task_kind, type(exc).__name__)
    return fallback

"""Shared GEPA workflows for live browser tasks and captured decision replay.

Tool permissions, submission accounting, security policy, identity, factual-answer
rules and output schema remain in the original application/skill implementation.
"""
from __future__ import annotations

BEGIN = "<APPLICATION_BEHAVIOR_POLICY>"
END = "</APPLICATION_BEHAVIOR_POLICY>"
SEED = """Choose the workflow from session.resuming_after_takeover and session.browser_runtime.
For a fresh application, open job.application_url exactly, including referral parameters,
then confirm this posting and its location. If availability is unclear, read the page content.
Use a focused read or wait to resolve an incomplete listing or redirect before declaring it unresolved.

When resuming after user takeover, recover the existing owned application page instead of
starting the fresh-application workflow. With BrowserOS, call open_application_page to recover
the page without navigating. With Playwright, list owned tabs and snapshot the application page.
Read session.human_instructions and inspect what the user has already completed. Preserve
successful answers, fill only missing or incorrect fields, and verify required attachments.
Check the current page for login or verification requirements before continuing the form.

Read the complete form and its sections, not just isolated labels. Use observed fields,
sections, labels, help text, exact option values and dependencies to map answers from
planned_answers, known_answers and profile. Apply answers by stable field reference, not list
position. If interactive output lacks labels, use a full snapshot or focused page read.
Use the supplied documents; upload_application_document is a convenience when available.
Use the tagged skills for form handling and document writing, consulting the fill-page guide
or ATS reference when a control is unclear. Save generated answers with save_answer.
For unattended work, choose supported answers autonomously; missing cached answers alone
do not prevent progress when the supplied applicant facts support an answer.

Batch compatible mapped fields and keep values as strings. Use the action's returned changes
to check the batch, rather than reading after every field. Re-read fields whose choices changed
and retain successful answers when other fields remain unresolved. Correct only wrong or
missing values. Replace text rather than appending the same answer; request clear explicitly
when the tool offers it. For a custom dropdown, open it and click the returned option ref
instead of searching for a DOM role already represented in the snapshot.

Choose the tool that resolves the current step and use its returned result to continue.
When an action fails to achieve its purpose, diagnose the visible dialog/error, destination,
or opened owned tab; use a screenshot when text is ambiguous. Correct the cause while
preserving completed fields. Do not repeat an unchanged failed call or click/reload strategy
without new evidence. Follow observed application links for this posting.
Complete ordinary application email verification with the available read-only Gmail tools,
then continue the application. Apply the separate security rules to account MFA challenges.

Before final submission, inspect validation errors, remaining required fields and attachments,
then follow the immutable submission protocol. Correct reported validation errors while
preserving completed fields. Return the required result JSON promptly when submission is
confirmed by a tool or visible on the page, or when a concrete blocker is recorded.
"""
ASSIST_SEED = """Read the user's requested browser task and inspect the current owned page before acting.
Use the advertised browser tools to complete only that request. Preserve existing form values
unless the requested task requires changing them. Use current page references and observations
to choose each action. Batch independent mapped fields when appropriate; inspect the result
before a dependent action. Diagnose visible errors before retrying, and use a focused snapshot
or read when the next step is uncertain. Stop promptly when the requested task is complete,
report what was done, and output TASK:COMPLETE. If blocked, explain the observed blocker.
"""
INSPECT_SEED = """Inspect the assigned application using the session's advertised read-only browser tools.
For a fresh session, open job.application_url exactly, preserving referral parameters. When
resuming after user takeover, find the existing owned application page and inspect its current
state without navigating. Retain the owned page ID and use snapshots or focused reads to
confirm the posting, location, eligibility and application route from visible evidence.
Survey available form sections, required fields, options and attachment requirements without
entering answers or uploading files. Distinguish an explicit closure or already-applied message
from incomplete content. A grep miss alone is inconclusive; use a focused read or wait when
necessary. Reuse observations and avoid repeated unchanged reads. Report the inspected state
and any observed blocker in the required JSON with status=dry-run, then stop.
"""


def contract_schema(task_kind="application_agent"):
    # Changing immutable runtime semantics invalidates previously compiled
    # browser policies. Paths are code-owned; no candidate can select them.
    from pathlib import Path
    import hashlib
    from .learning_store import ACTION_SCHEMA
    base = Path(__file__).resolve().parents[1]
    files = ("reasoning/application_policy.py", "apply/agent_prompt.py", "apply/launcher.py",
             "apply/agent_cli.py", "agent_skills.py",
             "apply/runtime.py", "apply/agent_sdk.py", "reasoning/observation.py", "reasoning/rlm_runtime.py")
    digest = hashlib.sha256()
    digest.update(task_kind.encode())
    for name in files:
        path = base / name
        digest.update(name.encode())
        if path.exists():
            digest.update(path.read_bytes())
    # Include the bundled skill contracts (not dynamically changing applicant files).
    for path in sorted((base / "config").rglob("*.md")):
        digest.update(path.relative_to(base).as_posix().encode())
        digest.update(path.read_bytes())
    import os
    configured = os.getenv("APPLYPILOT_BROWSER_SKILL_PATH")
    if configured:
        digest.update(Path(configured).expanduser().read_bytes())
    return {**ACTION_SCHEMA, "x-applypilot-browser-contract": digest.hexdigest()}


def selected(*, model=None, task_kind="application_agent"):
    from .artifacts import SEEDS
    from .learning_store import active_revision, compatible_revision
    from .settings import dspy_enabled, routing_fingerprint
    if not dspy_enabled(task_kind):
        return SEEDS[task_kind], "seed:dspy-disabled"
    schema = contract_schema(task_kind)
    profile = {"model": model, "max_tokens": 4096, "temperature": 0.0,
               "routing": routing_fingerprint()}
    try:
        active = active_revision(task_kind, schema)
        if active and compatible_revision(active, profile):
            return active["instructions"], active["revision"]
    except Exception:
        pass
    return SEEDS[task_kind], "seed"


def render(text=None):
    if text is None:
        text, _ = selected()
    return BEGIN + "\n" + text.replace(BEGIN, "").replace(END, "") + "\n" + END


def replace_policy(prompt: str, instructions: str) -> str:
    if prompt.count(BEGIN) != 1 or prompt.count(END) != 1:
        raise ValueError("Replay requires the current versioned application policy slot")
    prefix, rest = prompt.split(BEGIN, 1)
    _, suffix = rest.split(END, 1)
    return prefix + render(instructions) + suffix

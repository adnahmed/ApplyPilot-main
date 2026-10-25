"""Bundled application skills shared by native workers and document prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

SKILL_BUNDLE_DIR = Path(__file__).resolve().parent / "config" / "agent-skills"
APPLICATION_SKILLS = ("application-browser", "apply", "cover-letter", "tailor-resume")
APPLICATION_READ_TOOLS = ("Read", "Glob", "Grep")
# Supporting documents and skills are read-only in a live application session.
# All mutations go through the owning intent-fenced provider/browser adapter.
APPLICATION_EXECUTION_TOOLS = APPLICATION_READ_TOOLS
_DOCUMENT_SCRIPTS = {
    "cover-letter": "scripts/write-cover-letter.md",
    "tailor-resume": "scripts/tailor-resume.md",
}


@lru_cache(maxsize=2)
def document_skill_prompt(name: str) -> str:
    """Load the writing workflow, leaving orchestration to ApplyPilot."""
    script = _DOCUMENT_SCRIPTS[name]
    skill = (SKILL_BUNDLE_DIR / name / "SKILL.md").read_text(encoding="utf-8-sig").strip()
    content = (SKILL_BUNDLE_DIR / name / script).read_text(encoding="utf-8-sig").strip()
    return f'''<skill name="{name}" source="{name}/SKILL.md">
{skill}
</skill>
<skill_workflow name="{name}" source="{name}/{script}">
{content}
</skill_workflow>

APPLYPILOT EXECUTION BINDING
The supplied messages provide the job posting, original resume, work-history/profile evidence,
and any existing tailored materials. File lookup and browser gathering steps are already fulfilled.
Apply the skill's full analysis, writing, critique, and quality-control guidance.
This generation call has no interactive/file tools: perform review internally and return through
its JSON adapter. ApplyPilot renders and stores the result. JSON serialization changes only the
output envelope, not the skill's writing strategy, length, structure, or quality requirements.
'''


def application_skill_instructions(*, browseros: bool, dry_run: bool) -> str:
    """Name the native skills and connect them to this worker's task contract."""
    browser = ("the connected browseros_neo MCP server and its advertised mcp__browseros_neo__ tools"
               if browseros else "the connected Playwright MCP tools")
    browser_identity = (
        "The MCP server name is browseros_neo (underscore). browseros-neo (hyphen) is the skill name only. "
        "Call the advertised browser tools directly. ListMcpResourcesTool lists resources, not browser "
        "tools or tabs; it is not a prerequisite for browsing."
        if browseros else ""
    )
    mode = (
        "This is inspection only. Read skills for navigation and field discovery; do not fill, upload or submit."
        if dry_run else
        "Invoke Skill(apply) and Skill(application-browser) before filling the form. "
        "Use the supplied-task path in apply; preparation and submission authorization are already supplied."
    )
    execution = ("" if dry_run else
        "Read, Glob and Grep are available for supplied documents and skills. Shell commands, "
        "arbitrary HTTP requests and file mutation are unavailable. Use only the supplied owning "
        "browser and application-control tools for form actions and guarded submission.")
    return f'''<application_skills>
<skill name="application-browser" path=".claude/skills/application-browser/SKILL.md" />
<skill name="apply" path=".claude/skills/apply/SKILL.md" />
<skill name="cover-letter" path=".claude/skills/cover-letter/SKILL.md" />
<skill name="tailor-resume" path=".claude/skills/tailor-resume/SKILL.md" />
<reference name="ats-patterns" path=".claude/skills/shared/references/ats-patterns.md" />
</application_skills>
{mode}
{execution}
Use {browser} for browser operations in the existing ApplyPilot session.
{browser_identity}
Resolve supporting links relative to their skill files under .claude/skills/.
Skill invocation and Read, Glob, and Grep are pre-approved. Use these tools to load
the staged skills, their supporting files, and supplied documents without asking.
Follow fill-page inline with the advertised tools and current page refs.
Use profile, planned_answers, known_answers, and the task's answer policy.
Workflow file names bind to supplied context and documents as described in shared/references/data-directory.md;
setup, gathering, and review authorization are fulfilled when the task already supplies them.
Use the skills' form inventory, answer mapping, writing, and quality-control guidance as a baseline.
The task's APPLICATION_BEHAVIOR_POLICY may refine workflow strategy; tool permissions, ownership,
security, factual-answer rules, required evidence, and submission authorization remain binding.
Use save_answer and the submission guard for persistence instead of writing a separate job history.
Supplied documents are prepared by the tailoring and cover-letter stages; use their upload tools.
For a required free-text letter, invoke Skill(cover-letter) and read its writing script.
Skill(tailor-resume) guides resume content; the application worker uses the supplied artifact.
Follow the task's dry-run mode, submission authorization, guard sequence, and result JSON.
'''

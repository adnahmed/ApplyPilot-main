"""Job-specific resume content and presentation, with profile-injected contact details."""

from applypilot import operations as ops

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from applypilot.agent_skills import document_skill_prompt
from applypilot.config import RESUME_PATH, TAILORED_DIR, load_profile
from applypilot.database import get_connection, get_jobs_by_stage, transition_state
from applypilot.impact.matcher import build_evidence_pack_text
from applypilot.impact.pipeline import claim_pack_for_job
from applypilot.llm import LLMTask, get_client
from applypilot.reasoning.prompt_templates import instruction_message
from applypilot.profile import fit_requirement_context
from applypilot.scoring.validator import (
    sanitize_text,
)

log = logging.getLogger(__name__)

from applypilot.scoring.resume_blocks import LAYOUT_SCHEMA, manifest as layout_manifest, quality_notes, render_blocks

MAX_ATTEMPTS = 5  # historical reporting threshold; generation is not capped

_RESUME_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "header": {"type": "string"},
        "subtitle": {"type": ["string", "null"]},
        "dates": {"type": "string"},
        "technologies": {"type": "string"},
        "source_id": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["header", "bullets"],
}

TAILORED_RESUME_SCHEMA = {
    "type": "object",
    "properties": {
        "used_source_ids": {"type": "array", "items": {"type": "string"}},
        "positioning": {"type": "object", "additionalProperties": False,
            "properties": {"role_focus": {"type": "string"}, "emphasis_reason": {"type": "string"},
                           "known_gaps": {"type": "array", "items": {"type": "string"}}},
            "required": ["role_focus", "emphasis_reason", "known_gaps"]},
        # New generations always select a style; stored legacy layouts remain valid.
        "layout": {**LAYOUT_SCHEMA, "required": ["version", "style", "blocks"]},
        "certifications": {"type": "array", "items": {"type": "string"}},
        "languages": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "skills": {"type": "object", "additionalProperties": {"type": "string"}},
        "experience": {"type": "array", "items": _RESUME_ENTRY_SCHEMA},
        "projects": {"type": ["array", "null"], "items": _RESUME_ENTRY_SCHEMA},
        "education": {
            "anyOf": [
                {"type": "string"},
                {
                    "type": "array",
                    "items": {
                        "anyOf": [
                            {"type": "string"},
                            {
                                "type": "object",
                                "properties": {
                                    "institution": {"type": "string"},
                                    "degree": {"type": "string"},
                                    "dates": {"type": "string"},
                                    "area": {"type": "string"},
                                    "field": {"type": "string"},
                                    "studyType": {"type": "string"},
                                },
                            },
                        ],
                    },
                },
            ],
        },
    },
    "required": ["title", "summary", "skills", "experience", "education", "layout"],
}


def document_variant(job: dict) -> str:
    """Preserve an explicitly selected variant; do not infer a career identity."""
    value = job.get("document_variant")
    return value.strip() if isinstance(value, str) and value.strip() else "job-tailored"


# ── Prompt Builders (profile-driven) ──────────────────────────────────────

def _build_tailor_prompt(profile: dict, evidence_block: str = "", job: dict | None = None) -> str:
    """Build the resume tailoring system prompt from the user's profile.

    All skills boundaries, preserved entities, and formatting rules are
    derived from the profile -- nothing is hardcoded. ``evidence_block``
    (ledger-backed impact evidence) is appended only when present. Ordinary
    source-supported resume facts remain usable with or without a ledger.
    """
    boundary = profile.get("skills_boundary", {})
    resume_facts = profile.get("resume_facts", {})

    # Format skills boundary for the prompt
    skills_lines = []
    for category, items in boundary.items():
        if isinstance(items, list) and items:
            label = category.replace("_", " ").title()
            skills_lines.append(f"{label}: {', '.join(items)}")
    skills_block = "\n".join(skills_lines)

    # Preserved entities
    companies = resume_facts.get("preserved_companies", [])
    projects = resume_facts.get("preserved_projects", [])
    school = resume_facts.get("preserved_school", "")
    real_metrics = resume_facts.get("real_metrics", [])

    companies_str = ", ".join(companies) if companies else "N/A"
    _projects_str = ", ".join(projects) if projects else "N/A"
    metrics_str = ", ".join(real_metrics) if real_metrics else "N/A"

    education = profile.get("experience", {})
    education_level = education.get("education_level", "")

    variant = document_variant(job or {})
    # Prompt construction is pure: global trend endpoints do not prove JD fit.

    return f"""APPLYPILOT INPUTS:
Document variant: {variant}
Candidate skills from profile:
{skills_block}

Preserved employers: {companies_str}
Preserved projects: {_projects_str}
Preserved school: {school}
Recorded metrics: {metrics_str}
Education level (context, not an institution): {education_level}

{evidence_block}

APPLYPILOT OUTPUT ADAPTER:
Apply the skill's requirement mapping, relevance, ordering, layout and accuracy guidance
to write a resume tailored to this job. You may rewrite, paraphrase, combine related
evidence, and compose a new summary grounded in the supplied resume and profile.
Write each experience and project entry with its own header and bullets. Include
subtitle, dates and technologies when useful. Source IDs are optional JSON metadata.
Keep employers, projects, dates, credentials and metrics accurate and correctly
attributed. Do not invent experience or move achievements between employers/projects.
Write a professional title that reflects the candidate's supported experience.
Group and reorder supported skills for relevance, returning each category as a
comma-separated string. Source wording does not need to be copied verbatim.
ROLE-SPECIFIC WRITING:
Identify the employer's actual problem from the JD, not just its keywords. Select the
strongest 3–5 supported achievements for that problem and prioritize them within their
correct employer/project entries. Preserve chronology; reorder bullets, not employment dates.
The first third should make the relevant domain, contribution and scope clear. A summary
is optional; when useful, use 2–3 concrete sentences, not a generic adjective-led biography.
Avoid "results-driven", "dynamic professional", "passionate", and "proven track record".
Each achievement should explain what changed, your contribution and the supported result;
when no measurement exists, state the concrete delivered capability without inventing a number.
Do not repeat the same achievement as both employment and a personal project, or pad the
page with every technology you know. Use adjacent experience honestly; never convert a
requested skill into claimed experience. Public-safe private project claims have no public
repository URL: do not invent a link or confidential project name.
Return a short optional "positioning" object: role_focus, emphasis_reason and known_gaps.
These are user-facing editorial notes, not new facts, and are not printed on the résumé.

ApplyPilot assembles contact details and renders deterministic typed blocks.
Automatically choose the presentation that best fits this job and the supported content.
Always supply "layout" with "version": 1, "style": "technical", "editorial" or "classic",
and an ordered "blocks" list. Technical uses a restrained, left-aligned sans-serif hierarchy
for skills/project-heavy content; editorial uses a serif name and more white space for
narrative/leadership-focused content; classic is the compact centered option for dense or
conventional applications. Select for readability and relevance, not novelty. Do not ask
the user to choose a style.
Each block has "type" chosen from
summary, skills, experience, projects, education, certifications, languages.
Each type can appear at most once. Include experience or projects with substantive,
source-supported bullets. Select only blocks with usable evidence. You may omit
summary, skills, education or other optional blocks when they do not help this job;
keep their top-level required fields empty rather than inventing facts. You may put
projects or education before experience when the supplied job evidence warrants it.
Use "show_technologies": false on experience/projects blocks to omit separate stack
lines; technologies may still appear naturally in supported achievement bullets.
No markup, arbitrary block kinds, CSS, instructions or template code in layout.
Certifications and languages, when present, are arrays of supported text entries.
Return only valid JSON in this structure instead of a Markdown document. Category names
under "skills" may vary. Education may be text or an array of text entries or objects
with institution, degree and dates. Preserve the underlying facts when reformatting.
Use [] and omit the block if no education evidence is available.
Omit certifications and languages blocks when their arrays are absent or empty.
Replace the example placeholders with supplied facts.

{{
  "layout": {{"version": 1, "style": "technical", "blocks": [{{"type": "summary"}}, {{"type": "experience", "show_technologies": false}}, {{"type": "projects", "show_technologies": false}}, {{"type": "skills"}}, {{"type": "education"}}]}},
  "used_source_ids": ["<exact supplied source key>"],
  "title": "<professional title grounded in candidate experience>",
  "summary": "<tailored summary, or empty and omit summary block>",
  "skills": {{
    "Languages": "<supported skills, comma-separated>",
    "Frameworks": "<supported skills, comma-separated>",
    "DevOps & Infra": "<supported skills, comma-separated>",
    "Databases": "<supported skills, comma-separated>",
    "Tools": "<supported skills, comma-separated>"
  }},
  "experience": [
    {{"header": "<role and employer>", "dates": "<actual dates>", "bullets": ["<tailored achievement>"]}}
  ],
  "projects": [
    {{"header": "<project name>", "bullets": ["<relevant contribution>"]}}
  ],
  "education": [
    "<supported education details>"
  ]
}}"""

# ── JSON Extraction ───────────────────────────────────────────────────────

def extract_json(raw: str) -> dict:
    """Robustly extract JSON from LLM response (handles fences, preamble).

    Args:
        raw: Raw LLM response text.

    Returns:
        Parsed JSON dict.

    Raises:
        ValueError: If no valid JSON found.
    """
    raw = raw.strip()

    # Direct parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Markdown fences
    if "```" in raw:
        for part in raw.split("```")[1::2]:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue

    # Find outermost { ... }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError("No valid JSON found in LLM response")


# ── Resume Assembly (profile-driven header) ──────────────────────────────

def assemble_resume_text(data: dict, profile: dict) -> str:
    """Convert JSON resume data to formatted plain text.

    Header (name, location, contact) is ALWAYS code-injected from the profile,
    never LLM-generated. All text fields are sanitized.

    Args:
        data: Parsed JSON resume from the LLM.
        profile: User profile dict from load_profile().

    Returns:
        Formatted resume text.
    """
    personal = profile.get("personal", {})
    lines: list[str] = []

    # Header -- always code-injected from profile
    lines.append(personal.get("full_name", ""))
    lines.append(sanitize_text(data.get("title", "Software Engineer")))

    # Location from search config or profile -- leave blank if not available
    # The location line is optional; the original used a hardcoded city.
    # We omit it here; the LLM prompt can include it if the user sets it.

    # Contact line
    contact_parts: list[str] = []
    if personal.get("email"):
        contact_parts.append(personal["email"])
    if personal.get("phone"):
        contact_parts.append(personal["phone"])
    if personal.get("github_url"):
        contact_parts.append(personal["github_url"])
    if personal.get("linkedin_url"):
        contact_parts.append(personal["linkedin_url"])
    if contact_parts:
        lines.append(" | ".join(contact_parts))
    lines.append("")

    lines.append(render_blocks(data))
    return "\n".join(lines).strip()


# ── Factual-review compatibility ─────────────────────────────────────────

def judge_tailored_resume(
    original_text: str, tailored_text: str, job_title: str, profile: dict, job_context: str = "",
    evidence_block: str = "",
) -> dict:
    """Compatibility result for callers from before factual review was removed."""
    return {
        "passed": True,
        "verdict": "NOT_RUN",
        "issues": "",
        "raw": "",
        "factual_review": "not_run",
    }


# ── Core Tailoring ───────────────────────────────────────────────────────

def tailor_resume(
    resume_text: str, job: dict, profile: dict, max_retries: int = 2,
    claim_pack: object | None = None,
) -> tuple[str, dict]:
    """Generate once and accept the first structurally usable resume.

    ``max_retries`` remains in the public signature for compatibility. Retries
    happen across scheduled dispatches so a worker is never held for repair
    prompts or review calls.
    """
    company = display_company(job)
    job_text = (
        f"TITLE: {job['title']}\n"
        f"COMPANY: {company or 'unknown (aggregator listing; employer may be named in the description)'}\n"
        f"LOCATION: {job.get('location', 'N/A')}\n\n"
        f"DESCRIPTION:\n{job.get('full_description') or job.get('description') or ''}"
    )

    from applypilot.scoring.document_basic import validate_resume_payload
    from applypilot.scoring.document_contract import CONTRACT_VERSION
    from applypilot.career.documents import document_context, context_prompt, document_usage
    from applypilot.impact.ledger import default_repository
    records = default_repository().list_records()
    context = document_context(resume_text, profile, records, job)
    report: dict = {
        "attempts": 1,
        "validator": None,
        "judge": None,
        "status": "pending",
        "validation_mode": "basic",
        "factual_review": "not_run",
        "document_contract_version": CONTRACT_VERSION,
        "document_context": {"input_revisions": context["input_revisions"],
                             "supplied_source_ids": sorted(context["sources"]), "used_source_ids": [],
                             "usage_reporting": "model_reported"},
    }
    report["document_variant"] = document_variant(job)
    client = get_client(LLMTask.REASONING, structured=True)

    # Impact ledger claim pack — None on empty ledger (legacy path untouched).
    if claim_pack is None:
        claim_pack = claim_pack_for_job(job, records=records)
    evidence_block = build_evidence_pack_text(claim_pack) if claim_pack and claim_pack.has_records else ""
    tailor_prompt_base = _build_tailor_prompt(profile, evidence_block, job=job)
    tailor_prompt_base += "\n\n" + fit_requirement_context(job)
    tailor_prompt_base += context_prompt(context)
    report["selected_impact_ids"] = [item.record.id for item in claim_pack.records] if claim_pack else []

    messages = [
        instruction_message(document_skill_prompt("tailor-resume")),
        {"role": "system", "content": tailor_prompt_base},
        {"role": "user", "content": f"ORIGINAL RESUME:\n{resume_text}\n\n---\n\nTARGET JOB:\n{job_text}\n\nReturn the JSON:"},
    ]
    from applypilot.reasoning.learning_store import last_example
    last_example.set(None)
    try:
        raw = client.chat_json(
            messages, schema=TAILORED_RESUME_SCHEMA, task_kind="tailor_resume",
            max_tokens=16384, temperature=0.4,
        )
        data = extract_json(raw)
    except ValueError as exc:
        validation = {
            "passed": False,
            "errors": [f"Invalid document response: {exc}"],
            "warnings": [],
            "validation_mode": "basic",
            "factual_review": "not_run",
        }
        report["validator"] = validation
        report["status"] = "unusable_output"
        return "", report

    validation = validate_resume_payload(data)
    report["document_context"], source_warnings = document_usage(data, context)
    validation["warnings"].extend(source_warnings)
    report["validator"] = validation
    if not validation["passed"]:
        report["status"] = "unusable_output"
        return "", report

    report["resume_layout"] = layout_manifest(data)
    report["writing_quality"] = quality_notes(data)
    report["positioning"] = data.get("positioning") or {}
    tailored = assemble_resume_text(data, profile)
    report["status"] = "approved"
    return tailored, report


# ── Batch Entry Point ────────────────────────────────────────────────────

# Strategies where `site` column holds the actual employer (not an aggregator).
# When a job has NULL/empty `company`, these strategies let us fall back to
# `site` as the company-key for caps.
DIRECT_EMPLOYER_STRATEGIES = frozenset({
    "greenhouse_api", "workday_api", "lever_api", "ashby_api",
    "amazon_jobs", "microsoft_careers", "apple_jobs", "google_careers",
    "meta_careers", "twilio_greenhouse",
})


def resolve_company_key(job: dict) -> str | None:
    """Return a lowercase company key for cap logic, or None if exempt.

    Resolution order:
      1. The extracted ``company`` column when populated.
      2. The ATS-tenant slug embedded in ``application_url`` for any
         job that points at a known direct-employer ATS (Greenhouse,
         Lever, Ashby, Workday). This catches the LinkedIn-aggregator
         case where ``company`` is NULL but the apply URL clearly
         identifies the employer (e.g. a LinkedIn listing whose Apply
         button resolves to job-boards.greenhouse.io/{slug}/...).
      3. Direct-employer ``site`` (the existing fallback for the API
         scrapers).
    """
    co = (job.get("company") or "").strip().lower()
    if co:
        return co

    # 2) ATS-tenant extraction from application_url. Works regardless
    # of whether the discovery strategy was an aggregator or direct.
    apply_url = (job.get("application_url") or "").lower()
    if apply_url:
        # Greenhouse: boards.greenhouse.io/{slug}/jobs/N or
        #             job-boards.greenhouse.io/{slug}/jobs/N (eu/us)
        m = re.search(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([a-z0-9-]+)/", apply_url)
        if m:
            return m.group(1)
        # Lever: jobs.lever.co/{slug}/...
        m = re.search(r"jobs\.lever\.co/([a-z0-9-]+)/", apply_url)
        if m:
            return m.group(1)
        # Ashby: jobs.ashbyhq.com/{slug}/...
        m = re.search(r"jobs\.ashbyhq\.com/([a-z0-9-]+)/", apply_url)
        if m:
            return m.group(1)
        # Workday: {tenant}.wd*.myworkdayjobs.com/...
        m = re.search(r"https?://([a-z0-9-]+)\.wd[0-9]+\.myworkdayjobs\.com/", apply_url)
        if m:
            return m.group(1)

    # 3) Direct-employer site fallback.
    strategy = (job.get("strategy") or "").lower()
    if strategy in DIRECT_EMPLOYER_STRATEGIES:
        site = (job.get("site") or "").strip().lower()
        if site:
            return site
    return None


def display_company(job: dict) -> str:
    """Best-known employer name for LLM prompts, or '' if unknown.

    ``site`` is the discovery source — for aggregator listings (LinkedIn,
    Indeed, ...) it is NOT the employer and must never be presented as one.
    Prefers the ``company`` column's original casing; falls back to the
    lowercase ATS-tenant slug / direct-employer site from
    ``resolve_company_key``.
    """
    company = (job.get("company") or "").strip()
    if company:
        return company
    slug = resolve_company_key(job) or ""
    # Title-case the slug so "databricks" becomes "Databricks"
    if slug and slug == slug.lower():
        slug = slug.replace("-", " ").title()
    return slug


def _name_parts(profile: dict) -> tuple[str, str]:
    """Return (first, last) name parts from profile, sanitized for filenames.

    Prefers preferred_name for the given-name half when available (e.g. "Jordan"
    from "Jordan Alexander Lee") so generated filenames match the
    candidate's public-facing name.
    """
    personal = profile.get("personal", {})
    preferred = (personal.get("preferred_name") or "").strip()
    full = (personal.get("full_name") or "").strip()
    parts = [p for p in re.split(r"\s+", full) if p] if full else []

    first_raw = preferred or (parts[0] if parts else "")
    last_raw = parts[-1] if len(parts) > 1 else ""
    first = re.sub(r"[^\w]", "", first_raw)
    last = re.sub(r"[^\w]", "", last_raw)
    return (first, last)


def _extract_keywords(job: dict, profile: dict, limit: int = 12) -> list[str]:
    """Extract ATS-relevant keywords from the job description.

    Returns the job title followed by any candidate boundary skills that
    appear in the title or description. Populated into DOCX
    core_properties.keywords so recruiters / ATS scanning document
    properties see a relevant keyword cloud.
    """
    result: list[str] = []
    title = (job.get("title") or "").strip()
    if title:
        result.append(title)

    boundary = profile.get("skills_boundary", {})
    skills: list[str] = []
    for items in boundary.values():
        if isinstance(items, list):
            skills.extend(s for s in items if isinstance(s, str) and s)

    desc_head = (job.get("full_description") or "")[:4000].lower()
    combined = f"{title.lower()} {desc_head}"

    for skill in skills:
        if len(result) >= limit:
            break
        if skill.lower() in combined and skill not in result:
            result.append(skill)

    return result


@ops.observe('tailor', 'job', job_arg='job', stage='tailor')
def _tailor_one_job(job: dict, resume_text: str, profile: dict, doc_format: str = "pdf") -> dict:
    """Tailor resume for a single job. Safe to call from multiple threads."""
    # Infrastructure failures are deferred durably by the result writer; do
    # not sleep while holding a shared preparation permit.
    tailored, report = tailor_resume(resume_text, job, profile)

    # Filename: FirstName_LastName_JobTitle_hash.docx per Jobscan §3.
    # Hash retains uniqueness since the same title may recur across employers.
    first, last = _name_parts(profile)
    safe_title = re.sub(r"[^\w\s-]", "", job.get("title") or "untitled")[:50].strip().replace(" ", "_")
    url_hash = hashlib.md5(job["url"].encode()).hexdigest()[:8]
    if first and last:
        prefix = f"{first}_{last}_{safe_title}_{url_hash}"
    elif first:
        prefix = f"{first}_{safe_title}_{url_hash}"
    else:
        safe_site = re.sub(r"[^\w\s-]", "", job["site"])[:20].strip().replace(" ", "_")
        prefix = f"{safe_site}_{safe_title}_{url_hash}"

    from applypilot.scoring.artifact_store import atomic_text, generation_name, write_approval
    from applypilot.scoring.document_contract import validation_reasons
    prefix = generation_name(prefix)
    output_dir = TAILORED_DIR if report["status"] == "approved" else TAILORED_DIR / "rejected"
    txt_path = output_dir / f"{prefix}.txt"
    atomic_text(txt_path, tailored)

    # The source and its content-bound approval are the durable product. Save
    # them before best-effort diagnostics or rendering so those failures never
    # trigger another generation.
    if report["status"] == "approved":
        write_approval(txt_path, job, "tailor", layout=report.get("resume_layout"),
                       document_context=report["document_context"])
        from applypilot.reasoning.document_feedback import bind_document
        bind_document("tailor_resume", job.get("url", ""), tailored, layout=report.get("resume_layout"))

    job_path = output_dir / f"{prefix}_JOB.txt"
    job_desc = (
        f"Title: {job['title']}\n"
        f"Company: {display_company(job) or 'unknown'}\n"
        f"Source: {job['site']}\n"
        f"Location: {job.get('location', 'N/A')}\n"
        f"Score: {job.get('fit_score', 'N/A')}\n"
        f"URL: {job['url']}\n\n"
        f"{job.get('full_description', '')}"
    )
    report_path = output_dir / f"{prefix}_REPORT.json"
    validation_report = None
    try:
        atomic_text(job_path, job_desc)
        atomic_text(report_path, json.dumps(report, indent=2))
        validation_report = str(report_path)
    except Exception:
        log.debug("Diagnostic document write failed for %s", txt_path, exc_info=True)

    from applypilot.work_context import stage_context
    doc_path = None
    if report["status"] == "approved" and not stage_context().get("defer_document_rendering", False):
        try:
            from applypilot.scoring.pdf import convert_to_pdf
            personal = profile.get("personal", {})
            full_name = personal.get("full_name") or personal.get("preferred_name") or ""
            job_title = (job.get("title") or "").strip()[:150]
            site = (job.get("site") or "").strip()[:80]
            metadata = {
                "title": f"Resume — {full_name} for {job_title}" if full_name else f"Resume — {job_title}",
                "company": display_company(job) or job.get("site", "Company"),
                "subject": job_title,
                "author": full_name,
                "category": "Resume",
                "keywords": _extract_keywords(job, profile),
                "comments": (
                    f"Customized for: {job_title}\n"
                    f"Source: {site}\n"
                    f"Date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
                ),
            }
            doc_path = str(convert_to_pdf(txt_path, doc_format=doc_format, metadata=metadata))
        except Exception:
            log.debug("Document generation failed for %s", txt_path, exc_info=True)

    return {
        "url": job["url"],
        "_document_context": report["document_context"],
        "path": str(txt_path),
        "pdf_path": doc_path,
        "rendering_pending": report["status"] == "approved" and bool(stage_context().get("defer_document_rendering", False)),
        "title": job["title"],
        "site": job["site"],
        "status": report["status"],
        "attempts": report["attempts"],
        "document_variant": report.get("document_variant"),
        "validation_report": validation_report,
        "validation_errors": validation_reasons(report),
        "error_origin": "document_validator" if report["status"] != "approved" else None,
        "validation_mode": report["validation_mode"],
        "factual_review": report["factual_review"],
    }


def _mark_tailor_result(conn, url: str, status: str, path: str | None, *,
                        attempts: int | None = None, now: str | None = None,
                        document_variant: str | None = None, diagnostics: dict | None = None) -> None:
    """Persist one document outcome. Temporal handles failed Activity retries."""
    from applypilot.scoring.document_contract import CONTRACT_VERSION
    now = now or datetime.now(timezone.utc).isoformat()
    details = {**(diagnostics or {}), "attempts": attempts}
    if status == "approved":
        if not path:
            raise ValueError("Approved resume has no artifact reference")
        conn.execute("UPDATE jobs SET tailored_resume_path=?, tailored_at=?, document_variant=?, "
                     "tailor_attempts=COALESCE(tailor_attempts,0)+1, tailor_error=NULL, "
                     "tailor_contract_version=?, tailor_repair_pending=0 WHERE url=?",
                     (path, now, document_variant, CONTRACT_VERSION, url))
        transition_state(conn, url, "tailored", reason="tailored OK", metadata=details, force=True)
    else:
        conn.execute("UPDATE jobs SET tailor_attempts=COALESCE(tailor_attempts,0)+1, "
                     "tailor_error=?, tailor_contract_version=? WHERE url=?",
                     (status, CONTRACT_VERSION, url))
        transition_state(conn, url, "tailor_failed", reason=status, metadata=details, force=True)



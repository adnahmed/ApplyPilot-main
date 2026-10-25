"""LaTeX-based resume/cover letter rendering with deterministic gates.

Ports the compile-resume.ps1 logic to Python:
- latexmk → pdflatex×2 fallback chain
- Page count verification (pypdf; pdfinfo/pdftotext CLI if available)
- Extracted text must contain name + email
- One controlled compaction fallback (\vfill removal, topsep/itemsep ≤2pt)
- sha256 artifact manifest with idempotent reuse
- Professional filename: <First>_<Last>_<Company>_<Role>.pdf

Template style based on canonical V5.11 sources:
- canonical/ai-applied-canonical.tex
- canonical/backend-platform-canonical.tex

Unified hybrid template with optional sections (Option 3).
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pypdf

from applypilot.config import load_profile

log = logging.getLogger(__name__)

# LaTeX special characters that need escaping
_LATEX_SPECIAL_CHARS = {
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
}

_LATEX_ESCAPE_RE = re.compile("|".join(re.escape(c) for c in _LATEX_SPECIAL_CHARS.keys()))

def _escape_latex(text: str) -> str:
    """Escape LaTeX special characters in text."""
    if not text:
        return ""
    return _LATEX_ESCAPE_RE.sub(lambda m: _LATEX_SPECIAL_CHARS[m.group()], text)


# ─── Unified Canonical-Style LaTeX Template ────────────────────────────────

# This template merges the structure from both canonical sources:
# - ai-applied-canonical.tex (tight margins, 9.25pt, Projects section)
# - backend-platform-canonical.tex (standard margins, \small, Earlier Experience)
#
# All content is parameterized. Sections render only when data is provided.
# Uses helvet (sans-serif) font family matching canonical style.

RESUME_LATEX_TEMPLATE = r"""
\documentclass[11pt,letterpaper]{{article}}

% --- Packages ---
\usepackage[top={margin_top},bottom={margin_bottom},left={margin_left},right={margin_right}]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{helvet}}
\renewcommand{{\familydefault}}{{\sfdefault}}
\usepackage{{microtype}}
\usepackage[hidelinks]{{hyperref}}
\usepackage{{enumitem}}
\usepackage{{titlesec}}
\usepackage[none]{{hyphenat}}
\input{{glyphtounicode}}
\pdfgentounicode=1

% --- Page Setup ---
\pagestyle{{empty}}
\pagenumbering{{gobble}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{0pt}}
\raggedright
\hyphenpenalty=10000
\exhyphenpenalty=10000
\emergencystretch=3em
\urlstyle{{same}}

% --- Symbols ---
\newcommand{{\zeroone}}{{\ensuremath{{0\rightarrow1}}}}

% --- Role Macro (from ai-applied-canonical) ---
\newcommand{{\role}}[4]{{%
  \textbf{{#1}} \hfill \textbf{{#2}}\\
  \textit{{#3}} \hfill \textit{{#4}}\\[-2pt]
}}

% --- List Spacing (hybrid: ai tight + backend left) ---
\setlist[itemize]{{
  topsep={itemize_topsep},
  itemsep={itemize_itemsep},
  parsep=0pt,
  partopsep=0pt,
  left=1.05em,
  label=\textbullet
}}

% --- Section Formatting (canonical style) ---
\newcommand{{\titlerulewithspace}}{{%
  \vspace{{3pt}}%
  \titlerule
}}

\titleformat{{\section}}
  {{\normalsize\bfseries\uppercase}}
  {{}}
  {{0em}}
  {{}}
  [{{\titlerulewithspace}}]

\titlespacing*{{\section}}
  {{0pt}}
  {{9pt plus 3pt minus 2pt}}
  {{5pt plus 2pt}}

% --- Hyperref setup ---
\hypersetup{{
    colorlinks=true,
    linkcolor=black,
    urlcolor=blue,
    citecolor=black,
    pdfcreator={{ApplyPilot}},
    pdfproducer={{pdflatex}}
}}

\begin{{document}}
{font_size_select}

% ---------- Header ----------
\begin{{{header_environment}}}
  {{{name_style}\bfseries {full_name}}}\\[0.15em]
  {{\normalsize {headline}}}\\[0.2em]
  {location_line}
  {contact_line}
\end{{{header_environment}}}

% ---------- Summary ----------
{body_sections}

\end{{document}}
"""

# Default template parameters (can be overridden per style)
DEFAULT_TEMPLATE_PARAMS = {
    "margin_top": "0.5in",
    "margin_bottom": "0.5in",
    "margin_left": "0.55in",
    "margin_right": "0.55in",
    "font_size_select": r"\normalsize",
    "itemize_topsep": "3pt",
    "itemize_itemsep": "3pt",
}

# AI-applied style (tighter)
AI_TEMPLATE_PARAMS = {
    **DEFAULT_TEMPLATE_PARAMS,
    "margin_top": "0.4in",
    "margin_bottom": "0.4in",
    "margin_left": "0.52in",
    "margin_right": "0.52in",
    "font_size_select": r"\fontsize{10.25}{11.7}\selectfont",
    "itemize_topsep": "2pt",
    "itemize_itemsep": "2.1pt",
}

# Backend-platform style (standard)
BACKEND_TEMPLATE_PARAMS = {
    **DEFAULT_TEMPLATE_PARAMS,
    "margin_top": "0.6in",
    "margin_bottom": "0.6in",
    "margin_left": "0.6in",
    "margin_right": "0.6in",
    "font_size_select": r"\small",
    "itemize_topsep": "4pt",
    "itemize_itemsep": "4pt",
}

COVER_LETTER_LATEX_TEMPLATE = r"""
\documentclass[10pt,letterpaper]{{article}}

% --- Packages ---
\usepackage[top=1in,bottom=1in,left=1in,right=1in]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{helvet}}
\renewcommand{{\familydefault}}{{\sfdefault}}
\usepackage{{microtype}}
\usepackage[hidelinks]{{hyperref}}
\usepackage{{parskip}}
\usepackage[none]{{hyphenat}}

% --- Page Setup ---
\pagestyle{{empty}}
\pagenumbering{{gobble}}
\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{4pt}}
\raggedright
\hyphenpenalty=10000
\exhyphenpenalty=10000
\emergencystretch=3em

% --- Hyperref setup ---
\hypersetup{{
    colorlinks=true,
    linkcolor=black,
    urlcolor=blue,
    citecolor=black,
    pdfcreator={{ApplyPilot}},
    pdfproducer={{pdflatex}}
}}

\begin{{document}}
\small

{full_name} \\
{contact_line} \\[12pt]

\today \\[12pt]

{company_block}

Dear {salutation},

{body}

\vspace{{12pt}}

Sincerely, \\
{full_name}

\end{{document}}
"""


# ─── Helpers ────────────────────────────────────────────────────────────────


def _sh(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> tuple[int, str, str]:
    """Run command, return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except FileNotFoundError:
        return -1, "", "command not found"


def _latexmk_available() -> bool:
    # MiKTeX ships a latexmk launcher even when its Perl engine is absent.
    return shutil.which("latexmk") is not None and (os.name != "nt" or shutil.which("perl") is not None)


def _pdflatex_available() -> bool:
    return shutil.which("pdflatex") is not None


def _pdfinfo_available() -> bool:
    return shutil.which("pdfinfo") is not None


def _pdftotext_available() -> bool:
    return shutil.which("pdftotext") is not None


def _page_count_pypdf(pdf_path: Path) -> int | None:
    """Get page count using pypdf (always available)."""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        return len(reader.pages)
    except Exception as e:
        log.debug("pypdf page count failed: %s", e)
        return None


def _page_count_pdfinfo(pdf_path: Path) -> int | None:
    """Get page count using pdfinfo (optional CLI)."""
    if not _pdfinfo_available():
        return None
    rc, out, err = _sh(["pdfinfo", str(pdf_path)])
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":")[1].strip())
            except ValueError:
                pass
    return None


def _page_count_pdftotext(pdf_path: Path) -> int | None:
    """Get page count using pdftotext (optional CLI, counts form feeds)."""
    if not _pdftotext_available():
        return None
    try:
        probe = pdf_path.with_suffix(".page_probe.txt")
        rc, out, err = _sh(["pdftotext", "-layout", str(pdf_path), str(probe)])
        if rc != 0 or not probe.exists():
            return None
        text = probe.read_text(encoding="utf-8", errors="ignore")
        probe.unlink(missing_ok=True)
        ff = text.count("\f")
        return ff if ff > 0 else (1 if text.strip() else None)
    except Exception:
        return None


def get_page_count(pdf_path: Path) -> int | None:
    """Get page count with multiple fallbacks."""
    for fn in (_page_count_pypdf, _page_count_pdfinfo, _page_count_pdftotext):
        count = fn(pdf_path)
        if count is not None:
            return count
    return None


def _extract_text_pypdf(pdf_path: Path) -> str:
    """Extract text using pypdf."""
    try:
        reader = pypdf.PdfReader(str(pdf_path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        log.debug("pypdf text extraction failed: %s", e)
        return ""


def _compile_latex(tex_path: Path, strict_one_page: bool = True, auto_compact: bool = True) -> Path:
    """Compile .tex to PDF using latexmk → pdflatex×2 fallback.

    Args:
        tex_path: Path to .tex file
        strict_one_page: If True, fail if PDF > 1 page
        auto_compact: If True and >1 page, try one compaction pass

    Returns:
        Path to generated PDF

    Raises:
        RuntimeError: On compilation failure or page count violation
    """
    work_dir = tex_path.parent
    base_name = tex_path.stem
    pdf_path = work_dir / f"{base_name}.pdf"
    log_path = work_dir / f"{base_name}.log"
    precompact_path = work_dir / f"{base_name}.precompact.tex"

    def run_compile(tex_file: Path) -> tuple[int, str, str]:
        """Run latexmk or pdflatex×2."""
        if _latexmk_available():
            return _sh(["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", tex_file.name], cwd=work_dir)
        if not _pdflatex_available():
            raise RuntimeError("Neither latexmk nor pdflatex found on PATH")
        # Two-pass pdflatex
        rc1, out1, err1 = _sh(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", tex_file.name], cwd=work_dir)
        if rc1 != 0:
            return rc1, out1, err1
        rc2, out2, err2 = _sh(["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-file-line-error", tex_file.name], cwd=work_dir)
        return rc2, out1 + out2, err1 + err2

    # First compilation
    rc, out, err = run_compile(tex_path)
    if rc != 0:
        raise RuntimeError(f"LaTeX compilation failed (exit {rc}): {err or out}")

    if not pdf_path.exists():
        raise RuntimeError(f"PDF not produced: {pdf_path}")

    # Check log for fatal errors
    if log_path.exists():
        log_text = log_path.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"! LaTeX Error:|Fatal error occurred|Emergency stop", log_text):
            raise RuntimeError("LaTeX log contains fatal error")
        if re.search(r"Overfull \\hbox", log_text):
            log.warning("LaTeX reported overfull hbox — tighten content if visually significant")

    # Page count check
    pages = get_page_count(pdf_path)
    if pages is None:
        log.warning("Could not determine page count (no pdfinfo/pdftotext/pypdf fallback)")
    elif strict_one_page and pages != 1:
        if auto_compact:
            # Controlled compaction fallback
            log.warning("Generated resume has %d pages; applying controlled compact-layout fallback", pages)
            # Backup original
            if not precompact_path.exists():
                shutil.copy2(tex_path, precompact_path)
            # Apply compaction: remove \vfill, tighten itemize spacing
            tex_content = tex_path.read_text(encoding="utf-8")
            tex_content = re.sub(r'(?m)^\s*\\vfill\s*\r?\n?', '', tex_content)
            tex_content = re.sub(r'topsep\s*=\s*[\d.]+\s*pt', 'topsep=2pt', tex_content)
            tex_content = re.sub(r'itemsep\s*=\s*[\d.]+\s*pt', 'itemsep=2pt', tex_content)
            tex_path.write_text(tex_content, encoding="utf-8")

            # Recompile
            rc2, out2, err2 = run_compile(tex_path)
            if rc2 != 0:
                # Restore backup
                if precompact_path.exists():
                    shutil.copy2(precompact_path, tex_path)
                raise RuntimeError(f"LaTeX recompilation after compaction failed: {err2 or out2}")

            pages = get_page_count(pdf_path)
            if strict_one_page and pages != 1:
                raise RuntimeError(f"Strict one-page check failed after compaction: {pages} pages")

            log.info("Compaction successful: %d page(s)", pages or 0)
        else:
            raise RuntimeError(f"Strict one-page check failed: {pages} pages")

    # Text extraction verification
    extracted = _extract_text_pypdf(pdf_path)
    profile = load_profile()
    full_name = profile.get("personal", {}).get("full_name", "")
    email = profile.get("personal", {}).get("email", "")
    if full_name and full_name.lower() not in extracted.lower():
        raise RuntimeError(f"PDF text extraction missing candidate name '{full_name}'")
    if email and email.lower() not in extracted.lower():
        raise RuntimeError(f"PDF text extraction missing candidate email '{email}'")

    # File size sanity check
    if pdf_path.stat().st_size < 5000:
        raise RuntimeError(f"Generated PDF too small ({pdf_path.stat().st_size} bytes)")

    return pdf_path


def _application_filename(profile: dict, company: str, role: str) -> str:
    """Generate professional application filename: <First>_<Last>_<Company>_<Role>.pdf"""
    def sanitize(text: str, max_len: int) -> str:
        part = re.sub(r'[^A-Za-z0-9]+', '_', text).strip('_')
        if not part:
            part = 'Job'
        return part[:max_len].rstrip('_')

    personal = profile.get("personal", {})
    first = personal.get("preferred_name") or (personal.get("full_name", "").split()[0] if personal.get("full_name") else "Candidate")
    last = personal.get("full_name", "").split()[-1] if personal.get("full_name") and " " in personal.get("full_name", "") else ""

    first_part = sanitize(first, 50)
    company_part = sanitize(company, 40)
    role_part = sanitize(role, 65)

    if last:
        last_part = sanitize(last, 50)
        return f"{first_part}_{last_part}_{company_part}_{role_part}.pdf"
    return f"{first_part}_{company_part}_{role_part}.pdf"


def _artifact_path(tex_path: Path, profile: dict, company: str, role: str) -> Path:
    """Generate artifact JSON path."""
    return tex_path.with_name(f"{tex_path.stem}_artifact.json")


def _load_artifact(artifact_path: Path, source_tex: Path) -> dict | None:
    """Load existing artifact if valid."""
    try:
        data = json.loads(artifact_path.read_text(encoding="utf-8"))
        if data.get("status") != "ready-for-upload":
            return None
        pdf_file = Path(data["path"])
        if not pdf_file.exists() or pdf_file.stat().st_size < 5000:
            return None
        if data.get("source_tex") != str(source_tex):
            return None
        # Verify hash
        actual_hash = hashlib.sha256(pdf_file.read_bytes()).hexdigest().lower()
        if actual_hash != data.get("sha256", "").lower():
            return None
        return data
    except Exception:
        return None


def _save_artifact(artifact_path: Path, data: dict) -> None:
    """Atomically save artifact JSON."""
    tmp = artifact_path.with_suffix(f".tmp.{os.getpid()}.{hashlib.md5(str(time.time()).encode()).hexdigest()[:8]}")
    try:
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(artifact_path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _select_template_style(job: dict, tailored_data: dict) -> dict:
    """Select template style parameters based on job/tailored data.

    Heuristic: AI/ML/NLP keywords → AI style; Backend/Platform/Distributed → Backend style.
    Defaults to the backend/platform style; AI styling is selected only when
    the role evidence is more AI-specific than platform-specific.
    """
    title = (job.get("title") or "").lower()
    description = (job.get("full_description") or "").lower()

    ai_keywords = ["ai", "ml", "machine learning", "nlp", "llm", "generative", "applied ai", "agent", "knowledge graph", "embedding", "vector", "semantic"]
    backend_keywords = ["backend", "platform", "distributed", "infrastructure", "devops", "sre", "microservice", "kubernetes", "kafka", "postgres", "redis", "aws", "cloud"]

    ai_score = sum(1 for kw in ai_keywords if kw in title or kw in description)
    backend_score = sum(1 for kw in backend_keywords if kw in title or kw in description)

    # Also check tailored data for project types
    projects = tailored_data.get("projects", [])
    for p in projects:
        header = (p.get("header") or "").lower()
        if any(kw in header for kw in ["ai", "ml", "nlp", "llm", "agent", "graph", "embedding", "vector"]):
            ai_score += 1
        if any(kw in header for kw in ["backend", "platform", "distributed", "orchestration", "rendering", "kubernetes"]):
            backend_score += 1

    if ai_score > backend_score:
        return AI_TEMPLATE_PARAMS
    return BACKEND_TEMPLATE_PARAMS


def build_resume_latex(profile: dict, job: dict, tailored_data: dict) -> str:
    """Build .tex content from tailored resume JSON data using canonical-style template."""
    personal = profile.get("personal", {})
    full_name = _escape_latex(personal.get("full_name", ""))
    email = personal.get("email", "")  # email doesn't need escaping in href
    phone = _escape_latex(personal.get("phone", ""))
    linkedin = personal.get("linkedin_url", "")
    github = personal.get("github_url", "")
    location = _escape_latex(personal.get("city", "") or personal.get("location", ""))

    # Headline from tailored data or profile
    headline = _escape_latex(tailored_data.get("headline") or personal.get("headline") or "Software Engineer")

    # LinkedIn/GitHub display names (strip protocol)
    linkedin_display = linkedin.replace("https://", "").replace("http://", "") if linkedin else ""
    github_display = github.replace("https://", "").replace("http://", "") if github else ""

    contact_parts = []
    if email:
        contact_parts.append(f"\\href{{mailto:{email}}}{{{email}}}")
    if phone:
        contact_parts.append(phone)
    if location:
        contact_parts.append(location)
    if linkedin:
        contact_parts.append(f"\\href{{{linkedin}}}{{LinkedIn}}")
    if github:
        contact_parts.append(f"\\href{{{github}}}{{GitHub}}")
    # Select template style
    style = _select_template_style(job, tailored_data)

    # Skills section
    skills = tailored_data.get("skills", {})
    if skills:
        skills_lines = []
        for category, items in skills.items():
            if items:
                skills_lines.append(f"\\textbf{{{_escape_latex(category)}}}: {_escape_latex(items)}")
        skills_text = "\n\n".join(skills_lines)
        skills_section = f"% --- Skills ---\n\\section*{{Technical Skills}}\n{skills_text}"
    else:
        skills_section = ""

    # Experience section (using \role macro)
    exp_lines = []
    for exp in tailored_data.get("experience", []):
        company = _escape_latex(exp.get("company") or exp.get("header", ""))
        dates = _escape_latex(exp.get("dates") or exp.get("subtitle", ""))
        title = _escape_latex(exp.get("title") or "")
        loc = _escape_latex(exp.get("location") or "")
        bullets = exp.get("bullets", [])

        if company or title:
            exp_lines.append(f"\\role{{{company}}}{{{dates}}}{{{title}}}{{{loc}}}")

        if bullets:
            exp_lines.append("\\begin{itemize}")
            for b in bullets:
                exp_lines.append(f"    \\item {_escape_latex(b)}")
            exp_lines.append("\\end{itemize}")

        if exp.get("detail"):
            exp_lines.append(_escape_latex(exp["detail"]))

        # Stack line (footnotesize)
        stack = exp.get("stack")
        if stack:
            exp_lines.append(f"{{\\footnotesize \\textbf{{Stack:}} {_escape_latex(stack)}}}")

    if exp_lines:
        experience_text = "\n\n".join(exp_lines)
        experience_section = f"% --- Experience ---\n\\section*{{Professional Experience}}\n{experience_text}"
    else:
        experience_section = ""

    # Projects section (AI style - "Selected AI & Knowledge-Graph Projects" or generic)
    projects = tailored_data.get("projects", [])
    if projects:
        proj_lines = []
        for p in projects:
            header = _escape_latex(p.get("header", ""))
            subtitle = _escape_latex(p.get("subtitle", ""))
            bullets = p.get("bullets", [])
            if header:
                if subtitle:
                    proj_lines.append(f"\\textbf{{{header}}} \\hfill \\textit{{{subtitle}}}")
                else:
                    proj_lines.append(f"\\textbf{{{header}}}")
            if bullets:
                proj_lines.append("\\begin{itemize}")
                for b in bullets:
                    proj_lines.append(f"    \\item {_escape_latex(b)}")
                proj_lines.append("\\end{itemize}")
        projects_text = "\n\n".join(proj_lines)
        projects_section = f"% --- Projects ---\n\\section*{{Selected Projects}}\n{projects_text}"
    else:
        projects_section = ""

    # Open Source / Production Platforms section (Backend style)
    opensource = tailored_data.get("opensource", [])
    if opensource:
        os_lines = []
        for o in opensource:
            header = _escape_latex(o.get("header", ""))
            subtitle = _escape_latex(o.get("subtitle", ""))
            bullets = o.get("bullets", [])
            if header:
                if subtitle:
                    os_lines.append(f"\\textbf{{{header}}} \\hfill \\textit{{{subtitle}}}")
                else:
                    os_lines.append(f"\\textbf{{{header}}}")
            if bullets:
                os_lines.append("\\begin{itemize}")
                for b in bullets:
                    os_lines.append(f"    \\item {_escape_latex(b)}")
                os_lines.append("\\end{itemize}")
            stack = o.get("stack")
            if stack:
                os_lines.append(f"{{\\footnotesize \\textbf{{Stack:}} {_escape_latex(stack)}}}")
        opensource_text = "\n\n".join(os_lines)
        opensource_section = f"% --- Open Source / Platforms ---\n\\section*{{Open-Source & Production Platforms}}\n{opensource_text}"
    else:
        opensource_section = ""

    # Earlier Experience section (Backend style)
    earlier = tailored_data.get("earlier_experience", [])
    if earlier:
        earlier_lines = []
        for e in earlier:
            line = e.get("line", "")
            if line:
                earlier_lines.append(_escape_latex(line))
        if earlier_lines:
            earlier_text = "\n\n".join(earlier_lines)
            earlier_experience_section = f"% --- Earlier Experience ---\n\\section*{{Earlier Experience}}\n{earlier_text}"
        else:
            earlier_experience_section = ""
    else:
        earlier_experience_section = ""

    # Education section
    edu_lines = []
    for edu in tailored_data.get("education", []):
        inst = _escape_latex(edu.get("institution", ""))
        deg = _escape_latex(edu.get("degree", ""))
        dat = _escape_latex(edu.get("dates", ""))
        langs = _escape_latex(edu.get("languages", ""))
        line = f"\\textbf{{{inst}}}"
        if deg:
            line += f" --- {deg}"
        if dat:
            line += f" \\hfill {dat}"
        if langs:
            line += f" \\quad|\\quad \\textbf{{Languages:}} {langs}"
        edu_lines.append(line)
    if edu_lines:
        education_text = "\n\n".join(edu_lines)
        education_section = f"% --- Education ---\n\\section*{{Education}}\n{education_text}"
    else:
        education_section = ""

    from .resume_blocks import STYLE_PRESETS
    presentation = (tailored_data.get("layout") or {}).get("style", tailored_data.get("resume_style", "classic"))
    design = STYLE_PRESETS.get(presentation, STYLE_PRESETS["classic"])
    name_style = r"\Large"
    if presentation in {"technical", "editorial"}:
        style = {**style, "margin_top": f"{design['margin']}in", "margin_bottom": f"{design['margin']}in"}
        family = r"\rmfamily" if presentation == "editorial" else r"\sffamily"
        name_style = family + f"\\fontsize{{{design['name_size']}}}{{{design['name_size'] + 4}}}\\selectfont"
    # Merge style params with template
    template_params = {
        "header_environment": "center" if design["align"] == "center" else "flushleft",
        "name_style": name_style,
        "contact_line": r" \quad|\quad ".join(contact_parts),
        "full_name": full_name,
        "headline": headline,
        "location": location,
        "location_line": location + r"\\[0.2em]" if location else "",
        "email": email,
        "linkedin": linkedin,
        "linkedin_display": linkedin_display,
        "github": github,
        "github_display": github_display,
        "summary": _escape_latex(tailored_data.get("summary", "")),
        "skills_section": skills_section,
        "experience_section": experience_section,
        "projects_section": projects_section,
        "opensource_section": opensource_section,
        "earlier_experience_section": earlier_experience_section,
        "education_section": education_section,
        **style,
    }

    fragments = {"summary": ("\\section*{Summary}\n" + template_params["summary"]) if tailored_data.get("summary") else "",
        "skills": skills_section, "experience": experience_section, "projects": projects_section,
        "opensource": opensource_section, "earlier_experience": earlier_experience_section,
        "education": education_section}
    for key in ("certifications", "languages"):
        items = tailored_data.get(key) or []
        if items:
            fragments[key] = "\\section*{" + key.title() + "}\n" + "\n\n".join(_escape_latex(item) for item in items)
    order = tailored_data.get("section_order")
    if order is None and tailored_data.get("layout") is not None:
        from .resume_blocks import plan_for
        order = [block["type"] for block in plan_for(tailored_data)["blocks"]]
    template_params["body_sections"] = "\n\n".join(fragments.get(key, "") for key in dict.fromkeys(order if order is not None else fragments))
    return RESUME_LATEX_TEMPLATE.format(**template_params)


def build_cover_latex(profile: dict, job: dict, cover_text: str) -> str:
    """Build .tex content from cover letter text using canonical-style template."""
    personal = profile.get("personal", {})
    full_name = _escape_latex(personal.get("full_name", ""))
    email = personal.get("email", "")
    phone = _escape_latex(personal.get("phone", ""))
    linkedin = personal.get("linkedin_url", "")

    contact_parts = []
    if email:
        contact_parts.append(f"\\href{{mailto:{email}}}{{{email}}}")
    if phone:
        contact_parts.append(phone)
    if linkedin:
        contact_parts.append(f"\\href{{{linkedin}}}{{LinkedIn}}")
    contact_line = " \\quad|\\quad ".join(contact_parts)

    company = _escape_latex(job.get("company", "Hiring Team"))
    # Simple parsing: paragraphs separated by blank lines
    paragraphs = [p.strip() for p in cover_text.split("\n\n") if p.strip()]
    body = "\n\n".join(_escape_latex(p) for p in paragraphs)

    # Build company block (hiring manager, company, optional address)
    company_block = f"{company} \\\\"  # Always at least company name
    # Note: company_address is not typically available from job data, so we skip it

    return COVER_LETTER_LATEX_TEMPLATE.format(
        full_name=full_name,
        contact_line=contact_line,
        hiring_manager="Hiring Manager",
        company_block=company_block,
        salutation="Hiring Manager",
        body=body,
    )


def render_pdf_latex(
    tex_content: str,
    output_pdf: Path,
    *,
    profile: dict,
    company: str,
    role: str,
    job: dict | None = None,
    strict_one_page: bool = True,
    auto_compact: bool = True,
) -> dict:
    """Render LaTeX to PDF with full gates and artifact manifest.

    Args:
        tex_content: Complete .tex source
        output_pdf: Path to write final application PDF
        profile: User profile dict
        company: Company name (for filename + manifest)
        role: Role title (for filename + manifest)
        job: Optional job dict for artifact job_id
        strict_one_page: Enforce one page
        auto_compact: Allow one compaction pass

    Returns:
        Artifact dict with path, sha256, pages, etc.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        work_dir = Path(tmpdir)
        tex_path = work_dir / "resume.tex"
        tex_path.write_text(tex_content, encoding="utf-8")

        artifact_path = _artifact_path(tex_path, profile, company, role)

        # Check for existing valid artifact
        existing = _load_artifact(artifact_path, tex_path)
        if existing:
            log.info("Reusing existing artifact: %s", existing["path"])
            shutil.copy2(existing["path"], output_pdf)
            return existing

        # Compile
        pdf_path = _compile_latex(tex_path, strict_one_page=strict_one_page, auto_compact=auto_compact)

        # Build artifact
        app_filename = _application_filename(profile, company, role)
        app_pdf = work_dir / app_filename
        shutil.copy2(pdf_path, app_pdf)
        sha256 = hashlib.sha256(app_pdf.read_bytes()).hexdigest().lower()
        pages = get_page_count(app_pdf)

        artifact = {
            "job_id": job.get("url", "") if job else "",
            "company": company,
            "title": role,
            "filename": app_filename,
            "path": str(app_pdf),
            "sha256": sha256,
            "pages": pages,
            "source_tex": str(tex_path),
            "generic_compile_pdf": str(pdf_path),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready-for-upload",
        }

        _save_artifact(artifact_path, artifact)

        # Copy to final destination
        shutil.copy2(app_pdf, output_pdf)

        return artifact

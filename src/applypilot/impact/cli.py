"""CLI for the Impact Evidence Ledger (`applypilot impact`).

CRUD + export/import + LLM drafting + review + recovery. Mirrors the
``evidence`` command's conventions (typer app, ``_bootstrap()`` before any LLM
import, rich tables).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import typer

from applypilot.config import load_profile
from applypilot.impact import draft as draft_mod
from applypilot.impact.ledger import (
    LedgerCorruptionError,
    LedgerValidationError,
    default_repository,
)
from applypilot.impact.models import ImpactRecord, _now_iso

log = logging.getLogger(__name__)

impact_app = typer.Typer(
    name="impact",
    help="Impact Evidence Ledger — measured accomplishments backing resume/cover letter claims.",
    no_args_is_help=True,
)


def _bootstrap() -> None:
    # Load ~/.applypilot/.env before any LLM import (llm.py reads env at import).
    from applypilot.cli import _bootstrap as _cli_bootstrap
    _cli_bootstrap()


def _utility(operation, payload):
    from applypilot.orchestration.utility_client import run_utility
    result = run_utility(operation, payload)
    for key in ("authored", "rejected", "updated", "skipped"):
        if isinstance(result.get(key), list):
            result[key] = [ImpactRecord.from_dict(row) if isinstance(row, dict) and "scope" in row else row
                           for row in result[key]]
    return result


def _draft_bundle(bundle, *, ask_max=0, **options):
    result = _utility("impact_bundle_draft", {"bundle": bundle, **options})
    if ask_max > 0 and not result.get("errors"):
        _, per_source, _ = draft_mod._render_bundle(bundle.get("sources") or [])
        confirmed = draft_mod._interactive_measurement_confirm(default_repository(),
            result.get("authored", []) + result.get("rejected", []), per_source, ask_max)
        ids = {record.id for record in confirmed}
        result["authored"] = [record for record in result["authored"] if record.id not in ids] + confirmed
        result["rejected"] = [record for record in result["rejected"] if record.id not in ids]
    return result


def _draft_source(source_text, source_name="resume.txt", **options):
    return _draft_bundle({"sources": [{"id": source_name, "type": "resume", "text": source_text}]}, **options)


@impact_app.command("status")
def impact_status() -> None:
    """Ledger health: path, state, counts by review status."""
    repo = default_repository()
    status = repo.status()
    state_colors = {"ok": "green", "recovered_from_bak": "yellow", "readonly": "red"}
    typer.echo(f"Ledger:  {status.path}")
    typer.echo(f"State:   [{state_colors.get(status.state, 'white')}]{status.state}[/{state_colors.get(status.state, 'white')}]")
    typer.echo(f"Records: {status.records}")
    if status.warning:
        typer.echo(f"[yellow]Warning: {status.warning}[/yellow]")
    if status.readonly_reason:
        typer.echo(f"[red]Read-only reason: {status.readonly_reason}[/red]")


@impact_app.command("list")
def impact_list(
    all_statuses: bool = typer.Option(False, "--all", help="Include drafted/rejected records"),
) -> None:
    """List approved records (add --all for drafted/rejected too)."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    repo = default_repository()
    records = repo.list_records()
    if not records:
        console.print("[dim]Ledger is empty. Try `applypilot impact draft`.[/dim]")
        return
    shown = [r for r in records if all_statuses or r.review_status not in ("drafted", "rejected")]
    if not shown:
        console.print("[dim]No approved records yet; use `applypilot impact draft` or `--all` to see pending.[/dim]")
        return
    t = Table(title=f"Impact Evidence Ledger ({len(shown)} shown / {len(records)} total)")
    t.add_column("ID", style="cyan")
    t.add_column("Scope")
    t.add_column("Problem", max_width=45)
    t.add_column("Metric", max_width=40)
    t.add_column("Status")
    for r in shown:
        status_color = {"auto_approved": "green", "reviewed": "green",
                        "drafted": "yellow", "rejected": "red"}.get(r.review_status, "white")
        scope = r.scope.company or r.scope.project or r.scope.role or r.scope.type
        # FLATTEN: one row per metric so two KPIs legitimately grounded on the
        # same resume bullet (e.g. "Distributed workflow reliability ↓80%" and
        # "Payment resilience 5%→2%") both appear, each under the same record
        # id, instead of only the single "best" metric. The underlying evidence
        # ledger keeps one ImpactRecord; this is purely a display change.
        metrics = r.measurements or [None]
        for m in metrics:
            if m is None:
                label = "unmeasured"
            else:
                label = (m.claim or "")[:40]
                if m.has_numeric_evidence():
                    vt = _metric_value_text(m)
                    if vt and vt != "—":
                        if (not getattr(m, "quantity_text", None) and m.unit
                                and vt[-1] != "%" and m.value_qualifier != "range"):
                            vt = f"{vt} {m.unit}"
                        label = f"{label} ({vt})"
            t.add_row(r.id, scope or "—", (r.problem or "—")[:45], label,
                      f"[{status_color}]{r.review_status}[/{status_color}]")
    console.print(t)


@impact_app.command("show")
def impact_show(
    record_id: str,
    brief: bool = typer.Option(False, "--brief", help="Also render the Impact Defense Card for this record"),
    all_: bool = typer.Option(False, "--all", help="Show every metric in full detail (value, method, source text)"),
    all_statuses: bool = typer.Option(False, "--all-statuses", help="With record_id='all', include drafted/rejected records"),
) -> None:
    """Show a full record, or 'all' for every record (--all for per-metric detail)."""
    from rich.console import Console
    from rich.panel import Panel

    console = Console()
    repo = default_repository()
    if record_id == "all":
        records = repo.list_records()
        if not records:
            console.print("[dim]Ledger is empty. Try `applypilot impact draft`.[/dim]")
            return
        shown = [r for r in records if all_statuses or r.review_status not in ("drafted", "rejected")]
        if not shown:
            console.print("[dim]No approved records; use `--all-statuses` to include drafted/rejected.[/dim]")
            return
        for record in shown:
            _print_show_panel(console, Panel, record, all_)
            if brief:
                _render_brief(record, job=None, make_answers=False, json_out=False, console=console)
        console.print(f"[dim]{len(shown)} record(s).[/dim]")
        return
    record = repo.get(record_id)
    if record is None:
        raise typer.BadParameter(f"No record {record_id!r}")
    _print_show_panel(console, Panel, record, all_)
    if brief:
        _render_brief(record, job=None, make_answers=False, json_out=False, console=console)


def _metric_value_text(m) -> str:
    """Render a metric's numeric value for display, honoring ranges/bounds.

    A range (`50k-100k`) shows `min-max`; an approximate/bound value keeps its
    qualifier so the displayed number never invents precision the source lacks.
    A percent-reduction with only a delta (`reduced 25%`) shows the change.
    """
    if getattr(m, "quantity_text", None):
        return m.quantity_text
    if m.value_qualifier == "range" and m.value_min is not None and m.value_max is not None:
        return f"{_fnum(m.value_min)}-{_fnum(m.value_max)}"
    if m.value is not None:
        prefix = {
            "approximate": "~",
            "less_than": "<",
            "at_most": "≤",
            "greater_than": ">",
            "at_least": "≥",
        }.get(m.value_qualifier, "")
        return f"{prefix}{_fnum(m.value)}"
    if m.representation == "percent_change" and m.delta_percent is not None:
        neg = m.delta_percent > 0 and (m.direction == "decrease")
        sign = "-" if neg else "+" if m.delta_percent > 0 else ""
        return f"{sign}{_fnum(m.delta_percent)}%"
    return "—"


def _fnum(x) -> str:
    f = float(x)
    return str(int(f)) if f == int(f) else str(round(f, 3))


def _build_show_text(record: ImpactRecord, all_: bool) -> list[str]:
    """Build the rich body lines for `impact show` (single record or each of 'all')."""
    lines: list[str] = []
    lines.append(f"ID:         {record.id}")
    lines.append(f"Status:     {record.review_status}")
    lines.append(f"Scope:      {record.scope.to_dict()}")
    lines.append(f"Attribution:{record.attribution}  Confidence: {record.confidence}  Recency: {record.recency}")
    lines.append(f"Problem:    {record.problem}")
    lines.append(f"Intervention:{record.intervention.summary}")
    if record.intervention.skills:
        lines.append(f"Skills:     {', '.join(record.intervention.skills)}")
    for m in record.measurements:
        if all_:
            _render_metric_all(lines, m)
        else:
            src = f"  [source: {m.source_ref}]" if m.source_ref else ""
            lines.append(f"\nMETRIC ({m.layer}, {m.measurement_status}): {m.claim}{src}")
            if m.has_numeric_evidence():
                lines.append(f"  quantity={_metric_value_text(m)} unit={m.unit} direction={m.direction} rep={m.representation}")
    for o in record.outcomes:
        lines.append(f"\nOUTCOME ({o.layer}, {o.claimability}): {o.claim}")
    iv = record.interview_evidence
    if iv is not None:
        lines.append("\nINTERVIEW EVIDENCE:")
        if iv.impact_headline:
            lines.append(f"  headline: {iv.impact_headline}")
        for m in record.measurements:
            if m.method and m.method.has_content:
                src = m.method.source or "?"
                conf = m.method.attribution_confidence or "unstated"
                lines.append(f"  method[{m.name}]: source={src} attribution={conf}")
        if iv.tradeoffs:
            lines.append(f"  tradeoffs: {len(iv.tradeoffs)} recorded")
        if iv.decision_consequence and iv.decision_consequence.type != "none":
            lines.append(f"  decision consequence ({iv.decision_consequence.type}): {iv.decision_consequence.description}")
    else:
        lines.append("\nINTERVIEW EVIDENCE: none recorded — run `applypilot impact brief <ID>` to prepare.")
    if record.context:
        lines.append(f"\nContext:  {json.dumps(record.context, indent=2)[:800]}")
    return lines


def _print_show_panel(console, panel_cls, record: ImpactRecord, all_: bool) -> None:
    """Render one record's `impact show` panel (used for single and 'all')."""
    lines = _build_show_text(record, all_)
    console.print(panel_cls("\n".join(lines), title=record.id, expand=False))


def _render_metric_all(lines: list[str], m) -> None:
    """Render one metric in full detail for `impact show --all`."""
    src = f"  [source: {m.source_ref}]" if m.source_ref else ""
    lines.append(f"\nMETRIC ({m.layer}, {m.measurement_status}): {m.claim}{src}")
    if m.has_numeric_evidence():
        lines.append(f"  quantity={_metric_value_text(m)} unit={m.unit} direction={m.direction} rep={m.representation} qualifier={m.value_qualifier}")
    if m.delta_percent is not None:
        lines.append(f"  delta_percent={m.delta_percent}")
    if m.baseline or m.result:
        lines.append(f"  baseline={m.baseline!r} result={m.result!r}")
    if m.source_text:
        lines.append(f"  source_text: {m.source_text}")
    if m.method is not None:
        lines.append("  method:")
        lines.append(f"    comparison_method={m.method.comparison_method}")
        lines.append(f"    baseline_period={m.method.baseline_period}")
        lines.append(f"    result_period={m.method.result_period}")
        lines.append(f"    denominator={m.method.denominator}")
        lines.append(f"    sample_size={m.method.sample_size}")
        lines.append(f"    source={m.method.source}")
        lines.append(f"    attribution_confidence={m.method.attribution_confidence}")
        if m.method.confounders:
            lines.append(f"    confounders={', '.join(m.method.confounders)}")


def _render_brief(record: ImpactRecord, job: dict | None, make_answers: bool,
                  json_out: bool, console=None) -> None:
    """Shared rendering for `impact brief` and `show --brief`.

    Builds the deterministic DefenseFacts, renders the card, and (optionally)
    spends LLM calls on narrative answers. Director follow-ups come from the
    brief's own JSON — ONE generation path, never a second call.
    """
    from applypilot.impact.brief import render_defense_card
    from applypilot.impact.defense import JobSignals, build_defense_facts

    if console is None:
        from rich.console import Console
        console = Console()
    facts = build_defense_facts(record, job=job, job_signals=JobSignals())
    if make_answers:
        result = _utility("impact_brief", {"record": record.to_dict(), "job": job})
        card, brief = result["card"], result["brief"]
    else:
        card = render_defense_card(facts, record, job)
    console.print(card)
    if not make_answers:
        console.print("\n[dim]Add --answers to generate narrative answers and director follow-ups "
                      "(uses LLM credits).[/dim]")
        return

    console.print("\n[bold]ANSWER MODES[/bold]")
    depths = brief.get("answer_depths") or {}
    for label, key in (
        ("10-sec headline", "headline_10s"),
        ("45-sec executive", "executive_45s"),
        ("90-sec structured", "structured_90s"),
        ("3-5 min deep dive", "deep_dive_3to5m"),
    ):
        txt = depths.get(key, "")
        console.print(f"\n[bold cyan]{label}:[/bold cyan]\n{txt}" if txt else f"\n[dim]{label}: (not generated)[/dim]")
    if brief.get("business_framing"):
        console.print(f"\n[bold]BUSINESS FRAMING:[/bold]\n{brief['business_framing']}")
    if brief.get("technical_drill_down"):
        console.print(f"\n[bold]TECHNICAL DRILL-DOWN:[/bold]\n{brief['technical_drill_down']}")
    questions = brief.get("director_followups") or []
    if not questions:
        from applypilot.impact.brief import _fallback_questions
        questions = _fallback_questions(facts)
    console.print("\n[bold]DIRECTOR FOLLOW-UPS[/bold]")
    for i, q in enumerate(questions, start=1):
        console.print(f"  {i}. {q}")


def _load_job_from_db(job_url: str) -> dict | None:
    """Fetch the real JD row so the brief targets the actual employer instead of
    a bare URL string. Returns None when there is no DB or no matching row.
    """
    try:
        from applypilot.database import get_connection

        conn = get_connection()
        row = None
        for col in ("url", "application_url"):
            try:
                row = conn.execute(
                    f"SELECT * FROM jobs WHERE {col} = ? LIMIT 1", (job_url,)
                ).fetchone()
            except Exception:  # noqa: BLE001
                row = None
            if row is not None:
                break
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    d = {k: row[k] for k in row.keys()}
    return {
        "url": d.get("url") or job_url,
        "title": d.get("title") or job_url,
        "company": d.get("company") or d.get("site") or "",
        "site": d.get("site") or "",
        "location": d.get("location") or "",
        "description": d.get("description") or "",
        "full_description": d.get("full_description") or "",
        "selected_evidence": d.get("selected_evidence"),
        "salary_p25": d.get("salary_p25"),
        "salary_currency": d.get("salary_currency"),
    }


@impact_app.command("brief")
def impact_brief(
    record_id: str | None = typer.Argument(
        None, help="ImpactRecord id. Omit when --job points at a scored job: the "
                   "top selected-evidence record is resolved automatically.",
    ),
    job_url: str | None = typer.Option(
        None, "--job", "--job-url",
        help="Target job URL/ID to customize the brief to that employer",
    ),
    answers: bool = typer.Option(
        False, "--answers", "--llm",
        help="Also generate narrative answer depths + director follow-ups (uses LLM credits)",
    ),
    no_answers: bool = typer.Option(
        False, "--no-answers",
        help="Only render the deterministic card (never calls the LLM)",
    ),
    json_out: bool = typer.Option(False, "--json", help="Output the deterministic DefenseFacts as JSON"),
) -> None:
    """Interview Defense Brief for one ImpactRecord.

    Deterministic evidence card by default; add --answers to generate the four
    answer depths and 8 director follow-up questions tailored to a target role.
    `impact brief --job <url> --answers` renders a pipeline-generated brief for
    a job already in the apply cohort without needing a manual record id.
    """
    from rich.console import Console
    from applypilot.impact.defense import JobSignals, build_defense_facts

    console = Console()
    repo = default_repository()

    seniority = "senior"
    job = None
    if job_url:
        job = _load_job_from_db(job_url)
        if job is None:
            console.print(f"[yellow]No DB row for {job_url!r}; the brief will not have a real JD.[/yellow]")
            job = {"url": job_url, "title": job_url, "company": "", "site": "",
                   "location": "", "description": "", "full_description": "",
                   "selected_evidence": None}

    if record_id is None:
        leak = _resolve_record_for_job(job, repo)
        if leak is None:
            raise typer.BadParameter(
                "Provide a record_id, or use --job on a scored job whose evidence "
                "selection is stored (run the pipeline first)."
            )
        record_id = leak

    record = repo.get(record_id)
    if record is None:
        raise typer.BadParameter(f"No record {record_id!r}")

    make_answers = answers and not no_answers
    if json_out and not make_answers:
        facts = build_defense_facts(record, job=job, job_signals=JobSignals())
        import json as _json
        console.print(_json.dumps(facts.to_dict(), indent=2, default=str))
        return

    _render_brief(record, job=job, make_answers=make_answers, json_out=json_out, console=console)


def _resolve_record_for_job(job: dict | None, repo) -> str | None:
    """Top selected-evidence record id for a job row (from the pipeline stage)."""
    if not job or not job.get("selected_evidence"):
        return None
    data = job["selected_evidence"]
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return None
    if not isinstance(data, dict):
        return None
    records = data.get("records") or []
    if not records:
        return None
    return records[0].get("id")


@impact_app.command("briefs")
def impact_briefs(
    limit: int = typer.Option(20, "--limit", help="Max recent briefs to list (0 = all)"),
    json_out: bool = typer.Option(False, "--json", help="Emit rows as JSON"),
) -> None:
    """List pipeline-generated interview briefs and the pending backlog."""
    from applypilot.database import get_connection, interview_brief_pending_sql

    conn = get_connection()
    done = conn.execute(
        "SELECT url, title, company, fit_score, interview_brief_path, "
        "       brief_generated_at, brief_attempts "
        "FROM jobs WHERE interview_brief_path IS NOT NULL "
        "  AND interview_brief_path != '' "
        "  AND COALESCE(brief_attempts, 0) >= 1 "
        "ORDER BY brief_generated_at DESC"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    pending = conn.execute('SELECT COUNT(*) FROM jobs j WHERE ' + interview_brief_pending_sql()).fetchone()[0]

    if json_out:
        import json as _json
        typer.echo(_json.dumps({
            "count": len(done),
            "pending": pending,
            "briefs": [{k: r[k] for k in r.keys()} for r in done],
        }, indent=2, default=str))
        return

    from rich.console import Console
    from rich.table import Table

    console = Console()
    table = Table(title='Interview briefs', expand=True)
    table.add_column("title", style="bold", no_wrap=False)
    table.add_column("company")
    table.add_column("score", justify="right")
    table.add_column("generated_at")
    for r in done:
        table.add_row(
            (r["title"] or "")[:60],
            (r["company"] or "")[:24],
            str(r["fit_score"] or "?"),
            (r["brief_generated_at"] or "")[:19],
        )
    console.print(table)
    console.print(f"[dim]{len(done)} briefs on disk; {pending} opportunities ready for preparation.[/dim]")


# ── CRUD ──────────────────────────────────────────────────────────────────

def _load_json_file(path: Path) -> dict | list:
    if not path.exists():
        raise typer.BadParameter(f"File not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid JSON in {path}: {exc}")
    return data


@impact_app.command("add")
def impact_add(
    note: str | None = typer.Argument(
        None,
        help="A natural-language accomplishment to log, e.g. 'cut cache miss rate 40% at Acme'. "
             "Omit to auto-draft from your resume + profile metrics/evidence.",
    ),
    json_file: Path | None = typer.Option(None, "--json", help="Path to a JSON record file (power/scripted use)"),
    skip_interview: bool = typer.Option(
        False, "--skip-interview",
        help="Skip the best-effort interview-evidence pass (faster, less rich records)"),
    ask: int = typer.Option(
        0, "--ask",
        help="After the draft, ask you to verify a number for up to N records the "
             "pipeline could not quantify (measured KPI, no extra LLM call). 0 disables."),
) -> None:
    """Auto-draft impact records — no data-entry forms.

    No-arg: extract grounded records from resume.txt + profile metrics/public
    evidence (the exact bundle `impact draft` uses). Pass a note in natural
    language to draft from just that claim. `--json` imports a structured file.
    """
    _bootstrap()
    repo = default_repository()
    if json_file:
        data = _load_json_file(json_file)
        if not isinstance(data, dict):
            raise typer.BadParameter("--json must contain one record object")
        if "claim_snapshot" in data:
            data = data["claim_snapshot"]
        record = ImpactRecord.from_dict(data)
        if not re.fullmatch(r"IMP-[A-Z0-9]{1,8}-\d{3,}", record.id or ""):
            record.id = _interactive_id(repo, record)
        record.review_status = record.review_status if record.review_status in ("auto_approved", "reviewed", "drafted", "rejected") else "reviewed"
        now = _now_iso()
        record.created_at = record.created_at or now
        record.updated_at = now
        try:
            repo.save(record)
        except (LedgerValidationError, LedgerCorruptionError) as exc:
            raise typer.BadParameter(f"Refused: {exc}")
        typer.echo(f"Saved {record.id} ({record.review_status}).")
        return
    if note and note.strip():
        from applypilot.config import APP_DIR
        from applypilot.backup.diary import save_note
        saved_note = save_note(APP_DIR, {"note": note.strip()})
        typer.echo(f"Original note saved in diary ({saved_note['id']}).")
        from applypilot.onboarding.storage import write_json
        diary_path = APP_DIR / "diary" / (saved_note["id"] + ".json")
        try:
            result = _draft_source(
                note.strip(), source_name="diary-" + saved_note["id"], skip_interview=skip_interview,
                ask_max=ask)
            write_json(diary_path, saved_note | {"impact_state": "completed", "impact_result": json.loads(json.dumps(result, default=str))})
            from applypilot.backup.runtime import request_checkpoint
            request_checkpoint(APP_DIR)
        except Exception:
            write_json(diary_path, saved_note | {"impact_state": "failed", "impact_error": "Extraction failed; the original note is saved."})
            raise
        _print_draft_result(result, context="note")
    else:
        bundle = draft_mod.build_source_bundle()
        result = _draft_bundle(
            bundle, skip_interview=skip_interview, ask_max=ask)
        _print_draft_result(result, context="resume+profile bundle")


@impact_app.command("edit")
def impact_edit(
    record_id: str,
    json_file: Path = typer.Option(..., "--json", help="Path to the replacement record JSON"),
) -> None:
    """Replace a record wholesale from a JSON file."""
    repo = default_repository()
    existing = repo.get(record_id)
    if existing is None:
        raise typer.BadParameter(f"No record {record_id!r}")
    data = _load_json_file(json_file)
    if not isinstance(data, dict):
        raise typer.BadParameter("--json must contain one record object")
    if "claim_snapshot" in data:
        data = data["claim_snapshot"]
    data = dict(data)
    data["id"] = record_id
    record = ImpactRecord.from_dict(data)
    record.created_at = existing.created_at or _now_iso()
    record.updated_at = _now_iso()
    try:
        repo.save(record)
    except (LedgerValidationError, LedgerCorruptionError) as exc:
        raise typer.BadParameter(f"Refused: {exc}")
    typer.echo(f"Updated {record_id}.")


@impact_app.command("rm")
def impact_rm(record_id: str, yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Delete a record."""
    if not yes:
        typer.confirm(f"Delete {record_id}? (this is permanent)", abort=True)
    removed = default_repository().delete(record_id)
    if not removed:
        raise typer.BadParameter(f"No record {record_id!r}")
    typer.echo(f"Deleted {record_id}.")


@impact_app.command("export")
def impact_export(
    out: Path | None = typer.Option(None, "--out", help="Output file (default: stdout)"),
    raw: bool = typer.Option(False, "--raw", help="Export the full ledger file verbatim"),
) -> None:
    """Export approved records as JSON."""
    repo = default_repository()
    if raw:
        payload = repo.path.read_text(encoding="utf-8") if repo.path.exists() else "{}"
    else:
        shown = [r for r in repo.list_records() if r.review_status not in ("rejected", "drafted")]
        payload = json.dumps({"schema_version": 1, "records": [r.to_dict() for r in shown]},
                             indent=2, ensure_ascii=False)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Exported {len(shown) if not raw else 'ledger'} to {out}")
    else:
        typer.echo(payload)


@impact_app.command("import")
def impact_import(
    file: Path = typer.Option(..., "--file", "-f", help="JSON file ({records:[...]} or a bare list)"),
    auto_approve: bool = typer.Option(False, "--approve", help="Mark every record auto_approved"),
) -> None:
    """Bulk-import records from a JSON file (idempotent: same id overwrites)."""
    repo = default_repository()
    data = _load_json_file(file)
    if isinstance(data, dict):
        raw_records = data.get("records") or [data]
    elif isinstance(data, list):
        raw_records = data
    else:
        raise typer.BadParameter("Import file must be {records:[...]} or a list")
    now = _now_iso()
    imported: list[ImpactRecord] = []
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise typer.BadParameter("Each import entry must be a JSON object")
        if "claim_snapshot" in raw:
            raw = raw["claim_snapshot"]
        record = ImpactRecord.from_dict(raw)
        if auto_approve and record.review_status not in ("auto_approved", "reviewed"):
            record.review_status = "auto_approved"
        record.created_at = record.created_at or now
        record.updated_at = now
        imported.append(record)
    # Merge: keep existing records not present in the import file, overwrite
    # those that are.  The old code built merged from only the imported records
    # and called replace_all — any existing record absent from the import file
    # was silently deleted.
    existing = {r.id: r for r in repo.list_records()}
    prior_ids = set(existing)
    for record in imported:
        if not record.id:
            continue
        existing[record.id] = record
    merged = list(existing.values())
    added_ids = {r.id for r in imported if r.id} - prior_ids
    overwritten = len(imported) - len(added_ids)
    preserved = len(merged) - len(imported)
    try:
        repo.replace_all(merged)
    except (LedgerValidationError, LedgerCorruptionError) as exc:
        raise typer.BadParameter(f"Refused: {exc}")
    typer.echo(f"Imported {len(added_ids)} new record(s); {overwritten} overwritten; {preserved} existing preserved.")


@impact_app.command("draft")
def impact_draft(
    source: Path | None = typer.Option(None, "--source", help="Single source document (default: resume.txt + profile metrics/evidence bundle)"),
    no_approve: bool = typer.Option(False, "--no-approve", help="Keep extracted records as 'drafted' for manual review"),
    skip_interview: bool = typer.Option(
        False, "--skip-interview",
        help="Skip the best-effort interview-evidence pass (faster, less rich records)"),
    json_out: Path | None = typer.Option(None, "--json-out", help="Write the source bundle to this file instead of drafting"),
    ask: int = typer.Option(
        0, "--ask",
        help="After the draft, ask you to verify a number for up to N records the "
             "pipeline could not quantify (measured KPI, no extra LLM call). 0 disables."),
) -> None:
    """LLM-extract impact records from resume + profile metrics/evidence; grounded ones auto-approve."""
    _bootstrap()
    if json_out:
        bundle = draft_mod.build_source_bundle(
            resume_text=source.read_text(encoding="utf-8") if source else None
        )
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(bundle, indent=2, ensure_ascii=False), encoding="utf-8")
        typer.echo(f"Source bundle written to {json_out}")
        return
    if source:
        if not source.exists():
            raise typer.BadParameter(f"Source not found: {source}")
        source_text = source.read_text(encoding="utf-8")
        result = _draft_source(
            source_text, source_name=source.name, no_approve_review=no_approve,
            skip_interview=skip_interview, ask_max=ask,
        )
    else:
        bundle = draft_mod.build_source_bundle()
        result = _draft_bundle(
            bundle, no_approve_review=no_approve, skip_interview=skip_interview,
            ask_max=ask,
        )
    if result["errors"]:
        typer.echo("[red]Draft failed:[/red]")
        for err in result["errors"]:
            typer.echo(f"  - {err}")
        raise typer.Exit(code=1)
    for record in result["authored"]:
        typer.echo(f"[green]auto_approved[/green] {record.id}: {record.problem[:70]}")
    for record in result["rejected"]:
        reasons = "; ".join(record.context.get("draft_errors", []))
        typer.echo(f"[yellow]drafted[/yellow]  {record.id}: {record.problem[:60]} — {reasons[:100]}")
    updated = result.get("updated", [])
    skipped = result.get("skipped", [])
    if updated:
        typer.echo(f"[cyan]{len(updated)} duplicate(s) updated in place (same fingerprint).[/cyan]")
    if skipped:
        typer.echo(f"[dim]{len(skipped)} duplicate(s) already approved — skipped.[/dim]")
    persist_errors = result.get("persist_errors", [])
    if persist_errors:
        # Per-record persistence failures are recoverable and are NOT fatal: one
        # invalid record must not abort the rest of the batch. Print them as
        # warnings and finish normally, unlike result["errors"] above which still
        # means the pipeline could not complete.
        typer.echo("[yellow]Skipped invalid record(s) (not persisted):[/yellow]")
        for err in persist_errors:
            typer.echo(f"  {err}")
    typer.echo(f"\n{len(result['authored'])} auto-approved, {len(result['rejected'])} flagged for review.")
    if persist_errors:
        typer.echo(f"[yellow]{len(result['authored']) + len(result['rejected'])} records persisted, "
                   f"{len(persist_errors)} invalid record(s) skipped.[/yellow]")


@impact_app.command("bootstrap")
def impact_bootstrap(
    out: Path | None = typer.Option(None, "--out", help="Write the source bundle to this file"),
    source: Path | None = typer.Option(None, "--source", help="Override the resume source file"),
) -> None:
    """Preview the source bundle feeding `impact draft` (resume + metrics + evidence, provenance preserved)."""
    _bootstrap()
    bundle = draft_mod.build_source_bundle(
        resume_text=source.read_text(encoding="utf-8") if source else None
    )
    payload = json.dumps(bundle, indent=2, ensure_ascii=False)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"Source bundle written to {out}")
    else:
        typer.echo(payload)


@impact_app.command("review")
def impact_review(
    approve: list[str] = typer.Option([], "--approve", help="Record IDs to approve (repeatable)"),
    reject: list[str] = typer.Option([], "--reject", help="Record IDs to reject (repeatable)"),
    list_pending: bool = typer.Option(False, "--list-pending", help="Show drafted records that need review"),
) -> None:
    """Approve/reject drafted records."""
    repo = default_repository()
    if list_pending and not approve and not reject:
        pending = [r for r in repo.list_records() if r.review_status == "drafted"]
        if not pending:
            typer.echo("[dim]No drafted records waiting.[/dim]")
        for r in pending:
            reasons = "; ".join(r.context.get("draft_errors", []))
            typer.echo(f"{r.id}  {r.problem[:60]}  [dim]{reasons[:80]}[/dim]")
        return
    for rid in approve:
        record = repo.get(rid)
        if record is None:
            typer.echo(f"  [red]no record {rid}[/red]")
            continue
        record.review_status = "reviewed"
        record.updated_at = _now_iso()
        repo.save(record)
        typer.echo(f"  approved {rid}")
    for rid in reject:
        record = repo.get(rid)
        if record is None:
            typer.echo(f"  [red]no record {rid}[/red]")
            continue
        record.review_status = "rejected"
        record.updated_at = _now_iso()
        repo.save(record)
        typer.echo(f"  rejected {rid}")


@impact_app.command("dedup")
def impact_dedup(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview merges without modifying the ledger"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Apply without a confirmation prompt"),
) -> None:
    """Re-scan the whole ledger and merge/purge duplicate records (structural bullet-anchor match, not word overlap)."""
    plan = _utility("impact_dedup", {"dry_run": True})
    if not plan["groups"]:
        typer.echo("[dim]No duplicate groups found.[/dim]")
        return
    for entry in plan["groups"]:
        typer.echo(f"[cyan]{entry['winner']}[/cyan] ({entry['scope']}) keeps; "
                    f"purges: {', '.join(entry['purged'])}")
    typer.echo(f"\n{len(plan['groups'])} duplicate group(s), {plan['total_purged']} record(s) to purge.")
    if dry_run:
        return
    if not yes:
        typer.confirm("Apply these merges and purge the duplicates?", abort=True)
    result = _utility("impact_dedup", {"dry_run": False})
    for entry in result["groups"]:
        if entry.get("error"):
            typer.echo(f"  [red]{entry['winner']}: {entry['error']}[/red]")
    typer.echo(f"Purged {result['total_purged']} duplicate record(s).")


@impact_app.command("clean")
def impact_clean(
    yes: bool = typer.Option(False, "--yes", "-y", help="Run without a confirmation prompt"),
    limit: int = typer.Option(0, "--limit", help="Cap unmeasured records to re-check (0 = all)"),
) -> None:
    """Run the complete LLM-assisted ledger cleanup: remeasure, re-anchor, then dedup."""
    _bootstrap()
    if not yes:
        typer.confirm(
            "This calls the LLM to remeasure, re-anchor, and dedup the ledger. Continue?",
            abort=True,
        )

    typer.echo("[cyan]1/2 Rechecking unmeasured records for verifiable metrics...[/cyan]")
    remeasure = _utility("impact_remeasure", {"dry_run": False, "limit": limit or None})
    for err in remeasure["errors"]:
        typer.echo(f"  [red]{err}[/red]")
    typer.echo(f"  {len(remeasure['updated'])} measurement(s) added; "
               f"{len(remeasure['discarded'])} unverified candidate(s) discarded.")

    typer.echo("[cyan]2/2 Re-anchoring stale citations and collapsing duplicates...[/cyan]")
    reconcile = _utility("impact_reconcile", {"dry_run": False})
    for err in reconcile["errors"]:
        typer.echo(f"  [red]{err}[/red]")
    structural = reconcile["structural_dedup"]
    failed = [entry for entry in structural["groups"] + reconcile["llm_duplicates"]
              if entry.get("error")]
    for entry in failed:
        typer.echo(f"  [red]{entry['winner']}: {entry['error']}[/red]")
    purged = (structural["total_purged"]
              + sum(len(entry["purged"]) for entry in reconcile["llm_duplicates"]
                    if not entry.get("error")))
    typer.echo(f"\nCleaned ledger: {len(remeasure['updated'])} measurement(s) added, "
               f"{len(reconcile['reanchored'])} citation(s) re-anchored, "
               f"{purged} duplicate(s) purged.")


@impact_app.command("reconcile")
def impact_reconcile(
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute and report only (still calls the LLM); nothing is written"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run without a confirmation prompt"),
) -> None:
    """LLM-assisted re-anchor of records whose citation no longer resolves against the current resume, then dedup (makes real LLM calls — one run, not a preview+reapply)."""
    _bootstrap()
    if not dry_run and not yes:
        typer.confirm("This calls the LLM to re-anchor and dedup unresolved records. Continue?", abort=True)
    result = _utility("impact_reconcile", {"dry_run": dry_run})
    for err in result["errors"]:
        typer.echo(f"[red]{err}[/red]")
    if result["reanchored"]:
        verb = "would get" if dry_run else "got"
        typer.echo(f"Re-anchor: {len(result['reanchored'])} record(s) {verb} a corrected source_ref.")
        for r in result["reanchored"]:
            typer.echo(f"  {r['id']} -> {r['source_ref']} ({r['reason'][:80]})")
    groups = result["structural_dedup"]["groups"]
    if groups:
        typer.echo(f"Structural dedup after re-anchor: {len(groups)} group(s), "
                    f"{result['structural_dedup']['total_purged']} record(s) purged.")
        for entry in groups:
            if entry.get("error"):
                typer.echo(f"  [red]{entry['winner']}: {entry['error']}[/red]")
            else:
                typer.echo(f"  [cyan]{entry['winner']}[/cyan] keeps; purges: {', '.join(entry['purged'])}")
    if result["llm_duplicates"]:
        typer.echo(f"LLM-judged duplicates (no structural evidence): {len(result['llm_duplicates'])}")
        for entry in result["llm_duplicates"]:
            if entry.get("error"):
                typer.echo(f"  [red]{entry['winner']}: {entry['error']}[/red]")
            else:
                typer.echo(f"  [yellow]{entry['winner']}[/yellow] keeps; purges: "
                            f"{', '.join(entry['purged'])} — {entry['reason'][:100]}")
    total = (result["structural_dedup"]["total_purged"]
             + sum(len(e["purged"]) for e in result["llm_duplicates"] if not e.get("error")))
    if not result["reanchored"] and not total:
        typer.echo("[dim]Nothing to reconcile.[/dim]")
        return
    typer.echo(f"\n{len(result['reanchored'])} re-anchor(s), {total} record(s) purged.")


@impact_app.command("remeasure")
def impact_remeasure(
    dry_run: bool = typer.Option(False, "--dry-run", help="Compute and report only (still calls the LLM); nothing is written"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Run without a confirmation prompt"),
    limit: int = typer.Option(0, "--limit", help="Cap how many unmeasured records to re-check (0 = all)"),
) -> None:
    """Give unmeasured records a second LLM measurements pass, verified the same way a fresh draft is (makes real LLM calls)."""
    _bootstrap()
    if not dry_run and not yes:
        typer.confirm("This calls the LLM to re-check unmeasured records. Continue?", abort=True)
    result = _utility("impact_remeasure", {"dry_run": dry_run, "limit": limit or None})
    for err in result["errors"]:
        typer.echo(f"[red]{err}[/red]")
    if not result["updated"] and not result["discarded"]:
        typer.echo(f"[dim]Checked {result['checked']} unmeasured record(s); no new verifiable measurements.[/dim]")
        return
    for rid in result["updated"]:
        typer.echo(f"[green]{rid}[/green]: gained a verified measurement")
    if result["discarded"]:
        typer.echo(f"[dim]{len(result['discarded'])} candidate(s) failed verification, discarded.[/dim]")
    verb = "would gain" if dry_run else "gained"
    typer.echo(f"\n{len(result['updated'])} record(s) {verb} a measurement "
               f"(checked {result['checked']}).")


@impact_app.command("recover")
def impact_recover() -> None:
    """Re-probe the ledger; exit read-only state when a valid ledger exists."""
    status = default_repository().recover()
    typer.echo(f"State: {status.state} ({status.records} records)")
    if status.readonly_reason:
        typer.echo(f"[red]Still read-only: {status.readonly_reason}[/red]")


@impact_app.command("reset")
def impact_reset(yes: bool = typer.Option(False, "--yes", "-y")) -> None:
    """Recreate an empty ledger (corrupt files preserved as .corrupt-<ts>)."""
    repo = default_repository()
    if repo.status().records and not yes:
        typer.confirm(f"Ledger holds {repo.status().records} records. Drop them all?", abort=True)
    repo.reset()
    typer.echo("Ledger reset to empty.")


def _print_draft_result(result: dict, context: str) -> None:
    """Render an ``impact add`` auto-draft result, sharing the draft command's format."""
    if result["errors"]:
        typer.echo("[red]Draft failed:[/red]")
        for err in result["errors"]:
            typer.echo(f"  - {err}")
        raise typer.Exit(code=1)
    if not result["authored"] and not result["rejected"]:
        typer.echo("[yellow]Nothing extractable from the available data.[/yellow]")
        return
    for record in result["authored"]:
        typer.echo(f"[green]auto_approved[/green] {record.id}: {record.problem[:70]}")
    for record in result["rejected"]:
        reasons = "; ".join(record.context.get("draft_errors", []))
        typer.echo(f"[yellow]drafted[/yellow]  {record.id}: {record.problem[:60]} — {reasons[:100]}")
    updated = result.get("updated", [])
    skipped = result.get("skipped", [])
    if updated:
        typer.echo(f"[cyan]{len(updated)} duplicate(s) updated in place (same fingerprint).[/cyan]")
    if skipped:
        typer.echo(f"[dim]{len(skipped)} duplicate(s) already approved — skipped.[/dim]")
    persist_errors = result.get("persist_errors", [])
    if persist_errors:
        # Recoverable per-record failures, NOT fatal: report and finish normally.
        typer.echo("[yellow]Skipped invalid record(s) (not persisted):[/yellow]")
        for err in persist_errors:
            typer.echo(f"  {err}")
    typer.echo(f"\nSource: {context}. {len(result['authored'])} auto-approved, {len(result['rejected'])} flagged for review.")


def _interactive_id(repo, record: ImpactRecord) -> str:
    tag = {"employment": "EMP", "project": "PROJ", "education": "EDU",
           "volunteer": "VOL", "other": "OTH"}.get(record.scope.type, "OTH")
    existing = {r.id for r in repo.list_records()}
    prefix = f"IMP-{tag}-"
    nums = []
    for rid in existing:
        if rid.startswith(prefix):
            try:
                nums.append(int(rid[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{max(nums, default=0) + 1:03d}"

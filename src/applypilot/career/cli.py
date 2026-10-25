"""Owner-operated myCareer commands. No model calls or external submissions."""
from __future__ import annotations
import json
from pathlib import Path
import typer

app = typer.Typer(help="Recognize, review and use myCareer evidence in the canonical impact ledger.", no_args_is_help=True)


def _repository():
    from applypilot.config import load_env
    load_env()
    from applypilot.impact.ledger import default_repository
    return default_repository()


def _print(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str))


@app.command("validate")
def validate(path: Path):
    """Validate a user corpus with the original schema and myCareer semantic checks."""
    from .corpus import load_corpus, validate_corpus
    corpus, warnings = validate_corpus(load_corpus(path))
    _print({"valid": True, "claims": len(corpus["claims"]), "warnings": warnings})


@app.command("import")
def import_command(path: Path, dry_run: bool = typer.Option(False, "--dry-run"),
                   approve_confirmed: bool = typer.Option(False, "--approve-confirmed",
                       help="Explicit owner approval of already-confirmed source claims; disclosure restrictions still apply.")):
    """Import YOUR corpus, never the upstream example. Existing conflicting evidence is not overwritten."""
    from .corpus import load_corpus, import_corpus
    _print(import_corpus(load_corpus(path), _repository(), approve_confirmed=approve_confirmed, dry_run=dry_run))


@app.command("interview")
def interview(record_id: str):
    """Display a private recognition/decision/tradeoff card and unresolved source questions."""
    from .interview import interview_card
    repo = _repository()
    record = repo.get(record_id)
    if record is None:
        raise typer.BadParameter("Unknown record ID")
    _print(interview_card(record, context=repo.career_context()))


@app.command("confirm")
def confirm(record_id: str, response: Path, revision: str = typer.Option(..., "--revision")):
    """Apply an owner-written recognition response; use the revision from the interview card."""
    from .interview import confirm_record
    from .corpus import load_corpus
    from applypilot.impact.models import record_revision
    record = confirm_record(_repository(), record_id, load_corpus(response), expected_revision=revision)
    _print({"id": record.id, "revision": record_revision(record), "status": record.review_status})


@app.command("reject")
def reject(record_id: str, reason: str = typer.Option(..., "--reason"),
           revision: str = typer.Option(..., "--revision")):
    """Persist a permanent do-not-claim entry and revoke outbound use."""
    from .interview import reject_record
    record = reject_record(_repository(), record_id, reason=reason, expected_revision=revision)
    _print({"id": record.id, "status": record.review_status})


@app.command("public-view")
def public_view(target: str | None = typer.Option(None, "--target")):
    """Emit only the allowlisted recruiter projection, not the private interview corpus."""
    from .projection import recruiter_view
    repo = _repository()
    _print(recruiter_view(repo.list_records(), target=target, context=repo.career_context()))


@app.command("match")
def match(requirements: Path):
    """Read a JSON list of requirements and emit direct/adjacent/evidenced/missing links (no score)."""
    from .matching import requirement_links
    from .projection import recruiter_view
    from .corpus import load_corpus
    value = load_corpus(requirements)
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise typer.BadParameter("Requirements must be a JSON array of strings")
    repo = _repository()
    _print(requirement_links(value, recruiter_view(repo.list_records()), context=repo.career_context()))


@app.command("recognize")
def recognize(system_id: str):
    """Retrieve unconfirmed regime questions from a source system; never infer a credential."""
    from .matching import recognition_candidates
    context = _repository().career_context()
    system = next((s for s in context.get("systems", []) if s["id"] == system_id), None)
    if system is None:
        raise typer.BadParameter("Unknown source system ID")
    held = [r["id"] for r in context.get("regimes", []) if r.get("status") == "confirmed"]
    _print({"audience": "owner_only", "system_id": system_id, "candidates": recognition_candidates(
        data_classes=system.get("data_classes", []), buyer_types=system.get("buyer_types", []), held=held)})

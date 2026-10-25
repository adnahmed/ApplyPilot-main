"""CLI command for managing curated GitHub evidence (public and sanctioned private)."""

from __future__ import annotations

import json
import logging
import sys

import typer

from applypilot.config import PROFILE_PATH, load_profile

log = logging.getLogger(__name__)


evidence_app = typer.Typer(
    name="evidence",
    help="Manage curated public evidence for resume/cover letter tailoring.",
    no_args_is_help=True,
)


@evidence_app.command("discover")
def evidence_discover(
    provider: str = typer.Option("github", "--provider", "-p", help="Evidence provider (github)"),
    user: str | None = typer.Option(None, "--user", "-u", help="GitHub username"),
    repo: str | None = typer.Option(None, "--repo", "-r", help="Specific repo (owner/name)"),
    limit: int | None = typer.Option(None, "--limit", "-l", help="Max repositories (default: unlimited)"),
    workers: int = typer.Option(2, "--workers", min=1, help="Repositories to pack and extract concurrently (default: 2)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Compatibility option; supported claims publish automatically"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Read-only metadata preview; no packing, model calls or file writes"),
    refresh: bool = typer.Option(False, "--refresh", help="Extract claims again from cached revision-pinned source"),
    include_private: bool = typer.Option(True, "--include-private/--no-include-private", help="Include authorized private GitHub repositories"),
    share_private_with_model: bool = typer.Option(True, "--share-private-with-model/--no-share-private-with-model", help="Allow private source packs to the configured extraction model"),
):
    """Pack GitHub repositories and publish grounded, disclosure-safe claims."""
    from applypilot import config
    from applypilot.orchestration.utility_client import run_utility
    config.load_env()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if provider != "github":
        raise typer.BadParameter("Supported evidence provider: github")
    if not config.PROFILE_PATH.is_file():
        raise typer.BadParameter("Profile not found. Run `applypilot init` first.")
    source = repo or user
    if not source:
        profile = load_profile()
        source = profile.get("github_username") or profile.get("personal", {}).get("github_url")
    if not source:
        raise typer.BadParameter("Use --user, --repo, or configure your GitHub profile URL.")
    if limit is not None and limit < 1:
        raise typer.BadParameter("limit must be at least 1.")
    typer.echo(f"Discovering repository evidence for {source}...")
    try:
        result = run_utility("evidence_collect", {"identity": source, "limit": limit, "workers": workers,
            "include_private": include_private, "share_private_with_model": share_private_with_model,
            "use_ai": True, "share_with_model": True, "refresh": refresh, "dry_run": dry_run})
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get("repositories_incomplete") or result.get("extractions_failed"):
        raise typer.Exit(1)


# ── Prune ─────────────────────────────────────────────────────────────────

@evidence_app.command("prune")
def evidence_prune(
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be removed; no writes"),
    ref_filter: str | None = typer.Option(None, "--ref", help="Only consider entries whose ref contains this substring"),
):
    """Remove low-signal entries from public_evidence via one batched LLM audit."""
    from applypilot.cli import _bootstrap
    _bootstrap()

    profile_path = PROFILE_PATH
    if not profile_path.exists():
        raise typer.Exit("Profile not found. Run `applypilot init` first.")
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    all_entries = list(profile.get("public_evidence", []))
    if not all_entries:
        typer.echo("public_evidence is empty; nothing to prune.")
        return

    eligible = all_entries
    if ref_filter:
        eligible = [e for e in all_entries if ref_filter.lower() in (e.get("ref") or "").lower()]
        if not eligible:
            typer.echo(f"No entries match --ref {ref_filter!r}.")
            return

    typer.echo(f"Asking LLM to audit {len(eligible)} entr(ies)...")
    from applypilot.orchestration.utility_client import run_utility
    judged = run_utility("evidence_prune", {"entries": eligible})["judged"]

    eligible_ids = {e["id"] for e in eligible}
    drops = {d["id"]: d["reason"] for d in judged if d["id"] in eligible_ids}

    typer.echo(f"\n{'[dry-run] ' if dry_run else ''}Prune: {len(drops)} of {len(all_entries)} entr(ies) would be removed")
    for e in all_entries:
        if e["id"] in drops:
            first_claim = (e.get("allowed_claims") or [""])[0][:90]
            typer.echo(f"  ✗ {e['id']} {e['ref']}: {drops[e['id']]}\n      {first_claim}")

    if dry_run:
        typer.echo("[dry-run] No files modified.")
        return
    if not drops:
        typer.echo("Nothing to remove.")
        return

    from applypilot.onboarding.storage import setup_lock, atomic
    originals = {entry["id"]: entry for entry in all_entries}
    with setup_lock(profile_path.parent):
        current_bytes = profile_path.read_bytes()
        profile = json.loads(current_bytes)
        current = profile.get("public_evidence", [])
        drops = {entry["id"]: drops[entry["id"]] for entry in current
                 if entry["id"] in drops and originals.get(entry["id"]) == entry}
        kept = [entry for entry in current if entry["id"] not in drops]
        backup_path = profile_path.with_suffix(".bak")
        atomic(backup_path, current_bytes)
        profile["public_evidence"] = kept
        atomic(profile_path, json.dumps(profile, indent=2).encode("utf-8"))
    typer.echo(f"\n✓ Pruned {len(drops)}. {len(kept)} entr(ies) remain in {profile_path}")
    typer.echo(f"  Backup: {backup_path}")

# myCareer uses the existing impact ledger, not a second career datastore.
from applypilot.career.cli import app as career_app
evidence_app.add_typer(career_app, name="career")

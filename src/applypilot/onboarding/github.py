"""Revision-pinned collection with standing approval of grounded project claims."""
from __future__ import annotations

from copy import deepcopy
import re
import threading
from urllib.parse import urlsplit

from .storage import atomic, digest, now, read_json, revision, setup_lock, write_json


class _CollectionStop(threading.Event):
    """Local cancellation observes owners without stopping their shared runtime."""
    def __init__(self, *parents):
        super().__init__()
        self.parents = [event for event in parents if event is not None]

    def is_set(self):
        return super().is_set() or any(event.is_set() for event in self.parents)

    def wait(self, timeout=None):
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                break
            super().wait(.2 if remaining is None else min(.2, remaining))
        return self.is_set()


def identity(value):
    value = str(value or "").strip()
    if value.startswith("https://"):
        parsed = urlsplit(value)
        if parsed.hostname not in {"github.com", "www.github.com"} or parsed.query or parsed.fragment:
            raise ValueError("Use a GitHub profile or repository URL")
        value = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9_.-]{1,100})?", value):
        raise ValueError("Use a GitHub username or owner/repository")
    return value


def _private(entry):
    return entry.get("private") is True or entry.get("visibility") in {"private", "internal"}


def _revoked(row):
    if row.get("review_status") in {"revoked", "rejected", "declined"} or row.get("revoked_at") or row.get("rejected_at"):
        return True
    if row.get("resume_eligible") is not False:
        return False
    return bool(row.get("approved_at") or row.get("attested_at") or row.get("review_status") in {
        "user_approved", "auto_approved", "legacy_user_curated"} or any(
            isinstance(c, dict) and c.get("resume_eligible") is True for c in row.get("claims") or []))


def _grounded(claim):
    return (re.fullmatch(r"[0-9a-fA-F]{40,64}", str(claim.get("source_revision") or "")) is not None
            and bool(str(claim.get("excerpt") or "").strip())
            and claim.get("source_kind") in {"implementation", "configuration", "documentation", "test_code",
                "candidate_contribution", "successful_run", "deployment_record"}
            and (bool(claim.get("path")) or claim.get("source_kind") in {
                "candidate_contribution", "successful_run", "deployment_record"})
            and 10 <= len(str(claim.get("allowed_resume_claim") or "").strip()) <= 1500)


def _claim_key(claim):
    return " ".join(str(claim.get("allowed_resume_claim") or "").split()).casefold()


def _claim_keys(claim):
    return {value for value in (_claim_key(claim),
            ' '.join(str(claim.get('original_allowed_resume_claim') or '').split()).casefold()) if value}


def _merge_claims(prior, incoming):
    result = deepcopy([c for c in prior or [] if isinstance(c, dict)])
    for claim in incoming or []:
        if not isinstance(claim, dict):
            continue
        match = next((old for old in result if (claim.get("id") and old.get("id") == claim["id"])
                      or _claim_keys(old) & _claim_keys(claim)), None)
        if match is None:
            copied = deepcopy(claim)
            if _revoked(copied):
                copied["resume_eligible"] = False
            result.append(copied)
        elif _revoked(claim):
            match.update(deepcopy(claim))
            match["resume_eligible"] = False
        elif not _revoked(match):
            approved = {key: match[key] for key in ("approved_at", "attested_at", "resume_eligible", "review_status", "evidence_class")
                        if key in match and match.get("resume_eligible") is True}
            owner_edit = {key: deepcopy(match[key]) for key in
                          ('owner_edited_at', 'original_allowed_resume_claim', 'allowed_resume_claim')
                          if key in match} if match.get('owner_edited_at') else {}
            match.update(deepcopy(claim))
            match.update(approved)
            match.update(owner_edit)
    return result


def _source_ref(source):
    return "private-" + digest(source["ref"])[:24] if _private(source) else source["ref"]


def _safe_private_text(source, claim):
    text = " ".join(str(claim.get("allowed_resume_claim") or "").split())
    # Names, paths, and links are never required to express a technical capability.
    for token in sorted({str(source.get("ref") or ""), str(source.get("title") or ""),
                         str(source.get("url") or ""), str(claim.get("path") or "")}, key=len, reverse=True):
        if token:
            text = re.sub(re.escape(token), "a private project", text, flags=re.I)
    if re.search(r"https?://|www\.|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|(?:[\w-]+\.)+(?:com|io|net|org|dev|app)\b", text, re.I):
        return ""
    return text if 10 <= len(text) <= 1500 else ""


def _publish_locked(root, sources, selected=None, disclosure_texts=None):
    profile_path = root / "profile.json"
    profile = read_json(profile_path, None, limit=8_000_000)
    if not isinstance(profile, dict):
        raise ValueError("Profile not found or invalid. Initialize the profile before collecting evidence.")
    before = revision(root)
    draft_path = root / "setup" / "draft.json"
    draft = read_json(draft_path, None, limit=8_000_000)
    evidence = deepcopy(profile.get("public_evidence") or [])
    draft_evidence = ((draft or {}).get("profile") or {}).get("public_evidence") or []
    if not isinstance(evidence, list) or any(not isinstance(e, dict) for e in evidence):
        raise ValueError("Existing public_evidence must be a list of entries")
    if not isinstance(draft_evidence, list) or any(not isinstance(e, dict) for e in draft_evidence):
        raise ValueError("Draft public_evidence must be a list of entries")
    published = 0
    for source in sources:
        if not isinstance(source, dict) or not source.get("ref"):
            continue
        ref = _source_ref(source)
        matches = [e for e in evidence if e.get("provider") == "github" and e.get("ref") in {ref, source["ref"]}]
        entry = matches[0] if matches else None
        decisions = [e for e in draft_evidence if isinstance(e, dict) and e.get("provider") == "github"
                     and e.get("ref") in {ref, source["ref"]}]
        if _revoked(source) or any(_revoked(e) for e in [*matches, *decisions]):
            continue
        rejected = [c for e in [source, *matches, *decisions] for c in e.get("claims") or []
                    if isinstance(c, dict) and _revoked(c)]
        accepted = []
        for raw in source.get("claims") or []:
            if not isinstance(raw, dict) or (selected is not None and raw.get("id") not in selected):
                continue
            if not _grounded(raw) or _revoked(raw) or any(
                _claim_keys(c) & _claim_keys(raw) or (raw.get("id") and c.get("id") == raw["id"]) for c in rejected):
                continue
            claim = deepcopy(raw)
            if _private(source):
                replacement = (disclosure_texts or {}).get(claim.get("id"))
                if isinstance(replacement, str):
                    claim["allowed_resume_claim"] = replacement
                text = _safe_private_text(source, claim)
                if not text:
                    continue
                claim = {"id": claim.get("id"), "allowed_resume_claim": text,
                    "capability": "Private project contribution", "source_revision": claim["source_revision"],
                    "source_kind": "private_user_attestation", "source_url": "", "path": "", "excerpt": ""}
            claim.update(resume_eligible=True, review_status="user_approved", evidence_class="USER_ATTESTED",
                         approved_at=raw.get("approved_at") or now(), attested_at=raw.get("attested_at") or now())
            accepted.append(claim)
        if not accepted:
            if _private(source) and entry and entry.get("resume_eligible") is True:
                # A visibility change also sanitizes previously approved legacy
                # claims; this preserves approval without inventing provenance.
                accepted = [deepcopy(c) for c in entry.get("claims", []) if c.get("resume_eligible") is True]
                if "claims" not in entry:
                    accepted = [{"allowed_resume_claim": text, "resume_eligible": True,
                                 "review_status": "legacy_user_curated"} for text in entry.get("allowed_claims", [])]
            if not accepted:
                continue
        if entry is None:
            entry = {"id": "github-" + digest(source["ref"])[:16], "provider": "github", "ref": ref,
                     "scope": "curated_project", "claims": []}
            evidence.append(entry)
        for duplicate in matches[1:]:
            entry["claims"] = _merge_claims(entry.get("claims"), duplicate.get("claims"))
            evidence.remove(duplicate)
        if "claims" not in entry:
            entry["claims"] = [{"allowed_resume_claim": text, "resume_eligible": True,
                "review_status": "legacy_user_curated"} for text in entry.get("allowed_claims") or []]
        existing = {_claim_key(c) for c in entry["claims"] if c.get("resume_eligible") is True}
        published += sum(_claim_key(c) not in existing for c in accepted)
        entry["claims"] = _merge_claims(entry["claims"], accepted)
        entry.update(title="Private project" if _private(source) else entry.get('owner_title') or source.get("title", ref),
                     url="" if _private(source) else source.get("url", ""), ref=ref,
                     version=source.get("version", ""), fetched_at=source.get("fetched_at"),
                     private=_private(source), visibility="private" if _private(source) else "public",
                     resume_eligible=True)
        if _private(source):
            for key in list(entry):
                if key not in {"id", "provider", "ref", "title", "url", "scope", "claims", "allowed_claims",
                               "version", "fetched_at", "private", "visibility", "resume_eligible",
                               "disclosure_approved", "disclosure_approved_at"}:
                    entry.pop(key)
            projected = []
            for claim in entry["claims"]:
                text = _safe_private_text(source, claim)
                if not text:
                    continue
                safe = {key: deepcopy(value) for key, value in claim.items() if key in {
                    "id", "source_revision", "resume_eligible", "review_status", "evidence_class",
                    "approved_at", "attested_at", "revoked_at", "rejected_at", "owner_edited_at"}}
                if claim.get('original_allowed_resume_claim'):
                    original = _safe_private_text(source, {'allowed_resume_claim': claim['original_allowed_resume_claim']})
                    if original:
                        safe['original_allowed_resume_claim'] = original
                safe.update(allowed_resume_claim=text, capability="Private project contribution",
                            source_kind="private_user_attestation", source_url="", path="", excerpt="")
                projected.append(safe)
            entry["claims"] = projected
            entry.update(disclosure_approved=True, disclosure_approved_at=entry.get("disclosure_approved_at") or now())
        entry["allowed_claims"] = [c["allowed_resume_claim"] for c in entry["claims"] if c.get("resume_eligible") is True]
    changed = evidence != profile.get("public_evidence", [])
    if changed:
        atomic(profile_path.with_suffix(".bak"), profile_path.read_bytes())
        profile["public_evidence"] = evidence
        write_json(profile_path, profile)
    if isinstance(draft, dict):
        # Only sync evidence. An unrelated stale draft keeps its conflict marker.
        combined = deepcopy(draft_evidence)
        for entry in evidence:
            match = next((e for e in combined if (entry.get("id") and e.get("id") == entry.get("id")) or
                          (e.get("provider"), e.get("ref")) == (entry.get("provider"), entry.get("ref"))), None)
            if match is None:
                combined.append(deepcopy(entry))
            elif not _revoked(match):
                claims = _merge_claims(match.get("claims"), entry.get("claims"))
                match.update(deepcopy(entry))
                match["claims"] = claims
                match["allowed_claims"] = [c["allowed_resume_claim"] for c in claims if c.get("resume_eligible") is True]
        draft.setdefault("profile", {})["public_evidence"] = combined
        if draft.get("base_revision") == before:
            draft["base_revision"] = revision(root)
        draft.pop("revision", None)
        write_json(draft_path, draft)
    return {"claims_published": published, "live_profile_changed": changed}


def publish_grounded(root):
    """Promote already collected grounded drafts without fetching or inventing provenance."""
    with setup_lock(root):
        sources = read_json(root / "setup" / "github.json", [], limit=32_000_000)
        legacy = [e for e in sources if not e.get("pack_status")]
        for entry in legacy:
            entry.update(pack_status="incomplete", pack_error="Legacy intake requires a revision-pinned Repomix pack; run discovery.",
                         extraction_status="deferred")
        if legacy:
            write_json(root / "setup" / "github.json", sources)
        profile = read_json(root / "profile.json", {}, limit=8_000_000)
        draft = read_json(root / "setup" / "draft.json", {}, limit=8_000_000)
        by_ref = {entry["ref"]: entry for entry in sources if isinstance(entry, dict) and entry.get("ref")}
        historical = [*((draft.get("profile") or {}).get("public_evidence") or []),
                      *(profile.get("public_evidence") or [])]
        for entry in historical:
            if not isinstance(entry, dict) or entry.get("provider") != "github" or _private(entry):
                continue
            if entry.get("ref") not in by_ref:
                copied = deepcopy(entry)
                sources.append(copied)
                by_ref[copied["ref"]] = copied
            else:
                current = by_ref[entry["ref"]]
                revoked = _revoked(current)
                current["claims"] = _merge_claims(entry.get("claims"), current.get("claims"))
                # Intake's default ineligible flag is staging, not revocation.
                # Keep the newest repository visibility, including public->private.
                if not revoked and any(c.get("resume_eligible") is True for c in current["claims"]):
                    current["resume_eligible"] = True
        return _publish_locked(root, sources)


def recover_recorded_claims(root, ref, example_id):
    """Revalidate a captured model result against its exact cached repository revision."""
    import gzip
    import json
    from applypilot.evidence.base import RawSignals
    from applypilot.evidence.draft import validate_capability_claims
    from applypilot.postgres_read import read_snapshot
    from applypilot.reasoning.learning_store import root as learning_root

    if not re.fullmatch(r"[0-9a-f]{64}", str(example_id)):
        raise ValueError("Supply the recorded learning example ID")
    sources = read_json(root / "setup" / "github.json", [], limit=32_000_000)
    source = next((deepcopy(e) for e in sources if e.get("ref") == ref), None)
    if not source or source.get("pack_status") != "packed" or not re.fullmatch(r"[0-9a-f]{64}", source.get("pack_key", "")):
        raise ValueError("Recovery requires a successfully cached source pack")
    with read_snapshot(learning_root() / "learning.pgstore", timeout=30) as conn:
        example = conn.execute("SELECT task,messages,output FROM examples WHERE id=?", (example_id,)).fetchone()
    if not example or example["task"] != "evidence_draft_draft_capability_claims":
        raise ValueError("Recorded example does not belong to repository claim extraction")
    messages = json.loads(example["messages"])
    payload = json.loads(next(m["content"] for m in messages if m.get("role") == "user"))
    recorded = payload.get("observations") or []
    if (payload.get("project", {}).get("url") != source.get("url") or not recorded
            or any(row.get("source_revision") != source.get("version") for row in recorded)):
        raise ValueError("Recorded example does not match this repository revision")
    cache = root / "evidence" / "repomix" / (source["pack_key"] + ".json.gz")
    pack = json.loads(gzip.decompress(cache.read_bytes()))
    if pack.get("key") != source["pack_key"]:
        raise ValueError("Cached pack identity changed")
    signals = RawSignals(source["title"], "", source.get("stack", []), [], source["url"], "",
        {"private": _private(source), "fork": source.get("fork") is True,
         "source_revision": source["version"], "pack_key": source["pack_key"], "observations": pack["observations"]})
    output = json.loads(example["output"])
    claims, error = validate_capability_claims(signals, output["claims"])
    if error:
        return {"status": "incomplete", "error": error, "claims_published": 0}
    for claim in claims:
        claim["id"] = digest({"ref": ref, "revision": claim["source_revision"],
                              "claim": claim["allowed_resume_claim"]})[:24]
    source["claims"] = _merge_claims(source.get("claims"), claims)
    source.update(extraction_status="complete", extraction_error="", extraction_recovered_from=example_id,
                  extraction_recovery_reason="Revalidated recorded source citations with 200-line/16KB span bounds")
    source["observations"] = [{k: c.get(k, "") for k in ("source_revision", "path", "excerpt", "source_kind")}
                              for c in source["claims"] if _grounded(c)]
    _save_collected(root, [source])
    return {"status": "recovered", "claims_extracted": len(claims), "source_revision": source["version"],
            "recorded_example_id": example_id, **publish_grounded(root)}


def _save_collected(root, entries):
    """Checkpoint each finished repository so interrupted discovery can resume."""
    with setup_lock(root):
        path = root / "setup" / "github.json"
        current = read_json(path, [], limit=32_000_000)
        refs = {e["ref"] for e in entries}
        for entry in entries:
            latest = next((e for e in current if e.get("ref") == entry["ref"]), {})
            entry["claims"] = _merge_claims(latest.get("claims"), entry["claims"])
            if _revoked(latest):
                entry.update({k: latest[k] for k in ("resume_eligible", "review_status", "revoked_at", "rejected_at") if k in latest})
        write_json(path, [e for e in current if e.get("ref") not in refs] + entries)


def _collect_repository(root, candidate, previous, data, budget, stop, events, *, propagate_errors=False):
    """One finite task owns its source pack and inference context; never writes profiles."""
    from applypilot.evidence.github import GitHubProvider
    from applypilot import operations as ops

    entry = deepcopy(previous)
    entry.update(provider="github", ref=candidate.ref, title=candidate.title,
                 url=candidate.url, fetched_at=now())
    entry.setdefault("claims", [])
    entry.setdefault("observations", [])
    result = {"entry": entry, "claims_extracted": 0, "warnings": [], "reused": False}
    provider = GitHubProvider(cache_root=root, cancel_event=stop, budget=budget)
    with ops.scope(task_id="evidence:" + candidate.ref):
        events.put({"ref": candidate.ref, "phase": "packing"})
        try:
            signals = provider.fetch(candidate)
        except Exception as exc:
            if propagate_errors:
                raise
            entry.update(pack_status="incomplete", pack_error=(
                "Collection interrupted; retry discovery." if stop.is_set() else
                str(exc) if isinstance(exc, ValueError) else type(exc).__name__),
                extraction_status="deferred")
            result["warnings"].append(candidate.ref + ": " + entry["pack_error"])
            return result
        extra = signals.extra or {}
        entry.update({k: deepcopy(extra[k]) for k in ("private", "visibility", "fork", "pack_status", "pack_error",
            "pack_attempts", "pack_key", "pack_cache_hit", "packed_files", "packing_config") if k in extra})
        entry.update(stack=signals.languages, version=extra.get("source_revision", ""), scope="curated_project")
        if stop.is_set():
            entry.update(extraction_status="deferred", extraction_error="Collection interrupted; retry discovery.")
        elif extra.get("pack_status") != "packed":
            entry["extraction_status"] = "empty" if extra.get("pack_status") == "empty" else "deferred"
            if extra.get("pack_error"):
                result["warnings"].append(candidate.ref + ": " + extra["pack_error"])
        elif not signals.observations:
            entry.update(extraction_status="complete", extraction_error="No usable text in the source pack.")
        elif (not data.get("refresh") and previous.get("pack_key") == extra.get("pack_key")
              and previous.get("extraction_status") == "complete"
              and all(_grounded(c) or _revoked(c) for c in previous.get("claims") or [])):
            entry["extraction_status"] = "complete"
            result["reused"] = True
        elif data.get("use_ai") is True and (not _private(entry) or data.get("share_private_with_model", True) is True):
            from applypilot.evidence.draft import draft_capability_claims
            events.put({"ref": candidate.ref, "phase": "extracting"})
            try:
                claims, error = draft_capability_claims(signals)
            except Exception as exc:
                if propagate_errors:
                    raise
                claims, error = [], f"Claim extraction failed ({type(exc).__name__}); retry discovery."
            if stop.is_set() and not claims:
                entry.update(extraction_status="deferred", extraction_error="Collection interrupted; retry discovery.")
            else:
                for claim in claims:
                    claim["id"] = digest({"ref": candidate.ref, "revision": claim.get("source_revision"),
                                          "claim": claim.get("allowed_resume_claim")})[:24]
                entry["claims"] = _merge_claims(previous.get("claims"), claims)
                entry.update(extraction_status="incomplete" if error else "complete", extraction_error=error)
                result["claims_extracted"] = len(claims)
                if error:
                    result["warnings"].append(candidate.ref + ": " + error)
        else:
            entry.update(extraction_status="deferred", extraction_error="Model sharing disabled; source pack retained.")
        entry["observations"] = [{k: c.get(k, "") for k in ("source_revision", "path", "excerpt", "source_kind")}
                                 for c in entry["claims"] if _grounded(c)]
        entry.setdefault("resume_eligible", False)
        entry.setdefault("allowed_claims", [])
        return result


def _collect_candidates(root, data, provider, candidates, stop, progress, state, save_state):
    """Finite setup import. No executor or owned worker lifetime is created."""
    from queue import SimpleQueue
    from applypilot.execution import ExecutionStopped

    prior = read_json(root / "setup" / "github.json", [], limit=32_000_000)
    prior_by_ref = {entry.get("ref"): entry for entry in prior if isinstance(entry, dict)}
    entries, warnings = [], []
    extracted = published = reused = 0
    changed = False
    for candidate in candidates:
        if stop.is_set():
            raise ExecutionStopped("Evidence collection cancelled")
        state.update(completed=len(entries), total=len(candidates),
                     active_workers=1, active_repositories={candidate.ref: "collecting"})
        save_state()
        progress(message="Collecting: " + candidate.ref, completed=len(entries),
                 total=len(candidates), active_workers=1, workers=1)
        try:
            result = _collect_repository(root, candidate, prior_by_ref.get(candidate.ref, {}),
                                         data, provider.budget, stop, SimpleQueue())
        except Exception as exc:
            if stop.is_set():
                raise ExecutionStopped("Evidence collection cancelled") from exc
            entry = deepcopy(prior_by_ref.get(candidate.ref, {}))
            entry.update(provider="github", ref=candidate.ref, title=candidate.title,
                         url=candidate.url, fetched_at=now(), extraction_status="incomplete",
                         extraction_error=f"Repository import did not finish ({type(exc).__name__}); retry discovery.")
            entry.setdefault("claims", [])
            entry.setdefault("pack_status", "incomplete")
            result = {"entry": entry, "claims_extracted": 0, "reused": False,
                      "warnings": [candidate.ref + ": " + entry["extraction_error"]]}
        entry = result["entry"]
        _save_collected(root, [entry])
        outcome = publish_grounded(root)
        entries.append(entry)
        extracted += result["claims_extracted"]
        published += outcome["claims_published"]
        changed = changed or outcome["live_profile_changed"]
        reused += int(result["reused"])
        warnings.extend(result["warnings"])
        state.update(completed=len(entries), active_workers=0, active_repositories={},
                     claims_published=published)
        save_state()
        progress(message=f"Completed {len(entries)} of {len(candidates)}: {candidate.ref}",
                 completed=len(entries), total=len(candidates), workers=1, active_workers=0)
    outcome = publish_grounded(root)
    published += outcome["claims_published"]
    changed = changed or outcome["live_profile_changed"]
    return {"repositories": len(entries), "workers": 1, "repositories_reused": reused,
        "repositories_packed": sum(e.get("pack_status") == "packed" for e in entries),
        "repositories_incomplete": sum(e.get("pack_status") == "incomplete" for e in entries),
        "repositories_empty": sum(e.get("pack_status") == "empty" for e in entries),
        "extractions_incomplete": sum(e.get("extraction_status") in {"incomplete", "deferred"} for e in entries),
        "extractions_failed": sum(e.get("extraction_status") == "incomplete" for e in entries),
        "claims_extracted": extracted, "claims_drafted": extracted, "claims_published": published,
        "live_profile_changed": changed, "warnings": warnings,
        "note": "Each completed repository is saved and grounded claims published. Incomplete imports remain available for an explicit retry."}


def collect(root, data, progress=lambda **kw: None, *, stop_event=None):
    import os
    from contextlib import ExitStack
    from uuid import uuid4

    from applypilot.evidence.github import GitHubProvider
    from applypilot.processes import process_guard
    from applypilot.execution import current_task
    from applypilot.work_context import stage_context

    from .workspace import connected_identity
    who = identity(data.get("identity") or connected_identity(root, 'github'))
    limit = data.get("limit", 3)
    workers = data.get("workers", 2)
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("Collect at least one repository per batch")
    if type(workers) is not int or workers < 1:
        raise ValueError("workers must be a positive integer")
    if data.get("use_ai") is True and data.get("share_with_model") is not True:
        raise ValueError("Authorize sharing repository source with your configured model")
    stop = _CollectionStop(stop_event, current_task().get("stop_event"), stage_context().get("stop_event"))
    provider = GitHubProvider(cache_root=root, cancel_event=stop)
    if data.get("dry_run") is True:
        candidates = provider.discover(who, limit=limit, include_private=data.get("include_private") is True)
        return {"dry_run": True, "workers": workers, "repositories": len(candidates),
                "candidates": [c.ref for c in candidates], "live_profile_changed": False,
                "note": "Metadata preview only; no packing, model calls, or file writes."}
    state_path = root / "evidence" / "discovery-state.json"
    with ExitStack() as stack:
        try:
            stack.enter_context(process_guard(root / "evidence" / "discovery.guard"))
        except RuntimeError as exc:
            if not str(exc).startswith("Another process already owns "):
                raise
            owner = read_json(state_path, {})
            raise ValueError(f"Evidence discovery is already running (PID {owner.get('pid', 'starting')}, "
                             f"identity {owner.get('identity', 'pending')}). See {state_path}") from None
        import psutil
        state = {"run_id": uuid4().hex, "pid": os.getpid(), "process_started": psutil.Process().create_time(),
                 "identity": who, "workers": workers, "state": "discovering", "started_at": now(),
                 "completed": 0, "active_workers": 0, "active_repositories": {}}

        def save_state():
            state["updated_at"] = now()
            write_json(state_path, state)

        save_state()
        try:
            candidates = provider.discover(who, limit=limit, include_private=data.get("include_private") is True)
            if not candidates:
                raise ValueError(provider.last_error or "No eligible repositories found. Check username and repository permissions.")
            state.update(state="running", total=len(candidates))
            save_state()
            result = _collect_candidates(root, data, provider, candidates, stop, progress, state, save_state)
            state.update(state="incomplete" if result["repositories_incomplete"] or result["extractions_failed"] else "complete",
                         result=result, finished_at=now())
            return result
        except BaseException as exc:
            stop.set()
            state.update(state="interrupted" if isinstance(exc, KeyboardInterrupt) or
                         getattr(exc, "error_kind", None) == "cancelled" else "failed",
                         error_type=type(exc).__name__, finished_at=now())
            raise
        finally:
            state.update(active_workers=0, active_repositories={})
            save_state()


def curate(root, data):
    """Retained Setup selection API; standing approval removes repeated attestation."""
    selected = data.get("claim_ids", [])
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) for x in selected):
        raise ValueError("Choose at least one supported claim")
    with setup_lock(root):
        sources = read_json(root / "setup" / "github.json", [], limit=32_000_000)
        ids = {c.get("id") for e in sources for c in e.get("claims") or []}
        if set(selected) - ids:
            raise ValueError("A selected claim was refreshed or removed; reload evidence")
        outcome = _publish_locked(root, sources, set(selected), data.get("disclosure_texts"))
        return {"added_to_draft": outcome["claims_published"], **outcome}

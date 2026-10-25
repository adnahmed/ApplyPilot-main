"""Shared in-process status for the composite autopilot dashboard."""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field


@dataclass(frozen=True)
class SourceSnapshot:
    attempts: int = 0
    ok: int = 0
    failed: int = 0
    jobs: int = 0
    last_error: str = ""


@dataclass(frozen=True)
class AutopilotSnapshot:
    enabled: bool
    cycle: int
    min_score: int
    discovery_done: int
    discovery_total: int
    last_search: str
    new_jobs: int
    duplicates: int
    discovery_errors: int
    scoring_queue: int
    scoring_active: int
    scoring_workers: int
    last_score: int | None
    last_scored_job: str
    pipeline_state: str
    stages: dict[str, int]
    sources: dict[str, SourceSnapshot]
    last_active_stages: dict[str, int] = field(default_factory=dict)
    pipeline_mode: str = "finite"


@dataclass
class _SourceState:
    attempts: int = 0
    ok: int = 0
    failed: int = 0
    jobs: int = 0
    last_error: str = ""


@dataclass
class _AutopilotState:
    enabled: bool = False
    cycle: int = 0
    min_score: int = 8
    discovery_done: int = 0
    discovery_total: int = 0
    last_search: str = ""
    new_jobs: int = 0
    duplicates: int = 0
    discovery_errors: int = 0
    scoring_queue: int = 0
    scoring_active: int = 0
    scoring_workers: int = 0
    last_score: int | None = None
    last_scored_job: str = ""
    pipeline_state: str = "IDLE"
    stages: dict[str, int] = field(default_factory=dict)
    last_active_stages: dict[str, int] = field(default_factory=dict)
    pipeline_mode: str = "finite"
    sources: dict[str, _SourceState] = field(default_factory=dict)


_lock = threading.Lock()
_state = _AutopilotState()
log = logging.getLogger(__name__)


_PROGRESS_RE = re.compile(
    r"Progress:\s*(?P<done>\d+)/(?P<total>\d+)\s+(?:queries done|employers)"
    r"\s*\((?P<new>\d+)\s+new,\s*"
    r"(?P<dupes>\d+)\s+dupes,\s*"
    r"(?P<errors>\d+)\s+errors\)"
)

_RESULT_RE = re.compile(
    r'\["(?P<query>.+?)"\s+in\s+'
    r"(?P<location>.+?)\s+\[tier\s+\d+\]\]\s+"
    r"(?P<results>\d+)\s+results\s+->\s+"
    r"(?P<new>\d+)\s+new,\s+"
    r"(?P<dupes>\d+)\s+dupes"
)

_SCORING_RE = re.compile(
    r"Scoring\s+(?P<count>\d+)\s+jobs?"
    r"(?:\s+\(workers=(?P<workers>\d+)\))?"
)

_SCORE_RE = re.compile(
    r"score=(?P<score>ERR|\d+)\s{2,}(?P<title>.+?)\s*$"
)

_SOURCE_EVENT_RE = re.compile(
    r"ApplyPilot source (?P<kind>attempt|result):\s*(?P<payload>\{.*\})"
)

_FULL_CRAWL_RE = re.compile(
    r"Full crawl:\s*(?P<total>\d+)\s+(?:search combinations|queries\s+x\s+\d+\s+employers)"
)

_SITES_RE = re.compile(
    r"Sites:\s*(?P<sites>[^|]+)"
)

_STAGE_STATUS_RE = re.compile(
    r"Stage status:\s*(?P<stage>discover|enrich|score|tailor|cover|pdf|brief)\s+"
    r"(?P<status>active|idle|error)(?:\s+(?P<count>\d+))?",
    re.IGNORECASE,
)

_PDF_GENERATED_RE = re.compile(
    r"PDF generated via (?:LaTeX|HTML):\s*(?P<path>\S+)\s*\(pages=(?P<pages>\d+)\)"
)

_STAGES = ("discover", "enrich", "score", "tailor", "cover", "pdf", "brief")


def enable(min_score: int = 8, workers: int = 1) -> None:
    """Enable composite autopilot dashboard state."""
    with _lock:
        _state.enabled = True
        _state.min_score = min_score
        _state.scoring_workers = workers


def disable() -> None:
    """Disable autopilot-specific dashboard rendering."""
    with _lock:
        _state.enabled = False


def begin_cycle(cycle: int) -> None:
    """Reset per-discovery-cycle status."""
    with _lock:
        _state.cycle = cycle
        _state.pipeline_mode = "finite"
        _state.discovery_done = 0
        _state.discovery_total = 0
        _state.last_search = ""
        _state.new_jobs = 0
        _state.duplicates = 0
        _state.discovery_errors = 0
        _state.scoring_queue = 0
        _state.scoring_active = 0
        _state.last_score = None
        _state.last_scored_job = ""
        _state.pipeline_state = "RUNNING"
        _state.stages = {stage: 0 for stage in _STAGES}
        _state.last_active_stages = {stage: 0 for stage in _STAGES}
        _state.sources = {}


def set_pipeline_state(status: str) -> None:
    """Set the overall preparation state shown by the dashboard."""
    with _lock:
        _state.pipeline_state = status.upper()


def begin_continuous() -> None:
    """Initialize a runtime once, not after each stage batch."""
    begin_cycle(0)
    with _lock:
        _state.pipeline_mode = "continuous"


def snapshot() -> AutopilotSnapshot:
    """Return a safe immutable dashboard snapshot."""
    with _lock:
        return AutopilotSnapshot(
            enabled=_state.enabled,
            cycle=_state.cycle,
            pipeline_mode=_state.pipeline_mode,
            min_score=_state.min_score,
            discovery_done=_state.discovery_done,
            discovery_total=_state.discovery_total,
            last_search=_state.last_search,
            new_jobs=_state.new_jobs,
            duplicates=_state.duplicates,
            discovery_errors=_state.discovery_errors,
            scoring_queue=_state.scoring_queue,
            scoring_active=_state.scoring_active,
            scoring_workers=_state.scoring_workers,
            last_score=_state.last_score,
            last_scored_job=_state.last_scored_job,
            pipeline_state=_state.pipeline_state,
            stages=dict(_state.stages),
            last_active_stages=dict(_state.last_active_stages),
            sources={
                name: SourceSnapshot(
                    value.attempts,
                    value.ok,
                    value.failed,
                    value.jobs,
                    value.last_error,
                )
                for name, value in _state.sources.items()
            },
        )


def _source_name(raw: str) -> str:
    names = {
        "linkedin": "LinkedIn",
        "indeed": "Indeed",
        "glassdoor": "Glassdoor",
        "ziprecruiter": "ZipRecruiter",
        "zip_recruiter": "ZipRecruiter",
        "google": "Google",
        "freehire": "FreeHire",
    }
    return names.get(raw.lower(), raw)


def _clean_message(line: str) -> str:
    """Remove logging prefix from an interesting pipeline line."""
    return re.sub(
        r"^.*?\b(?:INFO|WARNING|ERROR)\s+-\s+",
        "",
        line.strip(),
        count=1,
    )


def record_source_attempt(source: str, query: str, location: str) -> None:
    """Log one canonical source attempt for the parent dashboard parser."""
    log.info(
        "ApplyPilot source attempt: %s",
        json.dumps(
            {"source": source, "query": query, "location": location},
            separators=(",", ":"),
        ),
    )


def record_source_result(
    source: str,
    *,
    ok: bool,
    results: int = 0,
    error: str = "",
) -> None:
    """Log one canonical terminal result for a source attempt."""
    log.info(
        "ApplyPilot source result: %s",
        json.dumps(
            {
                "source": source,
                "ok": ok,
                "results": results,
                "error": error,
            },
            separators=(",", ":"),
        ),
    )


def ingest_pipeline_line(line: str) -> str | None:
    """Update structured state from one pipeline log line.

    Returns a concise message when the line should also appear in the
    unified Recent Events panel.
    """
    if not line:
        return None

    full_crawl = _FULL_CRAWL_RE.search(line)
    if full_crawl:
        total = int(full_crawl.group("total"))
        with _lock:
            _state.discovery_total = total
        return f"Discovery started — {total} searches"

    stage_status = _STAGE_STATUS_RE.search(line)
    if stage_status:
        stage = stage_status.group("stage").lower()
        status = stage_status.group("status").lower()
        count = int(stage_status.group("count") or (1 if status == "active" else 0))
        with _lock:
            previous = _state.stages.get(stage, 0)
            _state.stages[stage] = count if status == "active" else 0
            if status == "active" and count > 0:
                _state.last_active_stages[stage] = count
            if stage == "score":
                _state.scoring_queue = count if status == "active" else 0
                if status != "active":
                    _state.scoring_active = 0
            if status == "error":
                _state.pipeline_state = "ERROR"
        if status != "active":
            return None
        if previous == count:
            return None
        label = {
            "discover": "Discovery",
            "enrich": "Enrichment",
            "score": "Score queue:",
            "tailor": "Tailoring",
            "cover": "Cover letters",
            "pdf": "Document conversion",
            "brief": "Interview briefs",
        }[stage]
        unit = "search" if stage == "discover" else "job"
        suffix = "" if count == 1 else "s"
        return f"{label} {count} {unit}{suffix}"

    sites_match = _SITES_RE.search(line)
    if sites_match:
        sites = [
            site.strip()
            for site in sites_match.group("sites").split(",")
            if site.strip()
        ]
        with _lock:
            for site in sites:
                _state.sources.setdefault(_source_name(site), _SourceState())
        return None

    source_event = _SOURCE_EVENT_RE.search(line)
    if source_event:
        try:
            payload = json.loads(source_event.group("payload"))
            source = _source_name(str(payload["source"]))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

        with _lock:
            source_state = _state.sources.setdefault(source, _SourceState())
            if source_event.group("kind") == "attempt":
                source_state.attempts += 1
            elif payload.get("ok") is True:
                source_state.ok += 1
                source_state.jobs += max(0, int(payload.get("results", 0)))
            else:
                source_state.failed += 1
                source_state.last_error = str(payload.get("error") or "ERR")
        return None

    result = _RESULT_RE.search(line)
    if result:
        query = result.group("query").strip()
        location = " ".join(
            result.group("location").split()
        )
        new_count = int(result.group("new"))
        duplicate_count = int(result.group("dupes"))

        with _lock:
            _state.discovery_done += 1
            _state.last_search = f"{query} — {location}"
            _state.new_jobs += new_count
            _state.duplicates += duplicate_count

            done = _state.discovery_done
            total = _state.discovery_total

        # Don't flood Recent Events with 0-new searches.
        if new_count:
            progress = (
                f"{done}/{total}"
                if total
                else str(done)
            )
            return (
                f"Discovery {progress} — "
                f"{new_count} new — {query} / {location}"
            )

        return None

    progress = _PROGRESS_RE.search(line)
    if progress:
        done = int(progress.group("done"))
        total = int(progress.group("total"))
        new_count = int(progress.group("new"))
        duplicates = int(progress.group("dupes"))
        errors = int(progress.group("errors"))

        with _lock:
            _state.discovery_done = done
            _state.discovery_total = total
            _state.new_jobs = new_count
            _state.duplicates = duplicates
            _state.discovery_errors = errors

        return (
            f"Discovery {done}/{total} — "
            f"{new_count} new, {duplicates} duplicates"
        )

    pdf_gen = _PDF_GENERATED_RE.search(line)
    if pdf_gen:
        path = pdf_gen.group("path")
        pages = pdf_gen.group("pages")
        return f"PDF generated: {path} (pages={pages})"

    scoring = _SCORING_RE.search(line)
    if scoring:
        count = int(scoring.group("count"))
        parsed_workers = scoring.group("workers")

        with _lock:
            workers = (
                int(parsed_workers)
                if parsed_workers
                else (_state.scoring_workers or 1)
            )
            previous = (
                _state.scoring_queue,
                _state.scoring_active,
                _state.scoring_workers,
            )
            _state.scoring_queue = count
            _state.scoring_workers = workers
            _state.scoring_active = min(count, workers)
            _state.stages["score"] = count

            current = (
                _state.scoring_queue,
                _state.scoring_active,
                _state.scoring_workers,
            )

        if previous == current:
            return None

        suffix = "" if count == 1 else "s"
        return f"Scoring {count} job{suffix}"

    score_match = _SCORE_RE.search(line)
    if score_match:
        score_raw = score_match.group("score")
        title = score_match.group("title").strip()

        with _lock:
            if _state.scoring_queue:
                _state.scoring_queue -= 1
            _state.scoring_active = min(
                _state.scoring_queue,
                _state.scoring_workers,
            )
            _state.stages["score"] = _state.scoring_queue

            if score_raw != "ERR":
                score = int(score_raw)
                _state.last_score = score
                _state.last_scored_job = title
            else:
                score = None

        if score is None:
            return f"Score failed — {title}"

        return f"Score {score} — {title}"

    message = _clean_message(line)
    lower = message.lower()

    # Surface meaningful downstream stage activity without dumping
    # every raw pipeline log line into the TUI.
    if (
        ("tailor" in lower or "cover letter" in lower or "pdf" in lower)
        and "started thread" not in lower
        and "stages:" not in lower
    ):
        return message[:120]

    return None

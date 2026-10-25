"""Source health tracking and region-aware routing for discovery.

Distinguishes between genuine failures and expected outcomes:
  - OK: request succeeded, jobs returned
  - EMPTY: request succeeded, zero results (not an error)
  - BROKEN: request failed (HTTP error, exception)
  - UNSUPPORTED: source doesn't apply to current region
  - DEGRADED: partial results (some queries failed)
  - SKIPPED: source skipped by configuration
  - UNKNOWN: not yet run

References:
  - feiyangliu2023/JobSpyFeiyang commits 5802239, c20532c
  - santoshmulakidi/JobSpy commit 6d8d203
"""
from __future__ import annotations

import logging
from applypilot import postgres
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Health status enum
# ---------------------------------------------------------------------------

VALID_STATUSES = ("ok", "empty", "broken", "unsupported", "degraded", "deferred", "skipped", "unknown")


@dataclass
class SourceHealth:
    """Per-source health record."""
    source_name: str
    status: str = "unknown"
    last_run_at: str | None = None
    last_success_at: str | None = None
    jobs_found: int = 0
    jobs_stored: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    updated_at: str | None = None


# ---------------------------------------------------------------------------
# Region applicability
# ---------------------------------------------------------------------------

# Maps source name → (location_text → bool) function
# If the function returns False, the source is SKIPPED for that location.
_SOURCE_REGION_SUPPORT: dict[str, Callable[[str], bool]] = {
    "naukri": lambda loc: "india" in loc.lower(),
    "bdjobs": lambda loc: "bangladesh" in loc.lower(),
    "ziprecruiter": lambda loc: any(
        c in loc.lower() for c in ("usa", "us ", "us,", "united states", "canada", "remote", "anywhere")
    ),
    "reed": lambda loc: any(
        c in loc.lower() for c in ("uk", "united kingdom", "london", "england", "scotland", "wales", "remote")
    ),
    "seek": lambda loc: any(
        c in loc.lower() for c in ("australia", "new zealand", "au ", "au,", "nz ", "nz,", "sydney", "melbourne", "auckland", "remote")
    ),
    "irishjobs": lambda loc: any(
        c in loc.lower() for c in ("ireland", "dublin", "cork", "galway", "remote")
    ),
}

# Country-to-region mapping for _SITE_COUNTRY_SUPPORTED style routing
_SOURCE_COUNTRY_SUPPORTED: dict[str, list[str]] = {
    "naukri": ["IN"],
    "bdjobs": ["BD"],
    "ziprecruiter": ["US", "CA"],
    "reed": ["GB"],
    "seek": ["AU", "NZ"],
    "irishjobs": ["IE"],
}


def is_source_supported_for_location(source_name: str, location: str) -> bool:
    """Check if a source is applicable for the given location.

    Returns True if the source has no regional restriction or if the
    location matches the source's supported regions.
    """
    if source_name not in _SOURCE_REGION_SUPPORT:
        return True  # No restriction = always applicable
    return _SOURCE_REGION_SUPPORT[source_name](location)


def is_source_supported_for_country(source_name: str, country_code: str) -> bool:
    """Check if a source is applicable for the given ISO country code.

    Returns True if the source has no country restriction or if the
    country matches.
    """
    if source_name not in _SOURCE_COUNTRY_SUPPORTED:
        return True
    return country_code.upper() in _SOURCE_COUNTRY_SUPPORTED[source_name]


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

_SOURCE_HEALTH_DDL = """
CREATE TABLE IF NOT EXISTS source_health (
    source_name         TEXT PRIMARY KEY,
    status              TEXT NOT NULL DEFAULT 'unknown',
    last_run_at         TEXT,
    last_success_at     TEXT,
    jobs_found          INTEGER DEFAULT 0,
    jobs_stored         INTEGER DEFAULT 0,
    consecutive_failures INTEGER DEFAULT 0,
    last_error          TEXT,
    updated_at          TEXT
);
"""


def ensure_source_health_table(
    conn: postgres.Connection | None = None,
    *,
    commit: bool | None = None,
) -> None:
    """Create the source_health table if it doesn't exist."""
    owns_transaction = conn is None
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    if commit is None:
        commit = owns_transaction
    conn.execute(_SOURCE_HEALTH_DDL)
    conn.execute("ALTER TABLE source_health ADD COLUMN IF NOT EXISTS run_details_json TEXT")
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Health tracking operations
# ---------------------------------------------------------------------------

def record_source_run(
    conn: postgres.Connection,
    source_name: str,
    *,
    status: str,
    jobs_found: int = 0,
    jobs_stored: int = 0,
    error: str | None = None,
    commit: bool = False,
) -> None:
    """Record the outcome of a source run.

    Args:
        conn: Database connection.
        source_name: Canonical source name.
        status: One of VALID_STATUSES.
        jobs_found: Number of jobs found by the source.
        jobs_stored: Number of new jobs stored in DB.
        error: Error message if status is 'broken'.
    """
    if status not in VALID_STATUSES:
        log.warning("Invalid source health status: %s", status)
        status = "broken"

    now = datetime.now(timezone.utc).isoformat()

    successful = status in ("ok", "empty")
    new_failures = 1 if status == "broken" else 0
    last_success = now if successful else None

    conn.execute(
        """INSERT INTO source_health
            (source_name, status, last_run_at, last_success_at,
             jobs_found, jobs_stored, consecutive_failures, last_error, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                status = excluded.status,
                last_run_at = excluded.last_run_at,
                 last_success_at = CASE
                     WHEN excluded.status IN ('ok','empty') THEN excluded.last_success_at
                     ELSE source_health.last_success_at END,
                 jobs_found = excluded.jobs_found,
                 jobs_stored = excluded.jobs_stored,
                 consecutive_failures = CASE
                     WHEN excluded.status IN ('ok','empty') THEN 0
                     WHEN excluded.status = 'broken' THEN source_health.consecutive_failures + 1
                     ELSE source_health.consecutive_failures END,
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
        """,
        (source_name, status, now, last_success, jobs_found, jobs_stored,
         new_failures, error, now),
    )
    if commit:
        conn.commit()


def get_source_health(conn: postgres.Connection, source_name: str) -> SourceHealth | None:
    """Get health record for a single source."""
    row = conn.execute(
        "SELECT source_name, status, last_run_at, last_success_at, "
        "jobs_found, jobs_stored, consecutive_failures, last_error, updated_at "
        "FROM source_health WHERE source_name = ?",
        (source_name,),
    ).fetchone()
    if not row:
        return None
    return SourceHealth(
        source_name=row[0],
        status=row[1],
        last_run_at=row[2],
        last_success_at=row[3],
        jobs_found=row[4],
        jobs_stored=row[5],
        consecutive_failures=row[6],
        last_error=row[7],
        updated_at=row[8],
    )


def get_all_source_health(conn: postgres.Connection) -> list[SourceHealth]:
    """Get health records for all sources."""
    rows = conn.execute(
        "SELECT source_name, status, last_run_at, last_success_at, "
        "jobs_found, jobs_stored, consecutive_failures, last_error, updated_at "
        "FROM source_health ORDER BY source_name"
    ).fetchall()
    return [
        SourceHealth(
            source_name=r[0], status=r[1], last_run_at=r[2],
            last_success_at=r[3], jobs_found=r[4], jobs_stored=r[5],
            consecutive_failures=r[6], last_error=r[7], updated_at=r[8],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Health summary
# ---------------------------------------------------------------------------

_STATUS_ICONS = {
    "ok": "✓",
    "empty": "·",
    "broken": "✗",
    "unsupported": "⊘",
    "degraded": "⚠",
    "skipped": "⏭",
    "unknown": "?",
}

_STATUS_COLORS = {
    "ok": "green",
    "empty": "dim",
    "broken": "red",
    "unsupported": "yellow",
    "degraded": "yellow",
    "skipped": "yellow",
    "unknown": "dim",
}


def format_health_summary(health_records: list[SourceHealth]) -> str:
    """Format source health records into a human-readable summary."""
    lines = ["SOURCE HEALTH SUMMARY:"]
    for h in health_records:
        icon = _STATUS_ICONS.get(h.status, "?")
        color = _STATUS_COLORS.get(h.status, "dim")
        detail = ""
        if h.status == "ok":
            detail = f"({h.jobs_found} jobs, {h.jobs_stored} new)"
        elif h.status == "empty":
            detail = "(0 jobs)"
        elif h.status == "broken":
            detail = f"({h.last_error or 'unknown error'})"
        elif h.status == "unsupported":
            detail = "(region not applicable)"
        elif h.status == "degraded":
            detail = f"(partial, {h.jobs_found} jobs)"
        elif h.status == "skipped":
            detail = "(configuration)"
        elif h.consecutive_failures > 0:
            detail = f"({h.consecutive_failures} consecutive failures)"

        lines.append(f"  [{color}]{icon} {h.source_name}: {h.status.upper()} {detail}[/{color}]")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Decorator for automatic health tracking
# ---------------------------------------------------------------------------

def track_source_health(source_name: str):
    """Decorator that automatically records source health on completion.

    Usage:
        @track_source_health("greenhouse")
        def run_greenhouse_discovery(workers=1):
            ...
            return {"found": 42, "new": 10, "errors": []}
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            from applypilot.database import get_connection
            conn = get_connection()
            try:
                result = func(*args, **kwargs)
                # Extract stats from return value
                errors = result.get("errors", []) if isinstance(result, dict) else []
                found = result.get("found", 0) if isinstance(result, dict) else 0
                new = result.get("new", 0) if isinstance(result, dict) else 0

                if errors:
                    status = "degraded" if new > 0 else "broken"
                    error_msg = str(errors[0]) if errors else None
                elif found == 0:
                    status = "empty"
                    error_msg = None
                else:
                    status = "ok"
                    error_msg = None

                record_source_run(
                    conn, source_name,
                    status=status,
                    jobs_found=found,
                    jobs_stored=new,
                    error=error_msg,
                    commit=True,
                )
                return result
            except Exception as e:
                record_source_run(
                    conn, source_name,
                    status="broken",
                    error=f"{type(e).__name__}: {e}",
                    commit=True,
                )
                raise
        return wrapper
    return decorator

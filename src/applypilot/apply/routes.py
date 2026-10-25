"""Application-route normalization and persistence.

Routes describe *how* a job is applied to.  They are deliberately separate
from ATS detection: a perfectly ordinary company careers page is a valid
external route even when ApplyPilot does not recognize the form vendor.
"""

from __future__ import annotations

import re
from applypilot import postgres
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit


RouteKind = Literal["external", "linkedin-easy-apply", "email"]

_SENTINELS = {"", "none", "null", "nan", "n/a", "na", "undefined", "<na>"}
_AGGREGATOR_HOST_RE = re.compile(
    r"(^|\.)(whatjobs\.[a-z.]+|jobleads\.[a-z.]+|jooble\.[a-z.]+|"
    r"adzuna\.[a-z.]+|talent\.com|bebee\.[a-z.]+|jobrapido\.com|"
    r"freehire\.me|indeed\.[a-z.]+|glassdoor\.[a-z.]+|ziprecruiter\.com)$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class ApplicationRoute:
    route: RouteKind
    target: str
    evidence: str
    resolved_at: str


def parse_email_target(value: object) -> dict[str, str]:
    """Decode an explicit application address, never an arbitrary message command."""
    text = str(value if value is not None else "").strip()
    if len(text) > 12000 or not text.lower().startswith("mailto:") or any(ord(c) < 32 for c in text):
        raise ValueError("An explicit mailto application link is required")
    parsed = urlsplit(text)
    if parsed.netloc or parsed.fragment:
        raise ValueError("Malformed application email link")
    recipient = unquote(parsed.path)
    from applypilot.referrals.store import email_address
    if any(c in recipient for c in "\r\n,;<>") or recipient.count("@") != 1:
        raise ValueError("Application email requires one unambiguous recipient")
    recipient = email_address(recipient)
    fields = {}
    for key, item in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=10):
        key = key.casefold()
        if key not in {"subject", "body"} or key in fields:
            raise ValueError("Application email links may specify only one subject and body; CC/BCC are unsupported")
        if "\x00" in item or (key == "subject" and any(ord(c) < 32 for c in item)):
            raise ValueError("Application email contains an invalid header")
        if len(item) > (500 if key == "subject" else 10000):
            raise ValueError("Application email link field is too long")
        fields[key] = item
    canonical = "mailto:" + quote(recipient, safe="@.+-_")
    if fields:
        canonical += "?" + urlencode(sorted(fields.items()))
    return {"target": canonical, "recipient": recipient,
            "subject": fields.get("subject", ""), "body": fields.get("body", "")}


def normalize_public_url(value: object) -> str | None:
    """Return a normalized public HTTP(S) URL or ``None``.

    pandas/JobSpy frequently expose missing values as strings (notably
    ``"None"`` and ``"nan"``).  Treating those strings as real application
    URLs was the source of the legacy manual-only queue corruption.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _SENTINELS:
        return None
    from applypilot.discovery.url_normalize import unwrap_linkedin_job_url
    text = unwrap_linkedin_job_url(text)
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    # Fragments never identify a distinct application endpoint.
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))


def host_for(value: object) -> str:
    url = normalize_public_url(value)
    if not url:
        return ""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def is_linkedin_url(value: object) -> bool:
    host = host_for(value)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def is_aggregator_url(value: object) -> bool:
    host = host_for(value)
    return bool(host and _AGGREGATOR_HOST_RE.search(host))


def application_target_kind(value: object, *, title: object = "") -> str:
    """Describe admission hazards without changing the persisted delivery channel."""
    if str(value or "").lower().startswith("mailto:"):
        return "email"
    url = normalize_public_url(value)
    if not url:
        return "missing"
    if re.search(r"\b(?:talent (?:network|pool|community)|general application|expression of interest)\b", str(title), re.I):
        return "talent-network"
    parsed = urlsplit(url)
    parts = [part.lower() for part in parsed.path.strip("/").split("/") if part]
    query = dict(parse_qsl(parsed.query))
    posting_id = any(query.get(key) for key in ("job_id", "jobId", "jobid", "jid", "gh_jid", "reqid", "requisitionId", "jk", "vjk"))
    generic_parts = {"jobs", "job", "careers", "career", "search", "search-jobs", "job-search", "vacancies", "openings", "opportunities", "positions"}
    # Locale prefixes do not make a listing page a canonical posting.
    meaningful = [part for part in parts if not re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2})?", part)]
    if not posting_id and (not meaningful or all(part in generic_parts for part in meaningful)
                           or meaningful[-1] in {"jobs", "careers", "search", "search-jobs", "job-search", "vacancies", "openings"}):
        return "generic-listing"
    if is_aggregator_url(url):
        return "aggregator"
    return "posting"


def application_route_blocker(job: dict, *, check_suitability: bool = True) -> str | None:
    """Reject unresolved delivery routes before allocating a browser worker."""
    from applypilot.integrations.research import posting_suitability_blocker
    if check_suitability:
        if reason := posting_suitability_blocker(job):
            return reason
    channel = job.get("application_route")
    target = job.get("application_url")
    kind = application_target_kind(target, title=job.get("title"))
    if kind == "email" and channel == "email":
        return None
    if kind == "talent-network":
        return "application_route_talent_network"
    if kind in {"missing", "generic-listing", "aggregator"}:
        return "application_route_unresolved"
    if channel == "external" and is_linkedin_url(target):
        return "application_route_unresolved"
    return None


def classify_application_route(
    *,
    listing_url: object,
    application_url: object = None,
    source: str | None = None,
    linkedin_easy_apply: bool = False,
    evidence: str | None = None,
) -> ApplicationRoute | None:
    """Classify a route without assuming that unknown ATSes are manual-only."""
    from applypilot.discovery.url_normalize import is_linkedin_auth_url
    listing = normalize_public_url(listing_url)
    if listing and is_linkedin_auth_url(listing):
        listing = None
    if str(application_url if application_url is not None else "").strip().lower().startswith("mailto:"):
        email = parse_email_target(application_url)
        if not listing:
            return None
        return ApplicationRoute("email", email["target"],
                                evidence or f"Explicit application email supplied for {listing}",
                                datetime.now(UTC).isoformat())
    direct = normalize_public_url(application_url)
    if direct and is_linkedin_auth_url(direct):
        direct = None
    target = direct or listing
    if not target:
        return None

    now = datetime.now(UTC).isoformat()
    if is_linkedin_url(target):
        if linkedin_easy_apply:
            return ApplicationRoute(
                "linkedin-easy-apply",
                target,
                evidence or "LinkedIn Easy Apply verified",
                now,
            )
        return ApplicationRoute(
            "external",
            target,
            evidence or "Unresolved LinkedIn listing: requires a posting-specific external application destination",
            now,
        )

    if is_aggregator_url(target):
        return ApplicationRoute(
            "external",
            target,
            evidence or "Unresolved aggregator listing: canonical posting and application destination required",
            now,
        )

    origin = (source or "discovery").strip() or "discovery"
    return ApplicationRoute(
        "external",
        target,
        evidence or f"Direct public application target from {origin}",
        now,
    )


def persist_route(
    conn: postgres.Connection,
    job_url: str,
    route: ApplicationRoute,
) -> None:
    """Persist route identity, incrementing its fence only when identity changes."""
    from applypilot.referrals.store import preserve_route
    route = preserve_route(conn, job_url, route)
    columns = {row[1] for row in conn.table_info('application_routes')}
    if "version" not in columns:
        conn.execute("ALTER TABLE application_routes ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        INSERT INTO application_routes
            (job_url, route, target, evidence, resolved_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(job_url) DO UPDATE SET
            version = application_routes.version + CASE
                WHEN application_routes.route IS DISTINCT FROM excluded.route
                  OR application_routes.target IS DISTINCT FROM excluded.target THEN 1 ELSE 0 END,
            route = excluded.route,
            target = excluded.target,
            evidence = excluded.evidence,
            resolved_at = excluded.resolved_at
        """,
        (
            job_url,
            route.route,
            route.target,
            route.evidence,
            route.resolved_at,
        ),
    )


def infer_and_persist_route(
    conn: postgres.Connection,
    *,
    job_url: str,
    application_url: object = None,
    source: str | None = None,
    linkedin_easy_apply: bool = False,
    evidence: str | None = None,
) -> ApplicationRoute | None:
    route = classify_application_route(
        listing_url=job_url,
        application_url=application_url,
        source=source,
        linkedin_easy_apply=linkedin_easy_apply,
        evidence=evidence,
    )
    if route:
        persist_route(conn, job_url, route)
    return route


def get_route(conn: postgres.Connection, job_url: str) -> ApplicationRoute | None:
    row = conn.execute(
        "SELECT route, target, evidence, resolved_at "
        "FROM application_routes WHERE job_url = ?",
        (job_url,),
    ).fetchone()
    if not row:
        return None
    return ApplicationRoute(
        route=row["route"],
        target=row["target"],
        evidence=row["evidence"],
        resolved_at=row["resolved_at"],
    )


def route_dict(route: ApplicationRoute | None) -> dict | None:
    return asdict(route) if route else None

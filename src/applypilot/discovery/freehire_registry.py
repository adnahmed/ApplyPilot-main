"""Persist FreeHire company inventories and lower them into native ATS targets.

The scheduler owns pagination and publication. Inventory execution returns
company metadata and board leads without inserting jobs.
"""
from __future__ import annotations

import json
import logging
from applypilot import postgres
from dataclasses import dataclass, field
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Company:
    """A company from the FreeHire registry."""
    slug: str
    name: str
    collections: list[str] = field(default_factory=list)
    company_type: str = ""
    company_size: str = ""
    hq_country: str = ""
    regions: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    ats_provider: str | None = None
    career_url: str | None = None
    verification_status: str = "unverified"
    job_count: int = 0
    employee_count: int | None = None
    year_founded: int | None = None
    tagline: str = ""
    raw_metadata: dict = field(default_factory=dict)

    @classmethod
    def from_freehire(cls, data: dict) -> Company:
        """Parse a company from FreeHire API response."""
        return cls(
            slug=data.get("slug", ""),
            name=data.get("name", ""),
            collections=data.get("collections", []),
            company_type=data.get("company_type", "") or (data.get("company_types", [""])[0] if data.get("company_types") else ""),
            company_size=data.get("company_size", "") or (data.get("company_sizes", [""])[0] if data.get("company_sizes") else ""),
            hq_country=data.get("hq_country", ""),
            regions=data.get("regions", []),
            countries=data.get("countries", []),
            industries=data.get("industries", []),
            domains=data.get("domains", []),
            ats_provider=data.get("ats_provider"),
            career_url=data.get("career_url"),
            verification_status="freehire",
            job_count=data.get("job_count", 0),
            employee_count=data.get("employee_count"),
            year_founded=data.get("year_founded"),
            tagline=data.get("tagline", ""),
            raw_metadata=dict(data),
        )


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS company_registry (
    slug                TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    collections         TEXT,  -- JSON array
    company_type        TEXT,
    company_size        TEXT,
    hq_country          TEXT,
    regions             TEXT,  -- JSON array
    countries           TEXT,  -- JSON array
    industries          TEXT,  -- JSON array
    domains             TEXT,  -- JSON array
    ats_provider        TEXT,
    career_url          TEXT,
    verification_status TEXT DEFAULT 'unverified',
    last_verified_at    TEXT,
    source              TEXT DEFAULT 'freehire',
    job_count           INTEGER DEFAULT 0,
    employee_count      INTEGER,
    year_founded        INTEGER,
    tagline             TEXT,
    updated_at          TEXT,
    raw_metadata        TEXT
);
"""


def ensure_registry_table(
    conn: postgres.Connection | None = None,
    *,
    commit: bool | None = None,
) -> None:
    """Create the company_registry table if it doesn't exist."""
    owns_transaction = conn is None
    if conn is None:
        from applypilot.database import get_connection
        conn = get_connection()
    if commit is None:
        commit = owns_transaction
    conn.execute(_REGISTRY_DDL)
    columns = {row[1] for row in conn.table_info('company_registry')}
    if "raw_metadata" not in columns:
        conn.execute("ALTER TABLE company_registry ADD COLUMN raw_metadata TEXT")
    if commit:
        conn.commit()


# ---------------------------------------------------------------------------
# Registry operations
# ---------------------------------------------------------------------------

def store_companies(
    conn: postgres.Connection,
    companies: list[Company],
    *,
    commit: bool = True,
) -> tuple[int, int]:
    """Store companies in the registry using UPSERT.

    Returns (new_count, updated_count).
    """
    ensure_registry_table(conn, commit=False)
    now = datetime.now(UTC).isoformat()
    new = updated = 0

    for company in companies:
        try:
            existed = conn.execute(
                "SELECT 1 FROM company_registry WHERE slug = ?", (company.slug,)
            ).fetchone() is not None
            result = conn.execute(
                """INSERT INTO company_registry
                    (slug, name, collections, company_type, company_size,
                     hq_country, regions, countries, industries, domains,
                     ats_provider, career_url, verification_status, source,
                      job_count, employee_count, year_founded, tagline, updated_at, raw_metadata)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slug) DO UPDATE SET
                        name = COALESCE(excluded.name, company_registry.name),
                        collections = COALESCE(excluded.collections, company_registry.collections),
                        company_type = COALESCE(excluded.company_type, company_registry.company_type),
                        company_size = COALESCE(excluded.company_size, company_registry.company_size),
                        hq_country = COALESCE(excluded.hq_country, company_registry.hq_country),
                        regions = COALESCE(excluded.regions, company_registry.regions),
                        countries = COALESCE(excluded.countries, company_registry.countries),
                        industries = COALESCE(excluded.industries, company_registry.industries),
                        domains = COALESCE(excluded.domains, company_registry.domains),
                        ats_provider = COALESCE(excluded.ats_provider, company_registry.ats_provider),
                        career_url = COALESCE(excluded.career_url, company_registry.career_url),
                        verification_status = COALESCE(excluded.verification_status, company_registry.verification_status),
                        source = excluded.source,
                        job_count = excluded.job_count,
                        employee_count = COALESCE(excluded.employee_count, company_registry.employee_count),
                        year_founded = COALESCE(excluded.year_founded, company_registry.year_founded),
                         tagline = COALESCE(excluded.tagline, company_registry.tagline),
                         raw_metadata = excluded.raw_metadata,
                        updated_at = excluded.updated_at
                """,
                (
                    company.slug, company.name,
                    json.dumps(company.collections),
                    company.company_type, company.company_size,
                    company.hq_country,
                    json.dumps(company.regions),
                    json.dumps(company.countries),
                    json.dumps(company.industries),
                    json.dumps(company.domains),
                    company.ats_provider, company.career_url,
                    company.verification_status, "freehire",
                    company.job_count, company.employee_count,
                    company.year_founded, company.tagline, now, json.dumps(company.raw_metadata),
                ),
            )
            if result.rowcount > 0:
                if existed:
                    updated += 1
                else:
                    new += 1
        except Exception as e:
            log.warning("Failed to store company %s: %s", company.slug, e)

    if commit:
        conn.commit()
    return new, updated


def list_board_targets(companies=(), *, configured=None):
    """Local planner inventory; accepts preloaded rows and never opens a DB."""
    from applypilot.discovery.ats_adapter import list_board_targets as inventory
    rows = []
    for company in companies:
        if isinstance(company, Company):
            from dataclasses import asdict
            rows.append(asdict(company))
        else:
            rows.append(company)
    return inventory(rows, configured=configured)


def execute_inventory_task(task, context, cursor=None):
    """Optional public GET /companies page; deliberately returns NO job records.

    Target: provider=freehire (or ats_registry), scope=companies,
    kind=company_inventory. Params: limit (1..100), q, collections, countries,
    regions, hq_country, optional endpoint=https://freehire.me/api/v1/companies.
    Metadata companies contains original rows; board_leads contains serialized
    SourceTargets. Main owns persistence/checkpoints and scheduling those boards.
    """
    from dataclasses import asdict
    from urllib.parse import urlencode
    from applypilot.discovery.ats_common import failure_page
    from applypilot.discovery.careers.base import offset_cursor, request
    from applypilot.discovery.models import Coverage, DiscoveryPage, FailureKind, FetchFailure
    from applypilot.discovery.transport import task_params, page_metadata

    metadata = {"inventory_only": True, "authoritative": False, "cursor": cursor}
    try:
        params = task_params(task, ("freehire", "ats_registry"))
        if task.target.kind != "company_inventory" or task.target.scope != "companies":
            raise ValueError("FreeHire inventory requires kind=company_inventory and scope=companies")
        if context is None:
            raise ValueError("Company inventory requires a discovery context")
        grants = context.runtime.get("authorized_sources", {})
        if grants.get("freehire") is False or grants.get(task.target.provider) is False:
            raise PermissionError("FreeHire inventory access denied by runtime policy")
        allowed = {"limit", "q", "collections", "countries", "regions", "hq_country", "endpoint"}
        if set(params) - allowed:
            raise ValueError("Unsupported company inventory parameters: " + ", ".join(sorted(set(params) - allowed)))
        endpoint = params.pop("endpoint", "https://freehire.me/api/v1/companies")
        if endpoint != "https://freehire.me/api/v1/companies":
            raise PermissionError("Only the public FreeHire /companies endpoint is allowed")
        limit = params.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("FreeHire inventory limit must be 1..100")
        state = offset_cursor(task, cursor)
        offset = state["offset"]
        if offset >= 10000:
            raise ValueError("FreeHire inventory offset reached the 10000 depth cap")
        params.update(limit=min(limit, 10000 - offset), offset=offset)
        for key in ("collections", "countries", "regions"):
            value = params.get(key)
            if isinstance(value, (tuple, list)) and all(isinstance(x, str) and x.strip() for x in value):
                params[key] = ",".join(value)
            elif value is not None and not isinstance(value, str):
                raise ValueError(f"Company {key} must be text or a list of text")
        for key in ("q", "hq_country"):
            if key in params and not isinstance(params[key], str):
                raise ValueError(f"Company {key} must be text")
        url = endpoint + "?" + urlencode(params)
        metadata.update(url=url, offset=offset, limit=params["limit"])
        data = request(context, url)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("FreeHire company inventory requires data[]")
        meta = data.get("meta") or {}
        if not isinstance(meta, dict):
            raise ValueError("FreeHire company metadata must be an object")
        if set(meta.get("ignored_params") or ()) & set(params):
            raise ValueError("FreeHire ignored planned company filters")
        rows = data["data"]
        total = meta.get("total", data.get("total"))
        if total is not None and (type(total) is not int or total < 0):
            raise ValueError("Invalid FreeHire company total")
        if len(rows) > params["limit"] or total is not None and offset + len(rows) > total:
            raise ValueError("FreeHire inventory page contradicts pagination bounds")
        if "total" in state and total != state["total"]:
            raise ValueError("Company inventory changed during pagination; restart")
        if not rows and total is not None and offset < total:
            raise ValueError("Empty company page before total")
        companies, failures = [], []
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("slug"), str) or not row["slug"]:
                failures.append(FetchFailure(FailureKind.SCHEMA, "Company requires a slug", provider_field=f"data[{index}]"))
            else:
                companies.append(dict(row, origins=[*row.get("origins", ()), f"freehire:companies:{row['slug']}"]))
        next_offset = offset + len(rows)
        exhausted = next_offset >= total if total is not None else len(rows) < params["limit"]
        capped = next_offset >= 10000 and not exhausted
        next_state = {"request_key": task.target.request_key(), "offset": next_offset}
        if total is not None:
            next_state["total"] = total
        next_cursor = None if exhausted or capped else json.dumps(next_state, sort_keys=True)
        leads = list_board_targets(companies, configured={})
        metadata.update(companies=companies, board_leads=[asdict(target) for target in leads], total=total)
        return DiscoveryPage(task.task_id or "", records=(), next_cursor=next_cursor,
                             exhausted=exhausted and not failures, complete=not capped and not failures,
                             capped=capped, partial=bool(failures), failures=tuple(failures),
                             coverage=Coverage.CAPPED if capped else Coverage.PARTIAL if failures else Coverage.COMPLETE,
                             request_metadata=page_metadata(task, context, **metadata))
    except Exception as error:
        return failure_page(task, error, metadata=metadata)


def store_board_leads(conn: postgres.Connection, leads, *, origin: str = "discovery"):
    """Persist observed public career/ATS surfaces in the existing registry.

    This is deliberately not a second source registry.  It stores only enough
    metadata for ats_adapter.list_registry_targets() to lower the observation
    into the repository's existing provider machinery.
    """
    from hashlib import sha256
    from urllib.parse import urlsplit, urlunsplit
    from applypilot.discovery.careers import detect_provider
    from applypilot.discovery.ats_adapter import list_board_targets

    ensure_registry_table(conn, commit=False)
    created = []
    for lead in leads or ():
        if not isinstance(lead, dict):
            continue
        raw_url = lead.get("career_url") or lead.get("url") or lead.get("endpoint")
        if not isinstance(raw_url, str):
            continue
        parsed = urlsplit(raw_url.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        canonical = urlunsplit(("https", parsed.hostname.lower(), parsed.path or "/", parsed.query, ""))
        detected = lead.get("provider") or detect_provider(canonical)
        if detected == "personio" and not parsed.path.lower().endswith("/xml"):
            canonical = f"https://{parsed.hostname.lower()}/xml?language=en"
        provider = detected if detected in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters", "bamboohr", "jazzhr"} else "generic"
        name = str(lead.get("name") or parsed.hostname).strip()
        slug = "observed-" + sha256(canonical.encode()).hexdigest()[:20]
        existing = conn.execute(
            "SELECT slug,verification_status FROM company_registry WHERE slug=? OR career_url=? LIMIT 1",
            (slug, canonical),
        ).fetchone()
        if existing:
            # Never resurrect an explicitly rejected/revoked observation.
            if existing[1] in {"rejected", "revoked", "deleted"}:
                continue
            continue
        metadata = {
            "slug": slug, "name": name, "career_url": canonical,
            "provider": provider, "ats_provider": provider,
            "detected_provider": detected,
            "origins": list(dict.fromkeys([*(lead.get("origins") or ()), origin])),
        }
        targets = list_board_targets([metadata], configured={})
        if not targets:
            continue
        metadata["tenant"] = targets[0].scope
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "INSERT INTO company_registry "
            "(slug,name,ats_provider,career_url,verification_status,source,updated_at,raw_metadata) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(slug) DO NOTHING",
            (slug, name, provider, canonical, "observed", "discovery", now,
             json.dumps(metadata, ensure_ascii=False)),
        )
        created.extend(targets)
    return tuple(created)

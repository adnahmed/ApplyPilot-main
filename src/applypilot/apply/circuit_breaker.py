"""Compatibility reader for pre-migration provider holds; no circuit-breaker engine.

Existing open holds are deliberately not cleared by migration. Only an explicit
operator clear removes them. New page restrictions use the application's
existing verification-required business outcome instead of shared error types.
"""
from urllib.parse import urlsplit
from applypilot.database import get_connection, commit_with_retry

SHARED_ATS_HOSTS = frozenset({
    "jobs.ashbyhq.com", "jobs.lever.co", "jobs.eu.lever.co",
    "boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io",
})

def domain_from_target(target: str) -> str:
    try:
        return (urlsplit(target).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""

def scope_from_target(target: str) -> str:
    """Shared ATS paths identify employers; a tenant error is not host-wide."""
    domain = domain_from_target(target)
    if domain in SHARED_ATS_HOSTS:
        tenant = urlsplit(target).path.strip("/").split("/", 1)[0].casefold()
        if tenant:
            return domain + "/" + tenant
    return domain

def clear(domain: str, conn=None) -> None:
    """Operator-only clear primitive."""
    conn = conn or get_connection()
    conn.execute("DELETE FROM domain_circuit_breakers WHERE domain=?", (domain,))
    commit_with_retry(conn)


def status(domain: str, conn=None) -> dict:
    conn = conn or get_connection()
    domain = domain.lower().removeprefix('www.')
    row = conn.execute('SELECT domain,status,reason FROM domain_circuit_breakers WHERE domain=?', (domain,)).fetchone()
    blocked = bool(row and row['status'] == 'open')
    return {'domain': domain, 'open': blocked, 'reason': row['reason'] if row else None,
            'manual_clear': blocked, 'retry_after': None}


def target_status(target: str, conn=None) -> dict:
    scoped = status(scope_from_target(target), conn)
    return scoped if scoped['open'] else status(domain_from_target(target), conn)

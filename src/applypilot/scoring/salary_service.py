"""One salary observation; Temporal owns independent execution and retries."""
from __future__ import annotations
from contextlib import closing


def refresh_salary(url: str, *, profile=None) -> dict:
    from applypilot import postgres, config
    from .market_salary import resolve_job_market_salary
    with closing(postgres.connect(config.DB_PATH, timeout=2, isolation_level=None)) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE url=? AND scored_at IS NOT NULL "
                           "AND eligibility='eligible'", (url,)).fetchone()
        if row is None:
            return {"status": "not_eligible"}
        job = dict(row)
    # Provider has at most four role-scoped attempts, each with a five-second
    # socket timeout and no physical retry. Results are cached separately.
    result = resolve_job_market_salary(job, profile=profile)
    if result.get("status") != "available":
        return {"status": "unavailable", "result": result}
    def amount(name):
        value = result.get(name)
        return round(float(value)) if value is not None else None
    with closing(postgres.connect(config.DB_PATH, timeout=2)) as conn:
        with conn:
            changed = conn.execute(
                "UPDATE jobs SET salary_p25=?,salary_p50=?,salary_p75=?,salary_source=?,"
                "salary_sample_size=?,salary_currency=?,salary_period=? WHERE url=? "
                "AND discovery_assessment_revision IS NOT DISTINCT FROM ? "
                "AND score_assessment_revision IS NOT DISTINCT FROM ? AND scored_at=?",
                (amount("p25"), amount("p50"), amount("p75"), result.get("source"),
                 result.get("sample_size"), result.get("currency"), result.get("period"), url,
                 job.get("discovery_assessment_revision"), job.get("score_assessment_revision"),
                 job["scored_at"]),
            ).rowcount
    return {"status": "available" if changed else "stale_discarded", "result": result}



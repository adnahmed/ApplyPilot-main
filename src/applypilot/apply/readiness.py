"""Transactional application-candidate projection, schema version 1.

Only preparation facts are materialized. Discovery/assessment revision, current
policy, route, company cap, duplicate/attempt protection and artifact content are
ALWAYS evaluated from current state by the existing queue and reservation guard.
Thus a stale policy cannot grant permission and an artifact created without a DB
update remains discoverable. Triggers also cover SQL writers outside this process.
"""
from __future__ import annotations
from applypilot import postgres

COLUMNS = ("url", "eligibility", "apply_status", "state", "agent_id", "fit_score",
           "scored_at", "tailored_resume_path", "application_contract_json")

def predicate(alias: str) -> str:
    # alias is an internal SQL identifier, never user input.
    if alias not in {"j", "NEW"}:
        raise ValueError("invalid readiness alias")
    return (f"{alias}.eligibility='eligible' AND {alias}.fit_score IS NOT NULL "
            f"AND {alias}.scored_at IS NOT NULL AND (NULLIF({alias}.tailored_resume_path,'') IS NOT NULL "
            f"OR NULLIF({alias}.application_contract_json,'')::jsonb #>> '{{documents,resume,status}}'='prohibited') "
            f"AND ({alias}.apply_status IS NULL OR {alias}.apply_status='failed') "
            f"AND COALESCE({alias}.state,'') NOT IN "
            "('applying','applied','verification_required','verification_quarantined')")

def ensure_schema(conn: postgres.Connection) -> None:
    """Add/backfill once in the caller's transaction; never commit or reserve."""
    exists = conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename='apply_readiness'").fetchone()
    conn.execute("CREATE TABLE IF NOT EXISTS apply_readiness (job_url TEXT PRIMARY KEY "
                 "REFERENCES jobs(url) ON DELETE CASCADE, fit_score DOUBLE PRECISION, schema_version INTEGER NOT NULL DEFAULT 1)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_apply_readiness_score ON apply_readiness(fit_score DESC,job_url)")
    insert = ("INSERT INTO apply_readiness(job_url,fit_score) SELECT NEW.url,NEW.fit_score WHERE "
              + predicate("NEW") + ";")
    # Refresh predicates on upgrade; existing databases must not retain retired states.
    conn.execute("""CREATE OR REPLACE FUNCTION ap_refresh_apply_readiness()
        RETURNS trigger LANGUAGE plpgsql SET search_path FROM CURRENT AS $$
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                DELETE FROM apply_readiness WHERE job_url=OLD.url;
            END IF;
            IF TG_OP <> 'DELETE' THEN
                """ + insert + """
            END IF;
            RETURN NULL;
        END $$""")
    for name, event in (("insert", "INSERT"), ("update", "UPDATE OF " + ",".join(COLUMNS)),
                        ("delete", "DELETE")):
        conn.execute(f"CREATE OR REPLACE TRIGGER apply_readiness_{name} AFTER {event} ON jobs "
                     "FOR EACH ROW EXECUTE FUNCTION ap_refresh_apply_readiness()")
    if not exists:
        conn.execute("INSERT INTO apply_readiness(job_url,fit_score) SELECT j.url,j.fit_score FROM jobs j WHERE "
                     + predicate("j"))

def present(conn: postgres.Connection) -> bool:
    return conn.execute("SELECT 1 FROM pg_tables WHERE schemaname=current_schema() AND tablename='apply_readiness'").fetchone() is not None

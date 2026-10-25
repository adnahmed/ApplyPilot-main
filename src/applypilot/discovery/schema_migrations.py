"""Explicit, data-preserving retirement of proven-unused discovery coordination.

This is deployment DDL, never called by plan, provider or publication Activities.
Only the obsolete singleton cycle gate and target worker-run history are archived
here. Other discovery tables still have legacy readers and must NOT be dropped.
"""


def retire_cycle_gate(conn):
    from .repository import transaction
    source = "discovery_cycle_lease"
    archive = "retired_discovery_cycle_lease_v1"
    with transaction(conn, resource="schema-v1"):
        existing = {row["table_name"] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN (?,?)", (source, archive)).fetchall()}
        if source in existing and archive in existing:
            # Do not overwrite an earlier archive if an old binary recreated the
            # live table. An operator must reconcile both histories explicitly.
            raise RuntimeError("Both discovery cycle tables exist; reconcile the archive before retirement")
        if source in existing:
            # Rename preserves every row, value, constraint and index. No CASCADE,
            # projection of selected columns, or destructive loss of old evidence.
            conn.execute("ALTER TABLE discovery_cycle_lease RENAME TO retired_discovery_cycle_lease_v1")
        count = (conn.execute("SELECT COUNT(*) AS count FROM retired_discovery_cycle_lease_v1")
                 .fetchone()["count"] if existing else 0)
    return {"migration": "retire-discovery-cycle-gate-v1", "archived_table": archive if existing else None,
            "rows_preserved": count, "changed": source in existing,
            "other_discovery_execution_state_retired": False}


def retire_target_runs(conn):
    """Preserve old worker-run history after deploying claim-free target code."""
    from .repository import transaction
    source = "company_target_runs"
    archive = "retired_company_target_runs_v1"
    with transaction(conn, resource="schema-v1"):
        existing = {row["table_name"] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema=current_schema() "
            "AND table_name IN (?,?)", (source, archive)).fetchall()}
        if source in existing and archive in existing:
            raise RuntimeError("Both target-run tables exist; reconcile the archive before retirement")
        if source in existing:
            conn.execute("ALTER TABLE company_target_runs RENAME TO retired_company_target_runs_v1")
        count = (conn.execute("SELECT COUNT(*) AS count FROM retired_company_target_runs_v1")
                 .fetchone()["count"] if existing else 0)
    return {"migration": "retire-target-worker-runs-v1", "archived_table": archive if existing else None,
            "rows_preserved": count, "changed": source in existing,
            "target_due_clocks_retired": False, "jobseek_traversal_migrated": False}

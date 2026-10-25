"""Networking business evidence only. Execution lives in Temporal histories."""
from __future__ import annotations

import asyncio
from applypilot.referrals import store as s

def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS unattended_send_windows(
        outbox_id TEXT PRIMARY KEY REFERENCES referral_outbox(id),
        payload_hash TEXT NOT NULL, basis_hash TEXT NOT NULL, grant_id TEXT NOT NULL,
        grace_seconds INTEGER NOT NULL, opened_at TEXT NOT NULL, send_after TEXT NOT NULL,
        notified_at TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS unattended_social_intents(
        outbox_id TEXT PRIMARY KEY, before_ids_json TEXT NOT NULL, created_at TEXT NOT NULL)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS unattended_network_files(
        path TEXT PRIMARY KEY, digest TEXT NOT NULL, imported_at TEXT NOT NULL)""")



async def call(operation, *args, path=None, **kwargs):
    """Run a bounded store operation off asyncio with a thread-owned connection.

    Never pass a live connection into a generic executor. The callback must not
    perform browser/model/network IO, and returns only detached data.
    """
    from contextlib import closing
    from applypilot import postgres, config
    def run():
        with closing(postgres.connect(path or config.DB_PATH,timeout=2,isolation_level=None)) as owned:
            return operation(owned,*args,**kwargs)
    return await asyncio.to_thread(run)



def incident(conn, component, reason, *, cause=None):
    from applypilot import operations
    operations.record(component_name='networking', operation=component,
                      outcome='attention', message=str(reason)[:500])
    return s.digest(component + '\0' + str(reason))


async def incident_async(component, reason, *, cause=None, path=None):
    return await call(incident, component, reason, cause=cause, path=path)


def status(conn):
    return {'execution_owner': 'Temporal', 'incidents': []}

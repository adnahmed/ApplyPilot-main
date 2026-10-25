"""Archive retired execution metadata before removing its schema.

Only explicit schema initialization calls these helpers. Archived records are
historical evidence; no runtime selector, owner or retry policy reads them.
Dropping without CASCADE deliberately fails on an unhandled external dependency.
"""
from __future__ import annotations

import re


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', value):
        raise ValueError('Invalid migration identifier')
    return '"' + value + '"'


def _archive_schema(conn) -> None:
    if not conn.in_transaction:
        raise RuntimeError('Execution retirement requires a schema transaction')
    conn.execute('''CREATE TABLE IF NOT EXISTS retired_execution_history (
        migration_id TEXT NOT NULL, source_table TEXT NOT NULL, row_identity TEXT NOT NULL,
        payload_json TEXT NOT NULL, retired_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(migration_id,source_table,row_identity))''')


def columns(conn, *, migration: str, table: str, key: tuple[str, ...], names: tuple[str, ...]) -> tuple[str, ...]:
    """Archive the removed columns and stable business key, then drop atomically."""
    table_sql = _identifier(table)
    existing = {row[1] for row in conn.table_info(table)}
    removed = tuple(name for name in names if name in existing)
    if not removed:
        return ()
    if not key or not set(key) <= existing or set(key) & set(removed):
        raise ValueError('A retained row identity is required')
    _archive_schema(conn)
    # Identifiers are validated above/below; values never become SQL identifiers.
    for name in (*key, *removed):
        _identifier(name)
    identity = 'jsonb_build_object(' + ','.join(f"'{name}',t.{_identifier(name)}" for name in key) + ')::text'
    payload = 'jsonb_build_object(' + ','.join(f"'{name}',t.{_identifier(name)}" for name in removed) + ')::text'
    present = ' OR '.join(f't.{_identifier(name)} IS NOT NULL' for name in removed)
    conn.execute(f'''INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
        SELECT ?,?,{identity},{payload} FROM {table_sql} t WHERE {present}
        ON CONFLICT(migration_id,source_table,row_identity) DO NOTHING''', (migration, table))
    for name in removed:
        conn.execute(f'ALTER TABLE {table_sql} DROP COLUMN {_identifier(name)}')
    return removed


def table(conn, *, migration: str, name: str, key: tuple[str, ...]) -> bool:
    """Archive every row of an obsolete execution table, retaining all evidence."""
    table_sql = _identifier(name)
    existing = {row[1] for row in conn.table_info(name)}
    if not existing:
        return False
    if not key or not set(key) <= existing:
        raise ValueError('A stable archival row identity is required')
    _archive_schema(conn)
    for column in key:
        _identifier(column)
    identity = 'jsonb_build_object(' + ','.join(f"'{column}',t.{_identifier(column)}" for column in key) + ')::text'
    conn.execute(f'''INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
        SELECT ?,?,{identity},to_jsonb(t)::text FROM {table_sql} t
        ON CONFLICT(migration_id,source_table,row_identity) DO NOTHING''', (migration, name))
    conn.execute(f'DROP TABLE {table_sql}')
    return True


def mailbox_v2(conn) -> None:
    columns(conn, migration='mailbox-temporal-v2', table='feedback_mailboxes', key=('mailbox_key',),
            names=('lease_token', 'lease_until', 'window_after', 'window_before', 'page_token',
                   'tail_after', 'tail_before', 'tail_page_token'))
    columns(conn, migration='mailbox-temporal-v2', table='feedback_messages', key=('message_key',),
            names=('next_retry_at',))
    # Provider failures are pending messages, not application-owned delayed tasks.
    conn.execute("UPDATE feedback_messages SET state='pending' WHERE state='retry'")
    row = conn.execute('SELECT config_json FROM feedback_settings WHERE id=1').fetchone()
    if row:
        import json
        value = json.loads(row[0])
        if not isinstance(value, dict):
            raise ValueError('Invalid persisted feedback configuration')
        removed = {name: value.pop(name) for name in ('max_llm_calls', 'sync_max_pages') if name in value}
        if removed:
            _archive_schema(conn)
            conn.execute('''INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
                VALUES(?,?,?,?) ON CONFLICT DO NOTHING''',
                ('mailbox-temporal-v2', 'feedback_settings', '1', json.dumps(removed, sort_keys=True)))
            conn.execute('UPDATE feedback_settings SET config_json=? WHERE id=1',
                         (json.dumps(value, sort_keys=True, separators=(',', ':')),))


def application_execution_v4(conn) -> None:
    if 'agent_id' in {row[1] for row in conn.table_info('jobs')}:
        _archive_schema(conn)
        conn.execute("INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json) "
                     "SELECT 'application-temporal-v4','jobs.agent_id',url,jsonb_build_object('agent_id',agent_id)::text "
                     "FROM jobs WHERE agent_id IS NOT NULL ON CONFLICT DO NOTHING")
        conn.execute("UPDATE jobs SET state='verification_required',apply_status='in_progress' "
                     "WHERE agent_id IS NOT NULL AND applied_at IS NULL")
        conn.execute("UPDATE jobs SET agent_id=NULL WHERE agent_id IS NOT NULL")
    columns(conn, migration='application-temporal-v4', table='jobs', key=('url',),
            names=('enrich_next_retry_at', 'score_next_retry_at', 'tailor_next_retry_at', 'cover_next_retry_at'))
    columns(conn, migration='application-temporal-v4', table='linkedin_governor_state', key=('singleton_id',),
            names=('lease_owner', 'lease_expires_at', 'cooldown_until'))
    conn.execute("UPDATE linkedin_governor_state SET manual_block_reason=NULL "
                 "WHERE manual_block_reason='Migrated provider restriction requires review'")
    table(conn, migration='application-temporal-v4', name='llm_admission_waits', key=('job_url', 'stage', 'scope'))


def discovery_execution_v4(conn) -> None:
    for name, key in (
        ('discovery_page_observations', ('page_key', 'identity_key')),
        ('discovery_pages', ('page_key',)),
        ('discovery_useful_yield', ('context_key', 'job_url')),
        ('discovery_tasks', ('task_id',)),
        ('discovery_allocation_reservations', ('context_key', 'lane', 'job_url')),
        ('discovery_allocation_batches', ('context_key', 'lane', 'batch_id')),
        ('discovery_allocations', ('context_key', 'lane')),
        ('discovery_scheduler_credit', ('context_key',)),
        ('discovery_cycle_costs', ('cycle_id', 'provider')),
    ):
        table(conn, migration='discovery-temporal-v4', name=name, key=key)


def integration_execution_v4(conn) -> None:
    for name, key in (
        ('unattended_tasks', ('name',)), ('unattended_research', ('case_id',)),
        ('unattended_incidents', ('key',)), ('referral_cycle_log', ('id',)),
    ):
        table(conn, migration='integrations-temporal-v4', name=name, key=key)
    columns(conn, migration='integrations-temporal-v4', table='career_ops_reports', key=('id',),
            names=('background_task_id', 'next_retry_at'))

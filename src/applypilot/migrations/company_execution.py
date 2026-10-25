"""Explicit, data-preserving retirement of company scheduling metadata."""
import json
from .retire_execution import columns, table, _archive_schema


def retire_company_execution(conn):
    migration = 'company-temporal-v4'
    for name, key, retired in (
        ('company_targets', ('id',), ('next_at',)),
        ('company_target_boards', ('target_id', 'board_slug'), ('cursor_json',)),
        ('company_sources', ('id',), ('next_at',)),
        ('company_predictions', ('website',), ('next_at',)),
    ):
        columns(conn, migration=migration, table=name, key=key, names=retired)
    table(conn, migration=migration, name='target_priority_credit', key=('lane',))
    for name, column, removed in (
        ('company_sources', 'evidence_json', ('next_page', 'catalog_offset', 'feed_cursor', 'allocation')),
        ('company_signals', 'detail_json', ('verification_retry_at',)),
    ):
        if not conn.table_info(name).fetchall():
            continue
        for row in conn.execute(f'SELECT id,{column} FROM {name}'):
            value = json.loads(row[column])
            archived = {key: value.pop(key) for key in removed if key in value}
            if not archived:
                continue
            _archive_schema(conn)
            conn.execute("""INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
                VALUES(?,?,?,?) ON CONFLICT DO NOTHING""",
                (migration, name + '.' + column, row['id'], json.dumps(archived, sort_keys=True)))
            conn.execute(f'UPDATE {name} SET {column}=? WHERE id=?',
                         (json.dumps(value, sort_keys=True, separators=(',', ':')), row['id']))

    if conn.table_info('company_discovery_settings').fetchall():
        row = conn.execute('SELECT config_json FROM company_discovery_settings WHERE id=1').fetchone()
        if row:
            value = json.loads(row['config_json'])
            archived = {key: value.pop(key) for key in ('sources_per_cycle', 'predict_companies_per_cycle') if key in value}
            if archived:
                _archive_schema(conn)
                conn.execute("""INSERT INTO retired_execution_history(migration_id,source_table,row_identity,payload_json)
                    VALUES(?,?,?,?) ON CONFLICT DO NOTHING""",
                    (migration, 'company_discovery_settings', '1', json.dumps(archived, sort_keys=True)))
                conn.execute('UPDATE company_discovery_settings SET config_json=? WHERE id=1',
                             (json.dumps(value, sort_keys=True, separators=(',', ':')),))

"""Preserve existing submission identities when upgrading reservation-era schemas."""


def migrate(conn):
    renames = {
        'application_attempts': {'reservation_id': 'attempt_id', 'reserved_at': 'prepared_at'},
        'application_email_deliveries': {'reservation_id': 'attempt_id'},
        'feedback_decisions': {'reservation_id': 'attempt_id'},
        'recovery_work': {'reservation_id': 'attempt_id'},
        'recovery_budget_credits': {'reservation_id': 'attempt_id'},
    }
    if not conn.in_transaction:
        raise RuntimeError('Application identity upgrade requires a schema transaction')
    for table, names in renames.items():
        columns = {row[1] for row in conn.table_info(table)}
        for old, new in names.items():
            if old in columns and new not in columns:
                conn.execute(f'ALTER TABLE {table} RENAME COLUMN {old} TO {new}')
    job_columns = {row[1] for row in conn.table_info('jobs')}
    if {'apply_task_id', 'application_attempt_id'} <= job_columns:
        conn.execute('UPDATE jobs j SET application_attempt_id=a.attempt_id '
                     'FROM application_attempts a WHERE j.application_attempt_id IS NULL '
                     'AND j.apply_task_id=a.attempt_id AND j.url=a.job_url')

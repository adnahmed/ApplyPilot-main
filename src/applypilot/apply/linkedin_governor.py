"""Read/clear an existing LinkedIn account hold; execution is owned by Temporal.

This compatibility module has no leases, counters deciding admission, automatic
cooldowns or signal taxonomy. Historical submission events are retained.
"""
from datetime import UTC, datetime
from applypilot.database import get_connection


def status(conn=None, now=None) -> dict:
    conn = conn or get_connection()
    row = conn.execute('SELECT manual_block_reason FROM linkedin_governor_state WHERE singleton_id=1').fetchone()
    reason = row['manual_block_reason'] if row else None
    return {'easy_apply_allowed': not bool(reason), 'manual_block_reason': reason,
            'execution_owner': 'Temporal', 'pacing': 'Dedicated LinkedIn Activity task queue'}


def clear_manual_block(conn=None) -> None:
    conn = conn or get_connection()
    conn.execute('UPDATE linkedin_governor_state SET manual_block_reason=NULL,updated_at=? WHERE singleton_id=1',
                 (datetime.now(UTC).isoformat(),))
    conn.commit()

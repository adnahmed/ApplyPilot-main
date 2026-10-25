"""One source-attributed interview brief; Temporal owns retry and cancellation."""
from __future__ import annotations

from applypilot.orchestration.contracts import digest


def generate_one(job_url: str, job_id: str, *, report_id=None) -> str:
    from applypilot.database import get_connection, close_connection
    from applypilot.discovery.repository import transaction
    from applypilot.execution import check_cancelled
    from .invitations import source_for_job
    from . import store, context, brief
    conn = get_connection()
    if report_id:
        existing = conn.execute("SELECT * FROM career_ops_reports WHERE id=? AND job_url=? AND mode='interview_brief'",
                                (report_id, job_url)).fetchone()
        if existing is None:
            raise ValueError('Interview report identity does not match this opportunity')
    else:
        existing = conn.execute("SELECT * FROM career_ops_reports WHERE job_url=? AND mode='interview_brief' "
            "AND state IN ('pending','error') ORDER BY created_at,id LIMIT 1", (job_url,)).fetchone()
    import hashlib
    import json
    if existing:
        source = json.loads(existing['source_json'] or '{}')
        data = json.loads(existing['input_json'] or '{}')
        data['job_url'] = job_url
        source = source or {'id': 'saved-report:' + existing['id'], 'channel': 'saved brief request', 'text': ''}
        trigger = existing['trigger_key']
        report_id = existing['id']
    else:
        source = source_for_job(conn, job_url)
        trigger = hashlib.sha256(store.dump([job_url, source['id'], 'interview_brief']).encode()).hexdigest()
        existing = conn.execute("SELECT * FROM career_ops_reports WHERE trigger_key=?", (trigger,)).fetchone()
        report_id = existing['id'] if existing else digest({'job': job_id, 'invitation': source['id']})
        data = {'job_url': job_url}
    if existing and existing['state'] in {'complete', 'cancelled'}:
        return 'complete' if existing['state'] == 'complete' else 'manual_review'
    payload = context.build(conn, 'interview_brief', data, invitation=source)
    stamp = store.now()
    with transaction(conn):
        conn.execute("""INSERT INTO career_ops_reports
            (id,mode,job_url,state,input_json,source_json,trigger_key,created_at,updated_at)
            VALUES(?,'interview_brief',?,'pending',?,?,?,?,?) ON CONFLICT(id) DO UPDATE
            SET state='pending',attempts=career_ops_reports.attempts+1,error='',updated_at=excluded.updated_at
            WHERE career_ops_reports.state NOT IN ('complete','cancelled')""",
            (report_id, job_url, store.dump(data), store.dump(source), trigger, stamp, stamp))
        row = conn.execute('SELECT state,attempts FROM career_ops_reports WHERE id=?', (report_id,)).fetchone()
        if row['state'] != 'pending':
            return 'complete' if row['state'] == 'complete' else 'manual_review'
        generation = row['attempts']
        conn.execute('UPDATE jobs SET brief_attempts=COALESCE(brief_attempts,0)+1 WHERE url=?', (job_url,))
    try:
        result = brief.prepare(conn, data, payload, report_id + ':' + str(generation))
        check_cancelled()
        if not result.get('artifact'):
            raise RuntimeError('Interview brief did not produce an artifact')
        with transaction(conn):
            current = conn.execute('SELECT state,attempts FROM career_ops_reports WHERE id=? FOR UPDATE',
                                   (report_id,)).fetchone()
            if current['state'] != 'pending' or current['attempts'] != generation:
                return 'superseded'
            conn.execute("UPDATE career_ops_reports SET state='complete',result_json=?,error='',updated_at=? WHERE id=?",
                         (store.dump(result), store.now(), report_id))
            # Newer invitations will have their own stable report; never make an
            # old generation overwrite a newer published brief.
            conn.execute("UPDATE jobs SET interview_brief_path=?,brief_generated_at=? WHERE url=? "
                         "AND (brief_generated_at IS NULL OR brief_generated_at<=?)",
                         (result['artifact'], stamp, job_url, stamp))
        return 'complete'
    except BaseException as exc:
        with transaction(conn):
            conn.execute("UPDATE career_ops_reports SET state='error',error=?,updated_at=? "
                         "WHERE id=? AND attempts=? AND state='pending'",
                         (type(exc).__name__, store.now(), report_id, generation))
        raise
    finally:
        close_connection()

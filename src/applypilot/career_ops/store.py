"""Durable career reports, practice sessions, and source-attributed story drafts."""
from __future__ import annotations

import json
import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from applypilot.discovery.repository import transaction

MODES = {
    'interview_brief': 'Interview brief',
    'evaluate': 'A–H evaluation', 'interview_plan': 'Interview preparation plan',
    'interview_practice': 'Practice interview', 'interview_debrief': 'Interview debrief',
    'negotiate': 'Negotiation scripts', 'offer_review': 'Offer reading companion',
    'patterns': 'Rejection patterns and learning plan',
}


def now():
    return datetime.now(UTC).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def ensure_schema(conn):
    for sql in (
        """CREATE TABLE IF NOT EXISTS career_ops_reports (
            id TEXT PRIMARY KEY, mode TEXT NOT NULL, job_url TEXT NOT NULL DEFAULT '',
            state TEXT NOT NULL, input_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '', session_revision INTEGER,
            attempts INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS career_ops_sessions (
            id TEXT PRIMARY KEY, job_url TEXT NOT NULL, turns_json TEXT NOT NULL DEFAULT '[]',
            context_json TEXT NOT NULL DEFAULT '{}',
            next_question TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS career_ops_stories (
            id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES career_ops_reports(id) ON DELETE CASCADE,
            job_url TEXT NOT NULL, story_json TEXT NOT NULL, created_at TEXT NOT NULL)""",
        'CREATE INDEX IF NOT EXISTS idx_career_ops_job ON career_ops_reports(job_url,created_at)',
    ):
        conn.execute(sql)
    for table, additions in {
        'career_ops_reports': {'session_revision': 'INTEGER',
                               'trigger_key': 'TEXT', 'source_json': "TEXT NOT NULL DEFAULT '{}'"},
        'career_ops_sessions': {'context_json': "TEXT NOT NULL DEFAULT '{}'"},
    }.items():
        existing = {row[1] for row in conn.table_info(table)}
        for name, definition in additions.items():
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_career_ops_trigger ON career_ops_reports(trigger_key)')
    conn.execute("""INSERT INTO career_ops_reports
        (id,mode,job_url,state,input_json,created_at,updated_at,trigger_key,source_json,result_json)
        SELECT 'legacy-brief-'||md5(url),'interview_brief',url,'complete',
        json_build_object('job_url',url)::text,COALESCE(brief_generated_at,?),COALESCE(brief_generated_at,?),
        'legacy-brief:'||md5(url),'{"channel":"existing brief"}',
        json_build_object('artifact',interview_brief_path)::text FROM jobs
        WHERE NULLIF(interview_brief_path,'') IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM career_ops_reports r WHERE r.job_url=jobs.url AND r.mode='interview_brief')
        ON CONFLICT DO NOTHING""", (now(), now()))
    conn.execute("""UPDATE career_ops_reports r SET result_json=json_build_object('artifact',j.interview_brief_path)::text
        FROM jobs j WHERE r.job_url=j.url AND r.id LIKE 'legacy-brief-%' AND r.result_json='{}'
        AND r.updated_at=j.brief_generated_at AND NULLIF(j.interview_brief_path,'') IS NOT NULL""")


def _report(row):
    result = dict(row)
    result['result'] = json.loads(result.pop('result_json'))
    result['input'] = json.loads(result.pop('input_json'))
    result['source'] = json.loads(result.pop('source_json', '{}'))
    return result


def queue_invitation(conn, job_url, source):
    """One durable workflow row per invitation; no separate preparation queue."""
    automatic = {'interview_brief', 'evaluate', 'interview_plan', 'interview_practice'}
    stamp = now()
    with transaction(conn, resource='career-ops'):
        job = conn.execute('SELECT description,full_description FROM jobs WHERE url=?', (job_url,)).fetchone()
        described = bool(job and (job['description'] or job['full_description']))
        for mode in MODES:
            state = 'pending' if mode in automatic and (mode == 'interview_brief' or described) else 'needs_input'
            key = hashlib.sha256(dump([job_url, source['id'], mode]).encode()).hexdigest()
            conn.execute("""INSERT INTO career_ops_reports
                (id,mode,job_url,state,input_json,source_json,trigger_key,attempts,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,0,?,?) ON CONFLICT(trigger_key) DO UPDATE SET state='pending',updated_at=excluded.updated_at
                WHERE career_ops_reports.state='needs_input' AND excluded.state='pending'""",
                (uuid4().hex, mode, job_url, state,
                 dump({'job_url': job_url}), dump(source), key, stamp, stamp))


def get(conn, report_id):
    row = conn.execute('SELECT * FROM career_ops_reports WHERE id=?', (report_id,)).fetchone()
    if row is None:
        raise ValueError('Career report not found')
    return _report(row)


def start(conn, mode, data, *, session_revision=None, report_id=None):
    """Create an artifact identity; Temporal owns execution and deduplication."""
    stamp = now()
    report_id = report_id or data.get('retry_report_id') or uuid4().hex
    with transaction(conn, resource='career-ops'):
        row = conn.execute('SELECT * FROM career_ops_reports WHERE id=? FOR UPDATE', (report_id,)).fetchone()
        if row:
            prior = _report(row)
            if prior['mode'] != mode:
                raise ValueError('Report identity belongs to another mode')
            if prior['state'] == 'complete':
                return prior
            if prior['state'] == 'cancelled':
                raise ValueError('Cancelled report requires a new request')
            conn.execute("UPDATE career_ops_reports SET state='pending',error='',updated_at=? WHERE id=?",
                         (stamp, report_id))
        else:
            conn.execute("""INSERT INTO career_ops_reports
                (id,mode,job_url,state,input_json,session_id,session_revision,created_at,updated_at)
                VALUES(?,?,?,'pending',?,?,?,?,?)""",
                (report_id, mode, data.get('job_url', ''), dump(data), data.get('session_id', ''),
                 session_revision, stamp, stamp))
    return get(conn, report_id)


def session(conn, session_id):
    row = conn.execute('SELECT * FROM career_ops_sessions WHERE id=?', (session_id,)).fetchone()
    if row is None:
        raise ValueError('Practice session not found')
    value = dict(row)
    value['turns'] = json.loads(value.pop('turns_json'))
    value['context'] = json.loads(value.pop('context_json'))
    return value


def finish(conn, report, result, *, practice=None, answer=''):
    with transaction(conn, resource='career-ops'):
        current = _report(conn.execute('SELECT * FROM career_ops_reports WHERE id=? FOR UPDATE', (report['id'],)).fetchone())
        if current['state'] == 'complete':
            return current
        if current['state'] == 'cancelled':
            raise ValueError('This report was cancelled')
        session_id = report['session_id']
        if report['mode'] == 'interview_practice':
            stamp = now()
            if practice:
                turns = [*practice['turns'], {'question': practice['next_question'], 'answer': answer,
                          'feedback': result['feedback'], 'report_id': report['id'], 'answered_at': stamp,
                          'effort_minutes': report['input'].get('effort_minutes')}]
                updated = conn.execute("""UPDATE career_ops_sessions SET turns_json=?,next_question=?,
                    revision=revision+1,updated_at=? WHERE id=? AND revision=?""",
                    (dump(turns), result['next_question'], stamp, practice['id'], practice['revision']))
                if updated.rowcount != 1:
                    raise ValueError('Practice session changed while generating feedback; reload the latest question')
                session_id = practice['id']
            else:
                session_id = uuid4().hex
                request = {key: report['input'][key] for key in
                           ('job_url', 'job_description', 'company_name', 'instructions') if key in report['input']}
                if report['source'].get('practice_lane'):
                    request['practice_lane'] = report['source']['practice_lane']
                conn.execute("""INSERT INTO career_ops_sessions
                    (id,job_url,next_question,context_json,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                    (session_id, report['job_url'], result['next_question'], dump(request), stamp, stamp))
            result['session_id'] = session_id
        for index, story in enumerate(result.get('stories', [])):
            conn.execute("""INSERT INTO career_ops_stories(id,report_id,job_url,story_json,created_at)
                VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET story_json=excluded.story_json""",
                (report['id'] + ':' + str(index), report['id'], report['job_url'], dump(story), now()))
        conn.execute("""UPDATE career_ops_reports SET result_json=?,state='complete',error='',session_id=?,
            updated_at=? WHERE id=?""", (dump(result), session_id, now(), report['id']))
        if report['mode'] == 'interview_brief' and result.get('artifact'):
            conn.execute("""UPDATE jobs SET interview_brief_path=?,brief_generated_at=? WHERE url=?""",
                (result['artifact'], report['updated_at'], report['job_url']))
    return get(conn, report['id'])


def fail(conn, report, exc, *, cancelled=False, retryable=True):
    """Record artifact failure; no database retry clock or execution ownership."""
    with transaction(conn, resource='career-ops'):
        conn.execute("""UPDATE career_ops_reports SET state=?,error=?,updated_at=?
            WHERE id=? AND state NOT IN ('complete','cancelled')""",
            ('cancelled' if cancelled else 'error', str(exc)[:2000], now(), report['id']))
    return get(conn, report['id'])


def cancel(conn, report_id):
    with transaction(conn, resource='career-ops'):
        get(conn, report_id)
        conn.execute("""UPDATE career_ops_reports SET state='cancelled',error='Cancelled by user',
            updated_at=? WHERE id=? AND state!='complete'""", (now(), report_id))
    return get(conn, report_id)


def overview(conn, job_url=None, report_id=None, state=None):
    where, params = (' WHERE job_url=?', (job_url,)) if job_url else ('', ())
    filters, report_params = (['r.job_url=?'], [job_url]) if job_url else ([], [])
    if state:
        if state not in {'pending','running','complete','error','needs_input','cancelled'}:
            raise ValueError('Choose a report status')
        filters.append('r.state=?')
        report_params.append(state)
    reports = [dict(row) for row in conn.execute("""SELECT r.id,r.mode,r.job_url,r.state,r.updated_at,
        r.error,r.source_json,j.title,j.company FROM career_ops_reports r
        LEFT JOIN jobs j ON j.url=r.job_url""" + (' WHERE ' + ' AND '.join(filters) if filters else '')
        + ' ORDER BY r.updated_at DESC,r.id DESC LIMIT 100', tuple(report_params))]
    for report in reports:
        report['source'] = json.loads(report.pop('source_json'))
    active = get(conn, report_id) if report_id else None
    if active and job_url and active['job_url'] != job_url:
        raise ValueError('This report belongs to a different opportunity')
    sessions = [dict(row) for row in conn.execute(
        'SELECT id,job_url,next_question,revision,created_at,updated_at FROM career_ops_sessions'
        + where + ' ORDER BY updated_at DESC LIMIT 20', params)]
    stories = [{'id': row['id'], 'job_url': row['job_url'], 'story': json.loads(row['story_json'])}
               for row in conn.execute('SELECT * FROM career_ops_stories' + where
                                       + ' ORDER BY created_at DESC LIMIT 30', params)]
    return {'reports': reports, 'active_report': active, 'sessions': sessions, 'stories': stories, 'modes': MODES}

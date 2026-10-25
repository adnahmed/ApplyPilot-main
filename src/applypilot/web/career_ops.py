"""Dashboard access to user-requested career reports and practice sessions."""
from __future__ import annotations


def opportunities(api, query='', job_url=''):
    from .data import like, tables, text
    query, job_url = text(query, 200), text(job_url, 4000)
    with api.store.read() as conn:
        if conn is None or 'jobs' not in tables(conn):
            return {'items': [], 'more': False, 'selected': None}
        columns = ('url,title,company,location,interview_brief_path,'
                   "NULLIF(TRIM(COALESCE(NULLIF(full_description,''),description)),'') IS NOT NULL AS has_description")
        where = ' WHERE title ILIKE ? OR company ILIKE ? OR url ILIKE ?' if query else ''
        params = (like(query),) * 3 if query else ()
        rows = [dict(row) for row in conn.execute('SELECT ' + columns + ' FROM jobs' + where
                + ' ORDER BY discovered_at DESC NULLS LAST,url LIMIT 41', params)]
        more, rows = len(rows) > 40, rows[:40]
        selected = next((row for row in rows if row['url'] == job_url), None)
        if job_url and selected is None:
            row = conn.execute('SELECT ' + columns + ' FROM jobs WHERE url=?', (job_url,)).fetchone()
            if row is None:
                raise ValueError('This saved opportunity no longer exists; choose another one')
            selected = dict(row)
            rows.insert(0, selected)
        for row in rows:
            row['has_brief'] = bool(row.pop('interview_brief_path'))
            row['label'] = ' · '.join(str(row[key]) for key in ('title', 'company', 'location') if row.get(key)) or row['url']
        return {'items': rows, 'more': more, 'selected': selected}


def overview(api, job_url=None, report_id=None, state=None):
    from applypilot import config
    from applypilot.career_ops.context import profile_pay
    from applypilot.career_ops import store
    from .data import tables
    context = opportunities(api, job_url=job_url or '')
    profile = config.load_profile() if config.PROFILE_PATH.exists() else {}
    context['profile_pay'] = profile_pay(profile)
    with api.store.read() as conn:
        if conn is None or 'career_ops_reports' not in tables(conn):
            return {'reports': [], 'sessions': [], 'stories': [], 'modes': store.MODES, **context}
        result = {**store.overview(conn, job_url=job_url or None, report_id=report_id, state=state), **context}
    report = result.get('active_report')
    if report and report['mode'] == 'interview_brief' and report['state'] == 'complete' and not report['result'].get('brief'):
        import json
        try:
            path = api.store.document(report['job_url'], 'brief', report['id'])
            if path.stat().st_size > 2_000_000:
                raise ValueError('Saved interview brief exceeds the supported display size')
            saved = json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(saved, dict):
                raise ValueError('Saved interview brief must contain an object')
            report['result']['brief'] = saved.get('brief', saved)
        except (OSError, ValueError, LookupError) as exc:
            report['display_error'] = 'The saved brief could not be read: ' + str(exc)
    return result


def launch(api, data):
    from applypilot.career_ops.store import MODES
    if data.get('mode') not in MODES:
        raise ValueError('Choose a career workflow')
    import asyncio
    from applypilot.orchestration.integration_client import submit_report
    return asyncio.run(submit_report(data['mode'], data, root=api.app_dir))

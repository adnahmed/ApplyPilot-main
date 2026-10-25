"""Owner views and actions using the existing opportunity records."""
from __future__ import annotations

from .data import integer, tables, text


def read(api, query):
    from applypilot.opportunities import next_actions, opportunity_view
    with api.store.read() as conn:
        if conn is None or 'feedback_reviews' not in tables(conn):
            return {'actions': [], 'opportunity': None, 'needs_initialization': True}
        job_url = text(query.get('job_url'), 4000)
        case = opportunity_view(conn, job_url) if job_url else None
        if case:
            from applypilot.career_ops.forwardable import build_brief
            try:
                case['brief'] = build_brief(conn, job_url)
            except (OSError, ValueError) as exc:
                case['brief'] = {'markdown': '', 'introduction': '', 'ready': False,
                                 'missing_materials': ['The brief could not be prepared: ' + str(exc)]}
        return {'actions': [] if case else next_actions(conn, limit=integer(query.get('limit'), 20, 1, 100)),
                'opportunity': case}


def strategies(api):
    from applypilot.tracking.pursuit import report
    with api.store.read() as conn:
        if conn is None or 'feedback_submissions' not in tables(conn):
            return {'cohorts': [], 'sequences': [], 'needs_initialization': True}
        return report(conn)


def brief(api, query):
    from applypilot.career_ops.forwardable import build_brief, read_brief
    with api.store.read() as conn:
        if conn is None:
            raise ValueError('The opportunity database is unavailable')
        if query.get('report_id'):
            return read_brief(conn, text(query.get('report_id'), 150))
        return build_brief(conn, text(query.get('job_url'), 4000))


def write(api, route, data):
    with api.write() as conn:
        if route == 'opportunities/complete':
            from applypilot.opportunities import complete_action
            return complete_action(conn, data.get('action_id'), note=data.get('note', ''),
                                   effort_minutes=data.get('effort_minutes'))
        if route == 'opportunities/response':
            from applypilot.apply.application_contract import record_response
            if not data.get('revision'):
                raise ValueError('Reload the opportunity before saving its response')
            result = record_response(conn, data.get('job_url'), data.get('requirement_id'), data.get('text', ''),
                                     proof_links=data.get('proof_links') or [],
                                     evidence_ids=data.get('evidence_ids') or [], expected_revision=data['revision'])
            return {'state': 'complete', 'message': 'Application response saved.', 'contract': result}
        if route == 'opportunities/brief':
            from applypilot.career_ops.forwardable import save_brief
            result = save_brief(conn, data.get('job_url'), introduction=data.get('introduction', ''))
            from urllib.parse import urlencode
            return {'state': 'complete', 'message': 'Forwardable brief saved.', 'brief': result,
                    'download_url': '/api/opportunities/brief.md?' + urlencode({'report_id': result['id']}),
                    'download_label': 'Download this forwardable brief'}
    raise ValueError('Unknown opportunity action')

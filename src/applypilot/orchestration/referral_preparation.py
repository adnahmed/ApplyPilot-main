"""Referral selection, draft preparation, revision and result business operations."""
from __future__ import annotations

from dataclasses import replace

from .contracts import digest
from . import store


def perform(request, value):
    from applypilot.database import get_connection
    from applypilot.config import load_profile
    from applypilot.referrals import planner, store as referrals, outreach
    from .utility_domain import receipt_key

    conn = get_connection()
    if request.phase == 'referral_inventory':
        key = digest(['referral-selection', request.reference])
        with referrals.transaction(conn):
            conn.advisory_lock(key)
            prior = store.receipt(conn, key)
            if prior is not None:
                return {}, prior, 'inventoried'
            if value['operation'] == 'referral_prepare':
                cases, result = planner.preparation_cases(conn, limit=value['payload'].get('limit'))
            else:
                result = {'errors': [], 'drafted': 0, 'held': 0, 'sent': 0}
                cases = planner.plan_jobs(conn, limit=value['payload'].get('limit'), errors=result['errors'])
                result['planned'] = len(cases)
            reference = store.put(conn, {**value, 'cases': [case['id'] for case in cases],
                'selection_result': result, 'profile': load_profile()})
            detail = {'selection_reference': reference}
            store.save_receipt(conn, key, detail)
        return {}, detail, 'inventoried'
    if request.phase == 'referral_case_prepare':
        if request.sequence >= len(value['cases']):
            return {}, {}, 'complete'
        key = digest(['referral-draft', request.reference, request.sequence])
        with referrals.transaction(conn):
            conn.advisory_lock(key)
            prior = store.receipt(conn, key)
            if prior is not None:
                return prior, {}, 'prepared'
            case = referrals.require_case(conn, value['cases'][request.sequence])
            drafts = planner.create_initial_drafts(conn, [case], value['profile'], improve=False)
            held = int(planner.maybe_hold(conn, case['id']) is not None) if value['operation'] == 'referral_prepare' else 0
            result = {'case_id': case['id'], 'draft_ids': [draft['id'] for draft in drafts], 'held': held}
            store.save_receipt(conn, key, result)
        return result, {}, 'prepared'
    if request.phase == 'referral_case_revise':
        prior = store.receipt(conn, digest(['referral-draft', request.reference, request.sequence]))
        if prior is None:
            raise ValueError('Referral draft preparation evidence is missing')
        from applypilot.integrations.referrals import improve_draft
        for outbox_id in prior['draft_ids']:
            draft = outreach._outbox(conn, outbox_id)
            if draft['state'] == 'draft':
                improve_draft(conn, draft, value['profile'], operation_id=digest([receipt_key(request), outbox_id]))
        if value['operation'] == 'referral_prepare':
            planner.prepared_case(conn, prior['case_id'])
        return prior, {}, 'prepared'
    results = []
    for index in range(len(value['cases'])):
        response = store.receipt(conn, receipt_key(replace(request, phase='referral_case_revise', sequence=index)))
        if response is None:
            raise ValueError('Referral case has not completed its model revision')
        results.append(store.get(conn, response['detail']['reference'])['result'])
    cases = [referrals.require_case(conn, case_id) for case_id in value['cases']]
    result = dict(value['selection_result'])
    drafts = [outbox_id for row in results for outbox_id in row['draft_ids']]
    if value['operation'] == 'referral_plan':
        result = {'cases': [dict(case, plan_json=referrals.obj(case['plan_json'])) for case in cases],
            'draft_ids': drafts, 'errors': result['errors'], 'status': 'partial' if result['errors'] else 'complete', 'sent': 0}
    else:
        result.update(drafted=len(drafts), held=sum(row['held'] for row in results))
        result = planner.preparation_result(conn, cases, result)
    return result, {}, 'complete'

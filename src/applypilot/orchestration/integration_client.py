"""Submit concrete business identities to native integration Workflows."""
from __future__ import annotations

import asyncio
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from .contracts import Command, identity
from .connection import connect
from .settings import load
from . import store


async def submit_report(mode, data, *, root=None):
    from applypilot.career_ops import service, store as reports
    from .integration_workflows import ReportWorkflow
    policy = load(root)

    def capture():
        from applypilot.database import get_connection, close_connection
        from applypilot.referrals.store import transaction
        try:
            conn = get_connection()
            inputs = service._inputs(mode, data)
            report_id = inputs.get('retry_report_id')
            if report_id:
                previous = reports.get(conn, report_id)
                if previous['mode'] != mode:
                    raise ValueError('Report mode changed')
                if previous['state'] == 'complete':
                    return report_id, None
                inputs = {**previous['input'], **inputs}
                with transaction(conn):
                    conn.execute("UPDATE career_ops_reports SET state='pending',error='',input_json=? WHERE id=?",
                                 (reports.dump(inputs), report_id))
            report = reports.start(conn, mode, inputs, report_id=report_id,
                                   session_revision=inputs.get('session_revision'))
            snapshot = store.put(conn, store.snapshot_options({}))
            reference = store.put(conn, {'snapshot': snapshot, 'report_id': report['id']})
            conn.commit()
            return report['id'], reference
        finally:
            close_connection()

    report_id, reference = await asyncio.to_thread(capture)
    if reference is None:
        return {'report_id': report_id, 'state': 'complete'}
    client = await connect(root)
    handle = await client.start_workflow(ReportWorkflow.run,
        Command(reference, policy.model_dump(mode='json'), 'report'),
        id=identity(policy.workspace, 'report', report_id), task_queue=policy.queue('workflow'),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    return {'report_id': report_id, 'workflow_id': handle.id, 'state': 'accepted'}


async def cancel_report(report_id, *, root=None):
    from applypilot.database import get_connection, close_connection
    from applypilot.career_ops import store as reports
    policy = load(root)
    client = await connect(root)
    await client.get_workflow_handle(identity(policy.workspace, 'report', report_id)).cancel()
    def persist():
        try:
            return reports.cancel(get_connection(), report_id)
        finally:
            close_connection()
    await asyncio.to_thread(persist)
    return {'report_id': report_id, 'state': 'cancellation_requested'}


async def submit_relationship(case_id, *, root=None):
    from .integration_workflows import NetworkingRelationshipWorkflow
    from applypilot.database import get_connection, close_connection
    from applypilot.referrals.store import require_case
    policy = load(root)
    def capture():
        try:
            conn = get_connection()
            require_case(conn, case_id)
            snapshot = store.put(conn, store.snapshot_options({}))
            reference = store.put(conn, {'snapshot': snapshot, 'case_id': case_id})
            conn.commit()
            return reference
        finally:
            close_connection()
    reference = await asyncio.to_thread(capture)
    client = await connect(root)
    handle = await client.start_workflow(NetworkingRelationshipWorkflow.run,
        Command(reference, policy.model_dump(mode='json'), 'relationship'),
        id=identity(policy.workspace, 'relationship', case_id), task_queue=policy.queue('workflow'),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    return {'case_id': case_id, 'workflow_id': handle.id, 'state': 'accepted'}


async def submit_professional_action(operation_id, *, root=None):
    from .integration_workflows import ProfessionalActionWorkflow
    from applypilot.database import get_connection, close_connection
    policy = load(root)
    def capture():
        try:
            conn = get_connection()
            row = conn.execute('SELECT state FROM professional_actions WHERE operation_id=?', (operation_id,)).fetchone()
            if row is None or row['state'] != 'approved':
                raise ValueError('Choose an exactly approved professional action')
            snapshot = store.put(conn, store.snapshot_options({}))
            reference = store.put(conn, {'snapshot': snapshot, 'operation_id': operation_id})
            conn.commit()
            return reference
        finally:
            close_connection()
    reference = await asyncio.to_thread(capture)
    client = await connect(root)
    handle = await client.start_workflow(ProfessionalActionWorkflow.run,
        Command(reference, policy.model_dump(mode='json'), 'professional_action'),
        id=identity(policy.workspace, 'professional-action', operation_id), task_queue=policy.queue('workflow'),
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING)
    return {'operation_id': operation_id, 'workflow_id': handle.id, 'state': 'accepted'}

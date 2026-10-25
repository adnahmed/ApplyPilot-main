"""Native clients for explicit profile and evidence requests."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import urlsplit
import uuid

from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from .contracts import Command, Observation, digest, identity
from .connection import connect
from .settings import load
from .utility_contracts import OPERATIONS


def _capture(operation, payload, request_id):
    from applypilot.database import get_connection, close_connection
    from . import store
    try:
        conn = get_connection()
        reference = store.put(conn, {'operation': operation, 'payload': payload, 'request_id': request_id})
        conn.commit()
        return reference
    finally:
        close_connection()


def private_result(reference):
    from applypilot.database import get_connection, close_connection
    from . import store
    try:
        value = store.get(get_connection(), reference)
        return value['result']
    finally:
        close_connection()


async def submit_utility(operation, payload, *, root=None, wait=False, request_id=None):
    from .utility_workflows import UtilityWorkflow
    if operation not in OPERATIONS or not isinstance(payload, dict):
        raise ValueError('Choose a supported profile or evidence operation')
    if operation in {'referral_plan', 'referral_prepare'} and payload.get('limit') is not None:
        if type(payload['limit']) is not int or not 0 <= payload['limit'] <= 100:
            raise ValueError('Choose a referral result count from 0 to 100')
    if operation == 'referral_discovery':
        from applypilot.referrals.discovery import PROVIDERS
        if payload.get('provider') not in PROVIDERS or type(payload.get('limit')) is not int or not 1 <= payload['limit'] <= 20:
            raise ValueError('Choose a referral provider and result count from 1 to 20')
        if payload['provider'] != 'web' and payload.get('authorized') is not True:
            raise ValueError('Authorize use of the configured social account')
    if operation == 'github_person' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', str(payload.get('username', ''))):
        raise ValueError('Invalid GitHub handle')
    original_operation = operation
    if operation == 'material_url' and urlsplit(str(payload.get('url') or '')).hostname in {'github.com', 'www.github.com'}:
        operation = 'github'
        payload = {**payload, 'identity': payload['url'], 'limit': payload.get('limit', 3)}
    policy = load(root)
    request_id = request_id or uuid.uuid4().hex
    reference = await asyncio.to_thread(_capture, operation, payload, request_id)
    workflow_id = identity(policy.workspace, 'utility', reference)
    client = await connect(root)
    from temporalio.exceptions import WorkflowAlreadyStartedError
    try:
        handle = await client.start_workflow(UtilityWorkflow.run,
            Command(reference, policy.model_dump(mode='json'), operation,
                    cursor=payload.get('provider', 'web') if operation == 'referral_discovery' else ''), id=workflow_id,
            task_queue=policy.queue('workflow'), id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            memo={'operation': original_operation, 'input_reference': reference})
    except WorkflowAlreadyStartedError:
        handle = client.get_workflow_handle(workflow_id, result_type=Observation)
    if wait:
        result = await handle.result()
        return await asyncio.to_thread(private_result, result.detail['reference'])
    return {'task_id': workflow_id, 'workflow_id': workflow_id, 'run_id': handle.result_run_id,
            'operation': original_operation, 'state': 'queued', 'accepted': True,
            'message': 'Accepted by Temporal', 'execution_owner': 'Temporal'}


def run_utility(operation, payload, *, root=None):
    return asyncio.run(submit_utility(operation, payload, root=root, wait=True))


async def _status(client, execution):
    memo = await execution.memo()
    state = execution.status.name.lower() if execution.status else 'unknown'
    state = {'completed': 'completed', 'canceled': 'cancelled', 'terminated': 'cancelled'}.get(state, state)
    row = {'id': execution.id, 'task_id': execution.id, 'workflow_id': execution.id,
           'operation': memo.get('operation', 'utility'), 'state': state,
           'message': 'Temporal operation ' + state, 'started_at': execution.start_time.isoformat(),
           'finished_at': execution.close_time.isoformat() if execution.close_time else None,
           'execution_owner': 'Temporal'}
    if state == 'completed':
        result = await client.get_workflow_handle(execution.id, run_id=execution.run_id, result_type=Observation).result()
        row['result'] = await asyncio.to_thread(private_result, result.detail['reference'])
    return row


async def get_utility_status(workflow_id, *, root=None):
    policy = load(root)
    if not workflow_id.startswith('ap:' + policy.workspace + ':utility:'):
        raise ValueError('Utility Workflow is outside this workspace')
    client = await connect(root)
    execution = await client.get_workflow_handle(workflow_id).describe()
    return await _status(client, execution)


async def utility_status(*, root=None):
    policy = load(root)
    client = await connect(root)
    rows = []
    prefix = 'ap:' + policy.workspace + ':utility:'
    async for execution in client.list_workflows(query='WorkflowType = "applypilot.utility.v1" AND WorkflowId STARTS_WITH "' + prefix + '"', limit=20):
        if execution.status and execution.status.name == 'CONTINUED_AS_NEW':
            continue
        if execution.id.startswith(prefix):
            rows.append(await _status(client, execution))
        if len(rows) >= 20:
            break
    return rows

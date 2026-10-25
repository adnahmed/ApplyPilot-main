"""Account submission history and native Temporal signal-with-start."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from .contracts import (AccountSubmission, ApplicationAccountCommand, ApplicationCommand,
                        Observation, identity, task_queue)


def execute(request):
    if request.phase == 'application_account_history':
        from applypilot.database import get_connection
        import json
        after = datetime.fromtimestamp(request.options['observed_after'], UTC).isoformat()
        rows = get_connection().execute(
            "SELECT occurred_at AS stamp FROM linkedin_activity l "
            "WHERE event_type='confirmed_submission' AND occurred_at>=? AND NOT EXISTS "
            "(SELECT 1 FROM application_attempts a WHERE a.job_url=l.job_url AND a.channel='linkedin-easy-apply' "
            "AND COALESCE(a.submitted_at,a.side_effect_intent_at)>=?) "
            "UNION ALL SELECT COALESCE(submitted_at,side_effect_intent_at) AS stamp FROM application_attempts "
            "WHERE channel='linkedin-easy-apply' AND COALESCE(submitted_at,side_effect_intent_at)>=?",
            (after, after, after)).fetchall()
        stamps = []
        for row in rows:
            stamp = datetime.fromisoformat(row['stamp'])
            stamps.append((stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp())
        restrictions = get_connection().execute(
            "SELECT payload_json FROM retired_execution_history WHERE migration_id='application-temporal-v4' "
            "AND source_table='linkedin_governor_state'").fetchall()
        available_at = 0
        for row in restrictions:
            until = json.loads(row['payload_json']).get('cooldown_until')
            if until:
                stamp = datetime.fromisoformat(until)
                available_at = max(available_at, (stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)).timestamp())
        from applypilot.apply.provider_observations import account_observations
        observed = account_observations(get_connection(), request.options.get('account_workflow_id', ''))
        return Observation('complete', {'submissions': sorted(stamps), 'provider_available_at': available_at,
                                        'provider_observations': observed})
    command = ApplicationCommand(**request.options['command'])
    policy = command.policy
    account_id = policy['linkedin_applications']['account_id']
    workflow_id = identity(policy['workspace'], 'linkedin-applications', account_id)
    from applypilot.database import get_connection
    from applypilot.apply.provider_observations import bind_account
    bind_account(get_connection(), command.attempt_id, workflow_id, account_id)

    async def enqueue():
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
        from .connection import connect
        from .account_workflows import ApplicationAccountWorkflow
        client = await connect()
        await client.start_workflow(ApplicationAccountWorkflow.run, ApplicationAccountCommand(policy),
            id=workflow_id, task_queue=task_queue(policy['queue_prefix'], policy['workspace'], 'workflow'),
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            start_signal='enqueue', start_signal_args=[AccountSubmission(command, request.options['requester'])])
    asyncio.run(enqueue())
    return Observation('accepted', {'workflow_id': workflow_id})

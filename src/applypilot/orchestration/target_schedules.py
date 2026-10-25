"""Owner checking cadence expressed as native Temporal Schedules."""
import asyncio
from datetime import timedelta
from temporalio.client import (Schedule, ScheduleActionStartWorkflow, ScheduleIntervalSpec,
    ScheduleSpec, SchedulePolicy, ScheduleOverlapPolicy, ScheduleState, ScheduleUpdate, ScheduleAlreadyRunningError)
from temporalio.service import RPCError, RPCStatusCode
from .contracts import Command, identity


def _targets(target_ids=None):
    from applypilot.database import get_connection, close_connection
    from applypilot.targets import store
    try:
        conn = get_connection()
        return [store.decode(row) for row in conn.execute('SELECT * FROM company_targets ORDER BY id')
                if target_ids is None or row['id'] in target_ids]
    finally:
        close_connection()


async def sync_target_schedules(target_ids=None, *, root=None, trigger=False, start=None):
    from .client import capture, run_work
    from .connection import connect
    from .settings import load
    from .workflows import DispatchWorkflow
    policy = load(root)
    client = await connect(root)
    results = []
    for target in await asyncio.to_thread(_targets, target_ids):
        schedule_id = identity(policy.workspace, 'target-schedule', target['id'])
        handle = client.get_schedule_handle(schedule_id)
        if target['cadence'] == 'once':
            try:
                await handle.delete()
            except RPCError as exc:
                if exc.status != RPCStatusCode.NOT_FOUND:
                    raise
            execution = None
            if trigger and target['state'] == 'active' and target['research_state'] == 'queued':
                execution = await run_work('discover', {'target_ids': [target['id']]}, root=root)
            results.append({'target_id': target['id'], 'cadence': 'once', 'execution': execution})
            continue
        reference = await asyncio.to_thread(capture, {'target_ids': [target['id']]})
        desired = Schedule(action=ScheduleActionStartWorkflow(DispatchWorkflow.run,
            Command(reference, policy.model_dump(mode='json'), 'discover'),
            id=identity(policy.workspace, 'target-dispatch', target['id']), task_queue=policy.queue('workflow')),
            spec=ScheduleSpec(intervals=[ScheduleIntervalSpec(every=timedelta(days=1 if target['cadence'] == 'daily' else 7))]),
            policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP, pause_on_failure=False),
            state=ScheduleState(paused=target['state'] != 'active' or start is False, note='Owner target checking cadence'))
        try:
            await client.create_schedule(schedule_id, desired, trigger_immediately=trigger and target['state'] == 'active')
        except ScheduleAlreadyRunningError:
            from dataclasses import replace
            def update(current):
                paused = target['state'] != 'active' or (start is False and current.description.schedule.state.paused)
                return ScheduleUpdate(replace(desired, state=replace(desired.state, paused=paused)))
            await handle.update(update)
            desired = (await handle.describe()).schedule
            if trigger and target['state'] == 'active':
                await handle.trigger()
        results.append({'target_id': target['id'], 'schedule_id': schedule_id, 'paused': desired.state.paused})
    return {'targets': results}

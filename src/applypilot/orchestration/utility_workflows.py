"""Durable owner requests; private model inputs/results remain in PostgreSQL."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import ActivityRequest, Command, Observation, task_queue
from .utility_contracts import PHASE_ROLES


async def operation(command, phase, **options):
    role = PHASE_ROLES[phase]
    cfg = command.policy['activities'][role]
    return await workflow.execute_activity('applypilot.utility.' + phase + '.v1',
        ActivityRequest(command.reference, phase, sequence=command.page,
            options={'heartbeat_interval_seconds': cfg['heartbeat_interval_seconds'], **options}),
        result_type=Observation, task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], role),
        start_to_close_timeout=timedelta(seconds=cfg['start_to_close_seconds']),
        heartbeat_timeout=timedelta(seconds=cfg['heartbeat_seconds']),
        cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=command.policy['activity_retry_initial_seconds']),
            maximum_interval=timedelta(seconds=command.policy['activity_retry_maximum_seconds']),
            non_retryable_error_types=['InvalidUtilityInput']))


@workflow.defn(name='applypilot.utility.v1')
class UtilityWorkflow:
    def __init__(self):
        self.phase = 'created'
        self.operation = ''

    @workflow.query
    def status(self) -> dict:
        return {'operation': self.operation, 'phase': self.phase}

    @workflow.run
    async def run(self, command: Command) -> Observation:
        self.operation = command.kind
        if command.kind in {'github', 'evidence_collect'}:
            if not command.cursor:
                self.phase = 'github_inventory'
                inventory = await operation(command, self.phase)
                if inventory.state == 'dry_run':
                    return inventory
                command.token = command.reference
                command.reference = inventory.detail['reference']
                command.cursor = 'repositories'
            for _ in range(command.policy['pages_per_run']):
                self.phase = 'github_repository'
                observed = await operation(command, self.phase)
                if observed.state == 'complete':
                    self.phase = 'github_finish'
                    result = await operation(command, self.phase)
                    command.reference = command.token
                    break
                command.page += 1
            else:
                workflow.continue_as_new(command)
        elif command.kind in {'referral_plan', 'referral_prepare'}:
            if not command.cursor:
                self.phase = 'referral_inventory'
                inventory = await operation(command, self.phase)
                command.reference = inventory.detail['selection_reference']
                command.cursor = 'cases'
            for _ in range(command.policy['pages_per_run']):
                self.phase = 'referral_case_prepare'
                prepared = await operation(command, self.phase)
                if prepared.state == 'complete':
                    self.phase = 'referral_prepare_finish'
                    result = await operation(command, self.phase)
                    break
                self.phase = 'referral_case_revise'
                await operation(command, self.phase)
                command.page += 1
            else:
                workflow.continue_as_new(command)
        elif command.kind == 'referral_discovery':
            operations = ('connections', 'people') if command.cursor == 'linkedin_rs' else (
                'people' if command.cursor in {'web', 'linkedin_mcp'} else 'posts',)
            for provider_operation in operations:
                self.phase = 'referral_discovery'
                result = await operation(command, self.phase, discovery_operation=provider_operation)
                if result.state != 'no_results':
                    break
                command.page += 1
        else:
            self.phase = command.kind
            result = await operation(command, command.kind)
        if result.detail.get('suggest_after'):
            self.phase = 'suggest'
            result = await operation(command, 'suggest', previous_result=result.detail['reference'])
        self.phase = 'complete'
        return result


UTILITY_WORKFLOWS = [UtilityWorkflow]

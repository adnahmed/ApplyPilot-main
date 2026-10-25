"""Owner-directed research and vacancy traversal using native Temporal execution."""
from dataclasses import replace
from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import ActivityRequest, Observation, task_queue
from .target_contracts import TARGET_ROLES, TargetCommand, TargetPage


async def operation(command, phase, **options):
    role = TARGET_ROLES[phase]
    settings = command.policy['activities'][role]
    return await workflow.execute_activity('applypilot.' + phase + '.v1',
        ActivityRequest(command.reference, phase, options={
            'heartbeat_interval_seconds': settings['heartbeat_interval_seconds'], 'observation': command.token,
            **options}), result_type=dict,
        task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], role),
        start_to_close_timeout=timedelta(seconds=settings['start_to_close_seconds']),
        heartbeat_timeout=timedelta(seconds=settings['heartbeat_seconds']),
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=command.policy['activity_retry_initial_seconds']),
            maximum_interval=timedelta(seconds=command.policy['activity_retry_maximum_seconds'])))


@workflow.defn(name='applypilot.owner_target.v1')
class TargetWorkflow:
    def __init__(self):
        self.command = None

    @workflow.query
    def status(self) -> dict:
        if self.command is None:
            return {'state': 'created', 'pages': 0, 'found': 0, 'new': 0}
        return {'state': self.command.phase, 'pages': len(self.command.seen),
                'found': self.command.found, 'new': self.command.new}

    @workflow.run
    async def run(self, command: TargetCommand) -> Observation:
        self.command = command
        if not command.token:
            command.token = workflow.info().run_id
        for _ in range(64):
            if command.phase == 'prepare':
                observed = await operation(command, 'target_prepare')
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                command.reference = observed['reference']
                command.query_reference = observed['query_reference']
                command.reads = [TargetPage(ref, ref) for ref in observed['read_references']]
                command.pending = [TargetPage(**page) for page in observed['pages']]
                command.searches = ['searxng', 'ddgs'] if observed['needs_research'] else []
                command.phase = 'search' if observed['needs_research'] else 'vacancy'
            elif command.phase == 'search':
                if not command.searches:
                    command.phase = 'read'
                    continue
                observed = await operation(command, 'target_search', query_reference=command.query_reference,
                                           provider=command.searches[0])
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                command.searches.pop(0)
                command.sources.extend(observed['sources'])
                known = set(command.read_seen) | {page.reference for page in command.reads}
                for reference in observed['pages']:
                    if reference not in known:
                        command.reads.append(TargetPage(reference, reference))
                        known.add(reference)
            elif command.phase == 'read':
                if not command.reads:
                    command.phase = 'synthesize'
                    continue
                page = command.reads[0]
                if page.identity in command.read_seen:
                    command.reads.pop(0)
                    continue
                observed = await operation(command, 'target_read', page_reference=page.reference, backend=page.backend)
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                if observed.get('challenge'):
                    command.reads[0] = replace(page, backend='flaresolverr')
                else:
                    command.reads.pop(0)
                    command.read_seen.extend([page.identity, observed['final_reference']])
                    command.sources.append(observed['source_reference'])
            elif command.phase == 'synthesize':
                observed = await operation(command, 'target_synthesize', sources=command.sources)
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                command.reference = observed['reference']
                command.pending = [TargetPage(**page) for page in observed['pages']]
                command.phase = 'complete' if observed['outreach_only'] else 'vacancy'
            elif command.phase == 'vacancy':
                if not command.pending:
                    command.phase = 'complete'
                    continue
                page = command.pending[0]
                if page.identity in command.seen:
                    command.pending.pop(0)
                    continue
                observed = await operation(command, 'target_vacancy', page_reference=page.reference,
                    depth=page.depth, backend=page.backend, found=command.found, last_page=len(command.pending) == 1)
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                if observed.get('challenge'):
                    command.pending[0] = replace(page, backend='flaresolverr')
                else:
                    command.pending.pop(0)
                    command.seen.append(page.identity)
                    known = set(command.seen) | {item.identity for item in command.pending}
                    following = []
                    for item in observed['pages']:
                        if item['identity'] not in known:
                            following.append(TargetPage(**item))
                            known.add(item['identity'])
                    command.pending[0:0] = following
                    command.publication_reference = observed['reference']
                    command.observations.append(observed['reference'])
                    command.found += observed['found']
                    command.coverage_clean = command.coverage_clean and observed['coverage_complete']
                    command.phase = 'publish'
            elif command.phase == 'publish':
                observed = await operation(command, 'target_publish', reference=command.publication_reference)
                if observed.get('state'):
                    return Observation(observed['state'], {'terminal': True})
                command.new += observed['new']
                command.phase = 'vacancy'
            elif command.phase == 'complete':
                observed = await operation(command, 'target_complete', observations=command.observations,
                                           coverage_clean=command.coverage_clean, found=command.found, new=command.new)
                return Observation(observed.get('state', 'exhausted'), {
                    'terminal': True, 'found': command.found, 'new': command.new,
                    'coverage_complete': command.coverage_clean})
            else:
                raise ValueError('Unknown owner-target research phase')
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)


TARGET_WORKFLOWS = [TargetWorkflow]

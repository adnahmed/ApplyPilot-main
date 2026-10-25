"""Durable relationships, messages, account pacing, and report artifacts."""
from __future__ import annotations

from datetime import timedelta
import asyncio

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

from .contracts import ActivityRequest, Command, Observation, identity, task_queue
from .integration_contracts import AccountCommand, PHASE_ROLES


async def operation(command, phase, *, once=False, **options):
    role = PHASE_ROLES[phase]
    settings = command.policy['activities'][role]
    request = ActivityRequest(command.reference, phase, command.cursor or None,
        options={'heartbeat_interval_seconds': settings['heartbeat_interval_seconds'],
                 'workspace': command.policy['workspace'], **options})
    return await workflow.execute_activity('applypilot.integration.' + phase + '.v1', request,
        result_type=Observation,
        task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], role),
        start_to_close_timeout=timedelta(seconds=settings['start_to_close_seconds']),
        heartbeat_timeout=timedelta(seconds=settings['heartbeat_seconds']),
        cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=command.policy['activity_retry_initial_seconds']),
            maximum_interval=timedelta(seconds=command.policy['activity_retry_maximum_seconds']),
            maximum_attempts=1 if once else 0,
            non_retryable_error_types=['InvalidWorkInput', 'CareerRequestError']))


async def _start(command, run, kind, subject):
    try:
        return await workflow.start_child_workflow(run, command,
            id=identity(command.policy['workspace'], kind, subject),
            task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow'),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON)
    except WorkflowAlreadyStartedError:
        return None


async def _outreaches(command, result):
    for item in result.detail.get('outreach', []):
        await _start(Command(item['reference'], command.policy, 'outreach'),
                     OutreachWorkflow.run, 'outreach', item['subject'])


@workflow.defn(name='applypilot.report.v1')
class ReportWorkflow:
    @workflow.run
    async def run(self, command: Command) -> Observation:
        return await operation(command, 'generate_report')


@workflow.defn(name='applypilot.reports.v1')
class ReportsWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        command.kind = 'career_ops'
        if not command.cursor:
            await _start(command, CareerInvitationsWorkflow.run, 'career-invitations', command.policy['workspace'])
        return await _inventory(command)


async def _inventory(command):
    for _ in range(command.policy['pages_per_run']):
        page = await operation(command, 'integration_inventory', kind=command.kind,
                               page_size=command.policy['page_size'])
        for item in page.detail.get('items', []):
            run = {'relationship': NetworkingRelationshipWorkflow.run,
                   'outreach': OutreachWorkflow.run, 'report': ReportWorkflow.run}[item['kind']]
            await _start(Command(item['reference'], command.policy, item['kind']),
                         run, item['kind'], item['subject'])
        if page.next_cursor is None:
            return {'state': page.state}
        if page.next_cursor == command.cursor:
            raise ApplicationError('Inventory cursor did not advance', non_retryable=True)
        command.cursor = page.next_cursor
    workflow.continue_as_new(command)


@workflow.defn(name='applypilot.networking.v1')
class NetworkingWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        if not command.cursor and command.kind in {'network_prepare','network_research','referrals'}:
            await operation(command, 'network_import')
            await _start(command, FeedbackDraftsWorkflow.run, 'feedback-drafts', command.policy['workspace'])
            await _start(command, NetworkConnectionsWorkflow.run, 'network-connections', command.policy['workspace'])
        return await _inventory(command)


@workflow.defn(name='applypilot.networking-relationship.v1')
class NetworkingRelationshipWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        state = await operation(command, 'relationship_state')
        if state.state != 'active':
            return {'state': state.state}
        if not state.detail['has_contacts']:
            for phase in ('discover_person', 'identify_hiring_manager', 'identify_employee'):
                people = await operation(command, phase)
                for item in people.detail.get('people', []):
                    person = await operation(Command(item['reference'], command.policy), 'research_person')
                    if person.detail.get('person_id'):
                        await operation(command, 'identify_relationship_path', person_id=person.detail['person_id'])
                await operation(command, 'update_relationship')
                state = await operation(command, 'relationship_state')
                if state.detail.get('has_contacts'):
                    break
        prepared = await operation(command, 'prepare_referral')
        await _outreaches(command, prepared)
        replies = await operation(command, 'process_response')
        await _outreaches(command, replies)
        await workflow.sleep(timedelta(seconds=state.detail.get('renewal_seconds', 86400)))
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.outreach.v1')
class OutreachWorkflow:
    def __init__(self):
        self.result = None
        self.state = 'created'

    @workflow.signal
    def delivery_result(self, result: Observation):
        self.result = result

    @workflow.query
    def status(self) -> str:
        return self.state

    @workflow.run
    async def run(self, command: Command) -> dict:
        prepared = await operation(command, 'prepare_outreach')
        self.state = prepared.state
        if prepared.state in {'cancelled','draft','disabled'}:
            return {'state': prepared.state}
        if prepared.state == 'approved':
            account = prepared.detail['account_id']
            account_command = AccountCommand(command.policy, account)
            await _start(account_command, AccountWorkflow.run, 'outreach-account', account)
            handle = workflow.get_external_workflow_handle(identity(command.policy['workspace'], 'outreach-account', account))
            try:
                await handle.signal('enqueue', {'reference': command.reference,
                    'reply_workflow': workflow.info().workflow_id, 'channel': prepared.detail['channel']})
                await workflow.wait_condition(lambda: self.result is not None)
            except asyncio.CancelledError:
                await handle.signal('cancel', command.reference)
                await operation(command, 'cancel_outreach')
                raise
            self.state = self.result.state
            if self.state not in {'sent','sending','uncertain'}:
                if self.state == 'unreachable':
                    relation = Command(prepared.detail['relationship'], command.policy, 'relationship')
                    await _outreaches(command, await operation(relation, 'prepare_fallback'))
                return {'state': self.state}
        relationship = Command(prepared.detail['relationship'], command.policy, 'relationship')
        # Store the original deadline in Workflow input across Continue-As-New.
        if command.selection_time is None:
            command.selection_time = workflow.now().timestamp() + prepared.detail['followup_seconds']
        for _ in range(100):
            self.state = 'observing_response'
            observed = await operation(command, 'observe_email' if prepared.detail['channel'] == 'email' else 'observe_response')
            await _outreaches(command, await operation(relationship, 'process_response'))
            if observed.state in {'cancelled','suppressed','conversation_changed'}:
                return {'state': observed.state}
            if workflow.now().timestamp() >= command.selection_time:
                if observed.state == 'sent' and prepared.detail['request']:
                    followup = await operation(command, 'follow_up')
                    if followup.detail.get('reference'):
                        await _start(Command(followup.detail['reference'], command.policy, 'outreach'),
                            OutreachWorkflow.run, 'outreach', followup.detail['outreach_id'])
                elif observed.state == 'sent':
                    await _outreaches(command, await operation(relationship, 'prepare_fallback'))
                return {'state': observed.state}
            await workflow.sleep(timedelta(seconds=min(prepared.detail['observe_seconds'],
                command.selection_time - workflow.now().timestamp())))
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.outreach-account.v1')
class AccountWorkflow:
    def __init__(self):
        self.pending = []
        self.cancelled = set()
        self.current_activity = None
        self.current_reference = None
        self.pacing = {}
        self.pacing_revision = 0
        self.effective_pacing = {}

    @workflow.update
    def set_pacing(self, pacing: dict) -> dict:
        self._validate_pacing(pacing)
        self.pacing = dict(pacing)
        self.pacing_revision += 1
        return dict(self.pacing)

    @set_pacing.validator
    def _validate_pacing(self, pacing: dict) -> None:
        import math
        if not isinstance(pacing, dict) or set(pacing) != {'spacing_seconds', 'daily_limit'}:
            raise ValueError('Provide spacing_seconds and daily_limit')
        spacing = pacing['spacing_seconds']
        if type(spacing) not in (int, float) or not math.isfinite(spacing) or spacing <= 0:
            raise ValueError('Spacing must be positive and finite')
        if type(pacing['daily_limit']) is not int or pacing['daily_limit'] < -1:
            raise ValueError('Daily limit must be an integer: -1 uncapped, 0 paused, or positive')

    @workflow.query
    def status(self) -> dict:
        return {'pending': len(self.pending), 'active_reference': self.current_reference,
                'pacing': dict(self.pacing), 'effective_pacing': dict(self.effective_pacing),
                'pacing_revision': self.pacing_revision}

    @workflow.signal
    def enqueue(self, value: dict):
        if not any(item['reference'] == value['reference'] for item in self.pending):
            self.pending.append(value)

    @workflow.signal
    def cancel(self, reference: str):
        self.cancelled.add(reference)
        if self.current_reference == reference and self.current_activity is not None:
            self.current_activity.cancel()

    @workflow.run
    async def run(self, command: AccountCommand) -> None:
        self.pending = list({item['reference']: item for item in [*command.pending, *self.pending]}.values())
        self.cancelled.update(command.cancelled)
        if not self.pacing:
            self.pacing = dict(command.pacing)
        self.pacing_revision += command.pacing_revision
        dispatched = list(command.dispatched_at)
        for _ in range(100):
            await workflow.wait_condition(lambda: bool(self.pending))
            item = self.pending[0]
            self.current_reference = item['reference']
            work = Command(item['reference'], command.policy, 'outreach')
            while True:
                if item['reference'] in self.cancelled:
                    result = Observation('cancelled')
                    break
                try:
                    policy = await operation(work, 'professional_send_policy' if item.get('kind') else 'account_send_policy', seed_history=not command.initialized)
                except ActivityError:
                    result = Observation('needs_attention')
                    break
                if policy.state != 'ready':
                    result = policy
                    break
                if not command.initialized:
                    dispatched.extend(policy.detail.get('historical_attempts', []))
                    command.initialized = True
                revision = self.pacing_revision
                spacing = self.pacing.get('spacing_seconds', policy.detail['spacing_seconds'])
                limits = [value for value in (policy.detail['daily_limit'], self.pacing.get('daily_limit', -1))
                          if value >= 0]
                daily = min(limits) if limits else -1
                self.effective_pacing = {'spacing_seconds': spacing, 'daily_limit': daily}
                if daily == 0:
                    await workflow.wait_condition(lambda: item['reference'] in self.cancelled
                                                  or self.pacing_revision != revision)
                    continue
                now = workflow.now()
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                last_dispatched = max(dispatched, default=0)
                dispatched = [stamp for stamp in dispatched if stamp >= midnight - 86400 or stamp == last_dispatched]
                ready_at = max(policy.detail['not_before'],
                    last_dispatched + spacing)
                if daily > 0 and sum(stamp >= midnight for stamp in dispatched) >= daily:
                    ready_at = max(ready_at, midnight + 86400)
                delay = max(0, ready_at - now.timestamp())
                if delay:
                    try:
                        await workflow.wait_condition(lambda: item['reference'] in self.cancelled
                                                      or self.pacing_revision != revision,
                                                      timeout=timedelta(seconds=delay))
                    except TimeoutError:
                        pass
                    continue
                dispatched.append(workflow.now().timestamp())
                self.current_activity = asyncio.create_task(operation(work,
                    item.get('kind') or ('send_email' if item['channel'] == 'email' else 'send_social'), once=True))
                try:
                    result = await self.current_activity
                except ActivityError:
                    result = Observation('needs_attention')
                except asyncio.CancelledError:
                    if item['reference'] not in self.cancelled:
                        raise
                    result = Observation('cancelled')
                finally:
                    self.current_activity = None
                break
            try:
                await workflow.get_external_workflow_handle(item['reply_workflow']).signal('delivery_result', result)
            except ApplicationError:
                # A closed caller cannot undo a provider effect; its receipt remains authoritative.
                workflow.logger.info('Outreach caller closed before account receipt delivery')
            self.pending.pop(0)
            self.cancelled.discard(item['reference'])
            self.current_reference = None
        workflow.continue_as_new(AccountCommand(command.policy, command.account_id, self.pending,
            dispatched, command.initialized, list(self.cancelled), dict(self.pacing), self.pacing_revision))


@workflow.defn(name='applypilot.network-connections.v1')
class NetworkConnectionsWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        for page in range(command.page, command.page + command.policy['pages_per_run']):
            result = await operation(command, 'network_connections_page', page=page)
            if not result.detail.get('more'):
                return {'state': result.state}
        command.page += command.policy['pages_per_run']
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.feedback-drafts.v1')
class FeedbackDraftsWorkflow:
    @workflow.run
    async def run(self, command: Command) -> Observation:
        return await operation(command, 'prepare_rejection_feedback')


@workflow.defn(name='applypilot.career-invitation.v1')
class CareerInvitationWorkflow:
    @workflow.run
    async def run(self, command: Command) -> Observation:
        return await operation(command, 'classify_career_reply')


@workflow.defn(name='applypilot.career-invitations.v1')
class CareerInvitationsWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        for _ in range(command.policy['pages_per_run']):
            page = await operation(command, 'career_invitation_inventory', page_size=command.policy['page_size'])
            for item in page.detail.get('items', []):
                await _start(Command(item['reference'], command.policy), CareerInvitationWorkflow.run,
                    'career-invitation', item['subject'])
            if page.next_cursor is None:
                return {'state': 'complete'}
            command.cursor = page.next_cursor
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.email-warmup.v1')
class EmailWarmupWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        warmup = await operation(command, 'observe_warmup')
        domain = await operation(command, 'domain_email_status')
        return {'warmup': warmup.state, 'domain': domain.state}


@workflow.defn(name='applypilot.professional-action.v1')
class ProfessionalActionWorkflow:
    def __init__(self):
        self.result = None

    @workflow.signal
    def delivery_result(self, result: Observation):
        self.result = result

    @workflow.run
    async def run(self, command: Command) -> dict:
        policy = await operation(command, 'professional_send_policy')
        if policy.state != 'ready':
            return {'state': policy.state}
        account = policy.detail['account_id']
        await _start(AccountCommand(command.policy, account), AccountWorkflow.run, 'outreach-account', account)
        handle = workflow.get_external_workflow_handle(identity(command.policy['workspace'], 'outreach-account', account))
        try:
            await handle.signal('enqueue', {'reference': command.reference, 'reply_workflow': workflow.info().workflow_id,
                                          'channel': policy.detail['channel'], 'kind': policy.detail['kind']})
            await workflow.wait_condition(lambda: self.result is not None)
        except asyncio.CancelledError:
            await handle.signal('cancel', command.reference)
            await operation(command, 'cancel_professional_action')
            raise
        if self.result.state == 'sent' and policy.detail['kind'] == 'publish_response':
            await workflow.sleep(timedelta(hours=24))
            await operation(command, 'observe_reply')
        return {'state': self.result.state}


@workflow.defn(name='applypilot.social.v1')
class SocialWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        if not command.cursor:
            signals = await operation(command, 'discover_social_signal')
            for reference in signals.detail.get('signals', []):
                author = await operation(Command(reference, command.policy), 'research_author')
                if author.detail.get('reference'):
                    await operation(Command(author.detail['reference'], command.policy), 'draft_response')
        for _ in range(command.policy['pages_per_run']):
            inventory = await operation(command, 'social_inventory', page_size=command.policy['page_size'])
            for item in inventory.detail.get('items', []):
                await _start(Command(item['reference'], command.policy, 'professional_action'),
                    ProfessionalActionWorkflow.run, 'professional-action', item['subject'])
            if not inventory.next_cursor:
                return {'state': 'complete'}
            command.cursor = inventory.next_cursor
        workflow.continue_as_new(command)


INTEGRATION_WORKFLOWS = [NetworkingWorkflow, NetworkingRelationshipWorkflow, NetworkConnectionsWorkflow,
    OutreachWorkflow, AccountWorkflow, ReportWorkflow, ReportsWorkflow, EmailWarmupWorkflow,
    SocialWorkflow, ProfessionalActionWorkflow, FeedbackDraftsWorkflow, CareerInvitationsWorkflow, CareerInvitationWorkflow]

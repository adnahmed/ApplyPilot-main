"""Mailbox lifecycle: native schedules, independent windows and message retries."""
from __future__ import annotations

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from .contracts import Command, identity, task_queue
from .workflows import operation


def _queue(command):
    return task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow')


async def _start(command, run, *, kind, subject):
    try:
        await workflow.start_child_workflow(run, command,
            id=identity(command.policy['workspace'], kind, subject), task_queue=_queue(command),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON)
        return True
    except WorkflowAlreadyStartedError:
        return False


async def _messages(command, result):
    for item in result.detail.get('messages', ()):
        await _start(Command(item['reference'], command.policy, 'mailbox_message'),
                     MailboxMessageWorkflow.run, kind='mailbox-message', subject=item['subject'])


@workflow.defn(name='applypilot.mailbox-sync.v1')
class MailboxSyncWorkflow:
    """A trigger never cancels active history scans or other message lifecycles.

    A separate recent window prevents a large historical backfill from hiding
    current replies. The same stable child IDs are used by CLI and schedules.
    """
    @workflow.run
    async def run(self, command: Command) -> dict:
        command.token = command.token or workflow.uuid4().hex
        result = await operation(command, 'mailbox_begin')
        if result.state == 'dry_run':
            return {'state': 'dry_run', 'started': 0}
        account = result.detail['account']
        started = 0
        for item in result.detail['windows']:
            started += await _start(Command(item['reference'], command.policy, 'mailbox_window'),
                MailboxWindowWorkflow.run, kind='mailbox-window', subject=account + ':' + item['lane'])
        started += await _start(Command(result.detail['backlog'], command.policy, 'mailbox_backlog'),
            MailboxBacklogWorkflow.run, kind='mailbox-backlog', subject=account)
        started += await _start(Command(result.detail['backlog'], command.policy, 'mailbox_observation'),
            MailboxObservationWorkflow.run, kind='mailbox-observation', subject=account)
        return {'state': 'accepted', 'started': started}


@workflow.defn(name='applypilot.mailbox-window.v1')
class MailboxWindowWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        cursor = command.cursor or None
        pages = command.page
        while True:
            result = await operation(command, 'mailbox_page', cursor=cursor, sequence=pages)
            await _messages(command, result)
            pages += 1
            if result.next_cursor is None:
                return {'state': 'complete', 'pages': pages}
            if result.next_cursor == cursor:
                raise ApplicationError('Mailbox pagination did not advance',
                                       type='SourceContractError', non_retryable=True)
            cursor = result.next_cursor
            if (pages % command.policy['discovery_pages_per_run'] == 0
                    or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new(Command(command.reference, command.policy, command.kind,
                                                  cursor, pages, token=command.token))


@workflow.defn(name='applypilot.mailbox-backlog.v1')
class MailboxBacklogWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        cursor = command.cursor or None
        for _ in range(command.policy['pages_per_run']):
            result = await operation(command, 'mailbox_pending_page', cursor=cursor)
            await _messages(command, result)
            if result.next_cursor is None:
                return {'state': 'complete'}
            if result.next_cursor == cursor:
                raise ApplicationError('Mailbox inventory did not advance',
                                       type='SourceContractError', non_retryable=True)
            cursor = result.next_cursor
        workflow.continue_as_new(Command(command.reference, command.policy, command.kind, cursor))


@workflow.defn(name='applypilot.mailbox-message.v1')
class MailboxMessageWorkflow:
    def __init__(self):
        self.state = 'created'

    @workflow.query
    def status(self) -> dict:
        return {'state': self.state}

    @workflow.run
    async def run(self, command: Command) -> dict:
        self.state = 'reading'
        result = await operation(command, 'mailbox_fetch_message')
        if result.state != 'fetched':
            self.state = result.state
            if self.state == 'processed':
                await operation(command, 'mailbox_tracking_document')
            return {'state': self.state}
        self.state = 'classifying'
        result = await operation(command, 'mailbox_classify_message')
        if result.state == 'classified':
            result = await operation(Command(result.detail['reference'], command.policy),
                                     'mailbox_publish_message')
        self.state = result.state
        if self.state == 'processed':
            await operation(command, 'mailbox_tracking_document')
        return {'state': self.state}


@workflow.defn(name='applypilot.mailbox-observation.v1')
class MailboxObservationWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        result = await operation(command, 'mailbox_observations')
        model = await operation(command, 'mailbox_outcome_model')
        return {'state': 'complete', **result.detail, **model.detail}


MAILBOX_WORKFLOWS = [MailboxSyncWorkflow, MailboxWindowWorkflow, MailboxBacklogWorkflow,
                    MailboxMessageWorkflow, MailboxObservationWorkflow]

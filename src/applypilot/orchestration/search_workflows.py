"""Provider failures are local: each provider has an independent durable child.

The dispatcher waits for each child to START, not for all providers to succeed.
Evidence is saved by each provider as it arrives. The dispatcher reports only
acceptance; it never mislabels accepted requests as completed research.
"""
from __future__ import annotations
from dataclasses import replace
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import ApplicationError, WorkflowAlreadyStartedError

from .contracts import identity
from .search_contracts import SearchActivityInput, SearchCommand, SearchDispatchResult, SearchEvidence


@workflow.defn(name='applypilot.search_provider.v1')
class SearchProviderWorkflow:
    @workflow.run
    async def run(self, command: SearchCommand) -> SearchEvidence:
        try:
            command.validate()
            if len(command.providers) != 1:
                raise ValueError('A provider workflow executes exactly one provider')
        except ValueError:
            raise ApplicationError('Invalid search provider workflow input',
                type='InvalidWorkInput', non_retryable=True) from None
        provider = command.providers[0]
        return await workflow.execute_activity('applypilot.search_' + provider + '.v1',
            SearchActivityInput(command.request_id, command.observation_id, provider),
            result_type=SearchEvidence, task_queue=command.research_queue,
            start_to_close_timeout=timedelta(seconds=command.attempt_seconds),
            retry_policy=RetryPolicy(maximum_attempts=0))


@workflow.defn(name='applypilot.search.v1')
class SearchWorkflow:
    @workflow.run
    async def run(self, command: SearchCommand) -> SearchDispatchResult:
        try:
            command.validate()
        except ValueError:
            raise ApplicationError('Invalid search workflow input',
                type='InvalidWorkInput', non_retryable=True) from None
        workflow_ids = []
        for provider in command.providers:
            wid = identity(command.workspace, 'search-' + provider, command.observation_id)
            try:
                await workflow.start_child_workflow(SearchProviderWorkflow.run,
                    replace(command, providers=(provider,)), id=wid,
                    task_queue=command.workflow_queue,
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                    parent_close_policy=workflow.ParentClosePolicy.ABANDON)
            except WorkflowAlreadyStartedError:
                # A retried dispatch never redoes a committed observation.
                pass
            workflow_ids.append(wid)
        return SearchDispatchResult(workflow_ids)


SEARCH_WORKFLOWS = [SearchWorkflow, SearchProviderWorkflow]

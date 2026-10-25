"""Temporal owns provider order, page budgets, backend fallback and signal traversal."""
from dataclasses import replace

from temporalio import workflow

from .contracts import Observation, task_queue
from .company_operations import operation
from .posting_research_contracts import (
    PostingResearchCommand, ResearchPage, ResearchSearch, SignalPage, SignalVacancyCommand,
)


def _add_pages(command, references):
    known = set(command.seen) | {page.reference for page in command.pending}
    backend = 'http' if command.crawler in {'auto', 'hybrid'} else command.crawler
    for reference in references:
        if command.max_pages is not None and len(known) >= command.max_pages:
            break
        if reference not in known:
            command.pending.append(ResearchPage(reference, backend))
            known.add(reference)


async def _collect(command, prepare_phase):
    if command.phase == 'prepare':
        prepared = await operation(command, prepare_phase)
        if prepared.get('state'):
            return prepared
        command.reference = prepared['reference']
        command.crawler, command.max_pages = prepared['crawler'], prepared['max_pages']
        providers = ('searxng', 'ddgs') if prepared['search_provider'] == 'auto' else (prepared['search_provider'],)
        command.searches = [ResearchSearch(reference, provider)
                            for reference in prepared['queries'] for provider in providers]
        _add_pages(command, prepared['pages'])
        command.phase = 'search'
    elif command.phase == 'search':
        if command.searches:
            search = command.searches[0]
            observed = await operation(command, 'posting_research_search', options={
                'query_reference': search.reference, 'provider': search.provider})
            command.reference = observed['reference']
            command.searches.pop(0)
            _add_pages(command, observed['pages'])
        else:
            command.phase = 'read'
    elif command.phase == 'read':
        if command.pending:
            page = command.pending[0]
            observed = await operation(command, 'posting_research_read', options={
                'page_reference': page.request_reference or page.reference, 'backend': page.backend})
            command.reference = observed['reference']
            if observed.get('challenge_reference') and page.backend != 'flaresolverr':
                command.pending[0] = replace(page, backend='flaresolverr',
                                            request_reference=observed['challenge_reference'])
            elif page.backend == 'http' and command.crawler in {'auto', 'hybrid'} and (
                    command.crawler == 'hybrid' or observed['thin']):
                command.pending[0] = replace(page, backend='playwright')
            else:
                command.pending.pop(0)
                command.seen.append(page.reference)
        else:
            command.phase = 'synthesize'
    else:
        raise ValueError('Unknown posting collection phase')
    return None


@workflow.defn(name='applypilot.company_signal_vacancy.v1')
class CompanySignalVacancyWorkflow:
    @workflow.run
    async def run(self, command: SignalVacancyCommand) -> dict:
        for _ in range(64):
            if not command.pending and command.observed:
                return await operation(command, 'posting_research_signal', options={'finalize': True})
            page = command.pending[0] if command.pending else None
            if page is not None and page.identity in command.seen:
                command.pending.pop(0)
                continue
            observed = await operation(command, 'posting_research_signal', options={
                'page_reference': (page.request_reference or page.reference) if page else '',
                'depth': page.depth if page else 0, 'backend': page.backend if page else 'http'})
            if observed.get('challenge_reference') and page is not None and page.backend != 'flaresolverr':
                command.pending[0] = replace(page, backend='flaresolverr',
                                            request_reference=observed['challenge_reference'])
                if workflow.info().is_continue_as_new_suggested():
                    workflow.continue_as_new(command)
                continue
            command.reference, command.observed = observed['reference'], True
            if page is not None:
                command.pending.pop(0)
                command.seen.append(page.identity)
            known = set(command.seen) | {item.identity for item in command.pending}
            following = []
            for item in observed['pages']:
                if item['identity'] not in known:
                    following.append(SignalPage(**item))
                    known.add(item['identity'])
            command.pending[0:0] = following
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.company_posting_research.v1')
class CompanyPostingResearchWorkflow:
    @workflow.run
    async def run(self, command: PostingResearchCommand) -> Observation:
        for _ in range(64):
            if command.phase in {'prepare', 'search', 'read'}:
                terminal = await _collect(command, 'posting_research_prepare')
                if terminal is not None:
                    return Observation(terminal['state'])
            elif command.phase == 'synthesize':
                value = await operation(command, 'posting_research_synthesize')
                command.reference = value['reference']
                command.signal_pages = [SignalPage(**page) for page in value.get('pages', [])]
                command.phase = 'signal' if value['signal'] else 'assess'
            elif command.phase == 'signal':
                value = await workflow.execute_child_workflow(CompanySignalVacancyWorkflow.run,
                    SignalVacancyCommand(command.reference, command.policy, pending=command.signal_pages),
                    id=workflow.info().workflow_id + ':signal-vacancies',
                    task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow'))
                command.reference, command.phase = value['reference'], 'assess'
                command.signal_pages = []
            elif command.phase == 'assess':
                value = await operation(command, 'posting_research_assess')
                command.reference, command.phase = value['reference'], 'publish'
            elif command.phase == 'publish':
                value = await operation(command, 'posting_research_publish')
                return Observation(value['state'], value.get('detail', {}))
            else:
                raise ValueError('Unknown posting research phase')
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.company_report.v1')
class CompanyReportWorkflow:
    @workflow.run
    async def run(self, command: PostingResearchCommand) -> dict:
        for _ in range(64):
            if command.phase == 'synthesize':
                return await operation(command, 'posting_research_synthesize')
            terminal = await _collect(command, 'company_report_prepare')
            if terminal is not None:
                return terminal
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)

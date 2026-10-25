"""Company/source/predictive traversal is durable deterministic Workflow state."""
from dataclasses import dataclass, field, replace
from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from .contracts import Command, EntityCommand, Observation, digest, identity, task_queue
from .company_operations import ROLES, operation
from .prediction_contracts import PredictionCommand
from .prediction_workflows import PredictiveCompanyWorkflow
from .posting_research_workflows import (CompanySignalVacancyWorkflow, CompanyPostingResearchWorkflow, CompanyReportWorkflow)
from .company_contracts import (CompanyCrawlCommand, CrawlPage, CompanySourceCommand,
                                CompanyEvidenceCommand, CompanyPage, IssuerObservation)


async def start_child(command, implementation, subject, kind):
    try:
        return await workflow.start_child_workflow(implementation, command,
            id=identity(command.policy['workspace'], kind, subject),
            task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], 'workflow'),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            parent_close_policy=workflow.ParentClosePolicy.ABANDON)
    except WorkflowAlreadyStartedError:
        return None


@workflow.defn(name='applypilot.company_discovery.v1')
class CompanyDiscoveryWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        if not command.token:
            command = replace(command, token=workflow.info().run_id)
        inventory = await operation(command, 'company_prepare', options={'cursor': command.cursor, 'observation': command.token})
        for item in inventory['items']:
            implementation = {'source': CompanySourceWorkflow.run, 'search': CompanySearchWorkflow.run,
                              'prediction': PredictiveCompanyWorkflow.run, 'drafts': CompanyDraftSelectionWorkflow.run}[item['kind']]
            child_command = (CompanySourceCommand(item['reference'], command.policy) if item['kind'] == 'source'
                             else PredictionCommand(item['reference'], command.policy) if item['kind'] == 'prediction'
                             else replace(command, reference=item['reference'], cursor='', page=0))
            await start_child(child_command,
                              implementation, item['subject'], 'company-' + item['kind'])
        if inventory.get('next_cursor'):
            workflow.continue_as_new(replace(command, cursor=inventory['next_cursor']))
        return {'state': 'accepted', 'completed': False}


@workflow.defn(name='applypilot.company_search.v1')
class CompanySearchWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        return await operation(command, 'company_search')


@workflow.defn(name='applypilot.company_source.v1')
class CompanySourceWorkflow:
    @workflow.run
    async def run(self, command: CompanySourceCommand) -> dict:
        if not command.initialized:
            command.pending = [CompanyPage(command.reference)]
            command.initialized = True
        for _ in range(64):
            if command.current is None:
                if not command.pending:
                    return {'state': 'published', 'companies': command.companies}
                page = command.pending.pop(0)
                if page.identity in command.seen:
                    continue
                command.current, command.phase = page, 'read'
            if command.phase == 'read':
                read = await operation(command, 'company_source_read', reference=command.current.reference,
                                       options={'backend': command.current.backend})
                if read.get('state') == 'paused':
                    return read
                if read.get('challenge'):
                    command.current = replace(command.current, backend='flaresolverr')
                    if workflow.info().is_continue_as_new_suggested():
                        workflow.continue_as_new(command)
                    continue
                command.reference = read['reference']
                command.seen = sorted(set([*command.seen, *read['identities']]))
                command.enrichment = [IssuerObservation(issuer, issuer, 'search', provider)
                    for issuer in read['issuers'] for provider in ('searxng', 'ddgs')]
                command.enrichment_seen, command.identified_issuers = [], []
                command.phase = 'enrich'
            elif command.phase == 'enrich':
                if not command.enrichment:
                    command.phase = 'extract'
                    continue
                step = command.enrichment[0]
                key = digest((step.reference, step.operation, step.provider))
                if step.issuer in command.identified_issuers or key in command.enrichment_seen:
                    command.enrichment.pop(0)
                    continue
                observed = await operation(command, 'company_source_enrich', reference=step.reference,
                    options={'operation': step.operation, 'provider': step.provider,
                             'document_reference': command.reference, 'backend': step.backend})
                if observed.get('challenge'):
                    command.enrichment[0] = replace(step, backend='flaresolverr')
                    if workflow.info().is_continue_as_new_suggested():
                        workflow.continue_as_new(command)
                    continue
                command.enrichment.pop(0)
                command.enrichment_seen.append(key)
                command.reference = observed.get('reference', command.reference)
                if observed.get('identified'):
                    command.identified_issuers.append(step.issuer)
                command.enrichment[0:0] = [IssuerObservation(reference, step.issuer, 'read')
                                           for reference in observed.get('candidates', [])]
            elif command.phase == 'extract':
                extracted = await operation(command, 'company_source_extract')
                for item in extracted['companies']:
                    await start_child(CompanyEvidenceCommand(item['reference'], command.policy),
                                      CompanyEvidenceWorkflow.run, item['subject'], 'company-evidence')
                candidates = []
                if extracted.get('continuation'):
                    candidates.append(CompanyPage(**extracted['continuation'], depth=command.current.depth))
                if command.current.depth < 2:
                    candidates.extend(CompanyPage(**item, depth=command.current.depth + 1)
                                      for item in extracted['details'])
                known = set(command.seen) | {page.identity for page in command.pending}
                for page in candidates:
                    if page.identity not in known:
                        command.pending.append(page)
                        known.add(page.identity)
                command.reference = extracted['reference']
                command.companies += len(extracted['companies'])
                command.phase = 'publish'
            elif command.phase == 'publish':
                await operation(command, 'company_source_publish')
                command.current = None
            else:
                raise ValueError('Unknown company source phase')
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)


@workflow.defn(name='applypilot.company_evidence.v1')
class CompanyEvidenceWorkflow:
    @workflow.run
    async def run(self, command: CompanyEvidenceCommand) -> dict:
        while not command.prepared:
            read = await operation(command, 'company_read', options={'backend': command.backend})
            if read.get('challenge'):
                command.backend = 'flaresolverr'
                if workflow.info().is_continue_as_new_suggested():
                    workflow.continue_as_new(command)
                continue
            if read.get('state') == 'passive':
                return await operation(command, 'company_publish', reference=read['reference'])
            assessed = await operation(command, 'company_assess', reference=read['reference'])
            command = replace(command, reference=assessed['reference'], prepared=True,
                              pending=[CompanyPage(**page) for page in assessed['pages']])
        for _ in range(64):
            if not command.pending:
                return await operation(command, 'company_publish')
            page = command.pending[0]
            if page.identity in command.seen:
                command.pending.pop(0)
                continue
            observed = await operation(command, 'company_vacancy',
                options={'page_reference': page.reference, 'depth': page.depth, 'backend': page.backend})
            if observed.get('challenge'):
                command.pending[0] = replace(page, backend='flaresolverr')
                if workflow.info().is_continue_as_new_suggested():
                    workflow.continue_as_new(command)
                continue
            command.reference = observed['reference']
            command.pending.pop(0)
            command.seen.append(page.identity)
            known = set(command.seen) | {item.identity for item in command.pending}
            following = []
            for item in observed['pages']:
                if item['identity'] not in known:
                    following.append(CompanyPage(**item))
                    known.add(item['identity'])
            command.pending[0:0] = following
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)




COMPANY_WORKFLOWS = [CompanyDiscoveryWorkflow, CompanySearchWorkflow, CompanySourceWorkflow,
                     CompanyEvidenceWorkflow, PredictiveCompanyWorkflow]





COMPANY_WORKFLOWS.append(CompanySignalVacancyWorkflow)




COMPANY_WORKFLOWS.append(CompanyPostingResearchWorkflow)





COMPANY_WORKFLOWS.append(CompanyReportWorkflow)



@workflow.defn(name='applypilot.company_draft_selection.v1')
class CompanyDraftSelectionWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        selected = await operation(command, 'prediction_select')
        for item in selected['items']:
            await start_child(replace(command, reference=item['reference']), CompanyPredictionDraftWorkflow.run,
                              item['subject'], 'company-draft')
        return {'state': 'accepted', 'selected': len(selected['items'])}


@workflow.defn(name='applypilot.company_prediction_draft.v1')
class CompanyPredictionDraftWorkflow:
    @workflow.run
    async def run(self, command: Command) -> dict:
        drafted = await operation(command, 'prediction_draft')
        return await operation(command, 'prediction_publish', reference=drafted['reference'])


COMPANY_WORKFLOWS.extend([CompanyDraftSelectionWorkflow, CompanyPredictionDraftWorkflow])



@workflow.defn(name='applypilot.company_crawl.v1')
class CompanyCrawlWorkflow:
    @workflow.run
    async def run(self, command: CompanyCrawlCommand) -> dict:
        if not command.prepared:
            prepared = await operation(command, 'company_crawl_prepare')
            command = replace(command, prepared=True, backend=prepared['backend'],
                allow_browseros=prepared['allow_browseros'], max_pages=prepared['max_pages'],
                max_depth=prepared['max_depth'], pending=[CrawlPage(prepared['root_reference'], 0,
                    'http' if prepared['backend'] in {'auto', 'hybrid'} else prepared['backend'])])
        for _ in range(64):
            if not command.pending or len(command.documents) >= command.max_pages:
                return await operation(command, 'company_crawl_publish', options={
                    'documents': command.documents, 'complete': not command.pending})
            page = command.pending[0]
            if page.reference in command.seen:
                command.pending.pop(0)
                continue
            observed = await operation(command, 'company_crawl_page', reference=page.reference,
                options={'crawl_reference': command.reference, 'backend': page.backend,
                         'previous_reference': page.previous_reference})
            if observed.get('challenge'):
                command.pending[0] = replace(page, backend='flaresolverr')
                if workflow.info().is_continue_as_new_suggested():
                    workflow.continue_as_new(command)
                continue
            render = (page.backend == 'http' and command.backend in {'auto', 'hybrid'}
                      and (observed['thin'] or command.backend == 'hybrid'))
            browser = (observed['thin'] and command.allow_browseros
                       and page.backend != 'browseros' and not render)
            if render or browser:
                command.pending[0] = replace(page, backend='playwright' if render else 'browseros',
                                            previous_reference=observed['document_reference'])
            else:
                command.pending.pop(0)
                command.documents.append(observed['document_reference'])
                command.seen = sorted(set([*command.seen, page.reference, observed['final_reference']]))
                if page.depth < command.max_depth:
                    known = set(command.seen) | {item.reference for item in command.pending}
                    backend = 'http' if command.backend in {'auto', 'hybrid'} else command.backend
                    for reference in observed['links']:
                        if reference not in known:
                            command.pending.append(CrawlPage(reference, page.depth + 1, backend))
                            known.add(reference)
            if workflow.info().is_continue_as_new_suggested():
                workflow.continue_as_new(command)
        workflow.continue_as_new(command)


COMPANY_WORKFLOWS.append(CompanyCrawlWorkflow)

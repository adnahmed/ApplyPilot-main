"""Native employer research client for existing CLI/report consumers."""
import asyncio
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from .contracts import identity, job_identity
from .posting_research_contracts import PostingResearchCommand


async def research_job(job_url, *, ai=True, root=None):
    from .client import capture
    from .connection import connect
    from .settings import load
    from .company_workflows import CompanyPostingResearchWorkflow
    from applypilot.integrations.state import SourceState
    policy = load(root)
    reference = await asyncio.to_thread(capture, {'company_ai': ai}, job_url=job_url, workspace=policy.workspace)
    entity = job_identity(policy.workspace, job_url)
    client = await connect(root)
    handle = await client.start_workflow(CompanyPostingResearchWorkflow.run,
        PostingResearchCommand(reference, policy.model_dump(mode='json'), entity_id=entity),
        id=identity(policy.workspace, 'posting-research', entity), task_queue=policy.queue('workflow'),
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    result = await handle.result()
    report = await asyncio.to_thread(lambda: SourceState().report('job:' + job_url))
    return report or {'status': 'skipped' if result.state == 'not_applicable' else result.state,
                      'job': {'url': job_url}, 'sources': [], 'sent': 0}


async def research_company(target, *, fields, queries, max_pages, ai, search, crawler, fresh=False, root=None):
    from .client import capture
    from .connection import connect
    from .settings import load
    from .company_workflows import CompanyReportWorkflow
    from .contracts import Command
    from . import store
    import uuid
    policy = load(root)
    options = {'company_report': {'target': target, 'fields': list(fields), 'queries': queries,
               'max_pages': max_pages, 'ai': ai, 'search': search, 'crawler': crawler}}
    reference = await asyncio.to_thread(capture, options)
    client = await connect(root)
    handle = await client.start_workflow(CompanyReportWorkflow.run,
        PostingResearchCommand(reference, policy.model_dump(mode='json'), kind='company_report'),
        id=identity(policy.workspace, 'company-report', reference + (uuid.uuid4().hex if fresh else '')),
        task_queue=policy.queue('workflow'), id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    result = await handle.result()
    def report():
        from applypilot.database import get_connection, close_connection
        try:
            return store.get(get_connection(), result['reference'])['report']
        finally:
            close_connection()
    return await asyncio.to_thread(report)


async def crawl(url, *, backend, allow_browseros=False, max_pages=1, depth=0, focus=None, fresh=False, root=None):
    from .client import capture
    from .connection import connect
    from .settings import load
    from .company_workflows import CompanyCrawlWorkflow
    from .company_contracts import CompanyCrawlCommand
    from . import store
    import uuid
    policy = load(root)
    options = {'crawl': {'url': url, 'backend': backend, 'allow_browseros': allow_browseros,
                        'max_pages': max_pages, 'depth': depth, 'focus': focus, 'fresh': fresh}}
    reference = await asyncio.to_thread(capture, options)
    client = await connect(root)
    handle = await client.start_workflow(CompanyCrawlWorkflow.run, CompanyCrawlCommand(reference, policy.model_dump(mode='json')),
        id=identity(policy.workspace, 'crawl', reference + (uuid.uuid4().hex if fresh else '')),
        task_queue=policy.queue('workflow'), id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE)
    observed = await handle.result()
    def result():
        from applypilot.database import get_connection, close_connection
        try:
            return store.get(get_connection(), observed['reference'])['result']
        finally:
            close_connection()
    return await asyncio.to_thread(result)

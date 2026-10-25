"""Deterministic Activity commands shared by company and posting workflows."""
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from .contracts import ActivityRequest, task_queue

ROLES = {
    'company_prepare': 'maintenance', 'company_search': 'research', 'company_source_read': 'research',
    'company_source_extract': 'models', 'company_source_publish': 'maintenance', 'company_read': 'research',
    'company_assess': 'models', 'company_vacancy': 'discovery', 'company_publish': 'maintenance',
    'prediction_prepare': 'maintenance', 'prediction_read': 'research', 'prediction_search': 'research',
    'prediction_analyze': 'models', 'prediction_audit': 'models', 'prediction_publish': 'maintenance',
    'prediction_draft': 'models', 'prediction_vacancy': 'discovery', 'prediction_select': 'maintenance',
    'posting_research_prepare': 'maintenance', 'posting_research_search': 'research',
    'posting_research_read': 'research', 'posting_research_synthesize': 'models',
    'posting_research_signal': 'discovery', 'posting_research_assess': 'models',
    'posting_research_publish': 'maintenance', 'company_report_prepare': 'maintenance',
    'company_source_enrich': 'research', 'company_crawl_prepare': 'maintenance',
    'company_crawl_page': 'research', 'company_crawl_publish': 'maintenance',
}


async def operation(command, phase, *, reference=None, options=None):
    role = ROLES[phase]
    settings = command.policy['activities'][role]
    return await workflow.execute_activity('applypilot.' + phase + '.v1',
        ActivityRequest(reference or command.reference, phase, options={
            'heartbeat_interval_seconds': settings['heartbeat_interval_seconds'], **(options or {})}),
        result_type=dict, task_queue=task_queue(command.policy['queue_prefix'], command.policy['workspace'], role),
        start_to_close_timeout=timedelta(seconds=settings['start_to_close_seconds']),
        heartbeat_timeout=timedelta(seconds=settings['heartbeat_seconds']),
        retry_policy=RetryPolicy(initial_interval=timedelta(seconds=command.policy['activity_retry_initial_seconds']),
            maximum_interval=timedelta(seconds=command.policy['activity_retry_maximum_seconds'])))

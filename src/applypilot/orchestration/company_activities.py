"""Finite company evidence operations. Unexpected failures reach the SDK unchanged."""
from temporalio import activity
from temporalio.exceptions import ApplicationError
from datetime import timedelta
import math
from .activity_runtime import heartbeats
from .contracts import ActivityRequest


def _execute(request, phase):
    from applypilot.database import close_connection
    from .company_domain import execute
    from applypilot.integrations.types import SourceError
    if request.phase != phase:
        raise ValueError('Company Activity phase mismatch')
    try:
        with heartbeats(request.options['heartbeat_interval_seconds']) as check:
            return execute(request, check)
    except SourceError as exc:
        delay = exc.retry_after
        if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
            raise ApplicationError('Provider requested a later attempt',
                                   next_retry_delay=timedelta(seconds=delay)) from None
        raise
    finally:
        close_connection()

@activity.defn(name='applypilot.company_prepare.v1')
def company_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'company_prepare')


@activity.defn(name='applypilot.company_search.v1')
def company_search(request: ActivityRequest) -> dict:
    return _execute(request, 'company_search')


@activity.defn(name='applypilot.company_source_read.v1')
def company_source_read(request: ActivityRequest) -> dict:
    return _execute(request, 'company_source_read')


@activity.defn(name='applypilot.company_source_extract.v1')
def company_source_extract(request: ActivityRequest) -> dict:
    return _execute(request, 'company_source_extract')


@activity.defn(name='applypilot.company_source_publish.v1')
def company_source_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'company_source_publish')


@activity.defn(name='applypilot.company_read.v1')
def company_read(request: ActivityRequest) -> dict:
    return _execute(request, 'company_read')


@activity.defn(name='applypilot.company_assess.v1')
def company_assess(request: ActivityRequest) -> dict:
    return _execute(request, 'company_assess')


@activity.defn(name='applypilot.company_vacancy.v1')
def company_vacancy(request: ActivityRequest) -> dict:
    return _execute(request, 'company_vacancy')


@activity.defn(name='applypilot.company_publish.v1')
def company_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'company_publish')


@activity.defn(name='applypilot.prediction_prepare.v1')
def prediction_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_prepare')


@activity.defn(name='applypilot.prediction_read.v1')
def prediction_read(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_read')


@activity.defn(name='applypilot.prediction_search.v1')
def prediction_search(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_search')


@activity.defn(name='applypilot.prediction_analyze.v1')
def prediction_analyze(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_analyze')


@activity.defn(name='applypilot.prediction_audit.v1')
def prediction_audit(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_audit')


@activity.defn(name='applypilot.prediction_publish.v1')
def prediction_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_publish')


@activity.defn(name='applypilot.prediction_draft.v1')
def prediction_draft(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_draft')


@activity.defn(name='applypilot.posting_research_prepare.v1')
def posting_research_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_prepare')


@activity.defn(name='applypilot.posting_research_search.v1')
def posting_research_search(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_search')


@activity.defn(name='applypilot.posting_research_read.v1')
def posting_research_read(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_read')


@activity.defn(name='applypilot.posting_research_synthesize.v1')
def posting_research_synthesize(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_synthesize')


@activity.defn(name='applypilot.posting_research_signal.v1')
def posting_research_signal(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_signal')


@activity.defn(name='applypilot.posting_research_assess.v1')
def posting_research_assess(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_assess')


@activity.defn(name='applypilot.posting_research_publish.v1')
def posting_research_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'posting_research_publish')


COMPANY_ACTIVITIES_BY_ROLE = {
    'maintenance': [company_prepare, company_source_publish, company_publish, prediction_prepare, prediction_publish],
    'research': [company_search, company_source_read, company_read, prediction_read, prediction_search],
    'models': [company_source_extract, company_assess, prediction_analyze, prediction_audit, prediction_draft],
    'discovery': [company_vacancy],
}
COMPANY_ACTIVITIES_BY_ROLE.setdefault('maintenance', []).extend([posting_research_prepare, posting_research_publish])
COMPANY_ACTIVITIES_BY_ROLE.setdefault('research', []).extend([posting_research_search, posting_research_read])
COMPANY_ACTIVITIES_BY_ROLE.setdefault('models', []).extend([posting_research_synthesize, posting_research_assess])
COMPANY_ACTIVITIES_BY_ROLE.setdefault('discovery', []).extend([posting_research_signal])

@activity.defn(name='applypilot.company_report_prepare.v1')
def company_report_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'company_report_prepare')

COMPANY_ACTIVITIES_BY_ROLE['maintenance'].append(company_report_prepare)

@activity.defn(name='applypilot.company_source_enrich.v1')
def company_source_enrich(request: ActivityRequest) -> dict:
    return _execute(request, 'company_source_enrich')

COMPANY_ACTIVITIES_BY_ROLE['research'].append(company_source_enrich)

@activity.defn(name='applypilot.prediction_vacancy.v1')
def prediction_vacancy(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_vacancy')

COMPANY_ACTIVITIES_BY_ROLE['discovery'].append(prediction_vacancy)

@activity.defn(name='applypilot.prediction_select.v1')
def prediction_select(request: ActivityRequest) -> dict:
    return _execute(request, 'prediction_select')

COMPANY_ACTIVITIES_BY_ROLE['maintenance'].append(prediction_select)

@activity.defn(name='applypilot.company_crawl_prepare.v1')
def company_crawl_prepare(request: ActivityRequest) -> dict:
    return _execute(request, 'company_crawl_prepare')


@activity.defn(name='applypilot.company_crawl_page.v1')
def company_crawl_page(request: ActivityRequest) -> dict:
    return _execute(request, 'company_crawl_page')

COMPANY_ACTIVITIES_BY_ROLE['maintenance'].append(company_crawl_prepare)
COMPANY_ACTIVITIES_BY_ROLE['research'].append(company_crawl_page)


@activity.defn(name='applypilot.company_crawl_publish.v1')
def company_crawl_publish(request: ActivityRequest) -> dict:
    return _execute(request, 'company_crawl_publish')


COMPANY_ACTIVITIES_BY_ROLE['maintenance'].append(company_crawl_publish)

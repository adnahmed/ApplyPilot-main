"""Named finite Activities for owner-requested evidence and profile semantics."""
import logging
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError
from .activities import execution
from .contracts import ActivityRequest, Observation
from .activity_runtime import provider_retry_delay


def _run(request, phase):
    if request.phase != phase:
        raise ApplicationError('Utility phase mismatch', type='InvalidUtilityInput', non_retryable=True)
    from .utility_domain import execute
    failure = None
    try:
        with execution(request) as (stop, heartbeat):
            return execute(request, stop, heartbeat)
    except (ApplicationError, CancelledError):
        raise
    except Exception as exc:
        logging.getLogger(__name__).exception('Utility operation failed phase=%s', phase)
        failure = ApplicationError('Utility operation failed; inspect private worker diagnostics',
                                   next_retry_delay=provider_retry_delay(exc))
    raise failure


@activity.defn(name='applypilot.utility.impact_source_draft.v1')
def impact_source_draft(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_source_draft')


@activity.defn(name='applypilot.utility.impact_bundle_draft.v1')
def impact_bundle_draft(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_bundle_draft')


@activity.defn(name='applypilot.utility.impact_brief.v1')
def impact_brief(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_brief')


@activity.defn(name='applypilot.utility.impact_remeasure.v1')
def impact_remeasure(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_remeasure')


@activity.defn(name='applypilot.utility.impact_reconcile.v1')
def impact_reconcile(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_reconcile')


@activity.defn(name='applypilot.utility.evidence_prune.v1')
def evidence_prune(request: ActivityRequest) -> Observation:
    return _run(request, 'evidence_prune')


@activity.defn(name='applypilot.utility.impact_extract.v1')
def impact_extract(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_extract')


@activity.defn(name='applypilot.utility.impact_revise.v1')
def impact_revise(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_revise')


@activity.defn(name='applypilot.utility.suggest.v1')
def suggest(request: ActivityRequest) -> Observation:
    return _run(request, 'suggest')


@activity.defn(name='applypilot.utility.intake.v1')
def intake(request: ActivityRequest) -> Observation:
    return _run(request, 'intake')


@activity.defn(name='applypilot.utility.material_url.v1')
def material_url(request: ActivityRequest) -> Observation:
    return _run(request, 'material_url')


@activity.defn(name='applypilot.utility.github_inventory.v1')
def github_inventory(request: ActivityRequest) -> Observation:
    return _run(request, 'github_inventory')


@activity.defn(name='applypilot.utility.github_repository.v1')
def github_repository(request: ActivityRequest) -> Observation:
    return _run(request, 'github_repository')


@activity.defn(name='applypilot.utility.github_finish.v1')
def github_finish(request: ActivityRequest) -> Observation:
    return _run(request, 'github_finish')


@activity.defn(name='applypilot.utility.referral_discovery.v1')
def referral_discovery(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_discovery')


@activity.defn(name='applypilot.utility.referral_revise.v1')
def referral_revise(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_revise')


@activity.defn(name='applypilot.utility.referral_reconcile.v1')
def referral_reconcile(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_reconcile')


@activity.defn(name='applypilot.utility.github_person.v1')
def github_person(request: ActivityRequest) -> Observation:
    return _run(request, 'github_person')


UTILITY_ACTIVITIES_BY_ROLE = {
    'network': [referral_discovery],
    'email': [referral_reconcile],
    'documents': [intake],
    'maintenance': [github_finish],
    'models': [impact_source_draft, impact_bundle_draft, impact_brief, impact_remeasure, impact_reconcile, evidence_prune, impact_extract, impact_revise, suggest, github_repository, referral_revise],
    'research': [material_url, github_inventory, github_person],
}


@activity.defn(name='applypilot.utility.impact_dedup.v1')
def impact_dedup(request: ActivityRequest) -> Observation:
    return _run(request, 'impact_dedup')


UTILITY_ACTIVITIES_BY_ROLE['models'].append(impact_dedup)


@activity.defn(name='applypilot.utility.referral_inventory.v1')
def referral_inventory(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_inventory')


@activity.defn(name='applypilot.utility.referral_case_prepare.v1')
def referral_case_prepare(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_case_prepare')


@activity.defn(name='applypilot.utility.referral_case_revise.v1')
def referral_case_revise(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_case_revise')


@activity.defn(name='applypilot.utility.referral_prepare_finish.v1')
def referral_prepare_finish(request: ActivityRequest) -> Observation:
    return _run(request, 'referral_prepare_finish')


UTILITY_ACTIVITIES_BY_ROLE['maintenance'].extend([referral_inventory, referral_case_prepare, referral_prepare_finish])
UTILITY_ACTIVITIES_BY_ROLE['models'].append(referral_case_revise)

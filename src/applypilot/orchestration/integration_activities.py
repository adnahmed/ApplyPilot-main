"""One named native Activity per semantic integration operation."""
from temporalio import activity
from temporalio.exceptions import ApplicationError, CancelledError
from .contracts import ActivityRequest, Observation
from .activities import execution
from .integration_contracts import PHASE_ROLES
from .activity_runtime import provider_retry_delay


def _run(request, phase):
    if request.phase != phase:
        raise ApplicationError('Activity operation mismatch', type='InvalidWorkInput', non_retryable=True)
    from .integration_domain import execute
    failure = None
    try:
        with execution(request) as (stop, heartbeat):
            return execute(request, stop)
    except (CancelledError, ApplicationError):
        raise
    except Exception as exc:
        from applypilot.career_ops.service import RequestError
        if isinstance(exc, ApplicationError):
            raise
        import logging
        logging.getLogger(__name__).exception('Integration operation failed phase=%s', phase)
        failure = ApplicationError('Integration operation failed; inspect Activity diagnostics',
                                   non_retryable=isinstance(exc, RequestError),
                                   next_retry_delay=provider_retry_delay(exc))
    raise failure


@activity.defn(name='applypilot.integration.network_import.v1')
def network_import(request: ActivityRequest) -> Observation:
    return _run(request, 'network_import')


@activity.defn(name='applypilot.integration.network_connections_page.v1')
def network_connections_page(request: ActivityRequest) -> Observation:
    return _run(request, 'network_connections_page')


@activity.defn(name='applypilot.integration.owner_digest.v1')
def owner_digest(request: ActivityRequest) -> Observation:
    return _run(request, 'owner_digest')


@activity.defn(name='applypilot.integration.cancel_outreach.v1')
def cancel_outreach(request: ActivityRequest) -> Observation:
    return _run(request, 'cancel_outreach')


@activity.defn(name='applypilot.integration.cancel_professional_action.v1')
def cancel_professional_action(request: ActivityRequest) -> Observation:
    return _run(request, 'cancel_professional_action')


@activity.defn(name='applypilot.integration.prepare_rejection_feedback.v1')
def prepare_rejection_feedback(request: ActivityRequest) -> Observation:
    return _run(request, 'prepare_rejection_feedback')


@activity.defn(name='applypilot.integration.career_invitation_inventory.v1')
def career_invitation_inventory(request: ActivityRequest) -> Observation:
    return _run(request, 'career_invitation_inventory')


@activity.defn(name='applypilot.integration.classify_career_reply.v1')
def classify_career_reply(request: ActivityRequest) -> Observation:
    return _run(request, 'classify_career_reply')


@activity.defn(name='applypilot.integration.observe_warmup.v1')
def observe_warmup(request: ActivityRequest) -> Observation:
    return _run(request, 'observe_warmup')


@activity.defn(name='applypilot.integration.domain_email_status.v1')
def domain_email_status(request: ActivityRequest) -> Observation:
    return _run(request, 'domain_email_status')


@activity.defn(name='applypilot.integration.social_inventory.v1')
def social_inventory(request: ActivityRequest) -> Observation:
    return _run(request, 'social_inventory')


@activity.defn(name='applypilot.integration.discover_social_signal.v1')
def discover_social_signal(request: ActivityRequest) -> Observation:
    return _run(request, 'discover_social_signal')


@activity.defn(name='applypilot.integration.research_author.v1')
def research_author(request: ActivityRequest) -> Observation:
    return _run(request, 'research_author')


@activity.defn(name='applypilot.integration.draft_response.v1')
def draft_response(request: ActivityRequest) -> Observation:
    return _run(request, 'draft_response')


@activity.defn(name='applypilot.integration.prepare_connection.v1')
def prepare_connection(request: ActivityRequest) -> Observation:
    return _run(request, 'prepare_connection')


@activity.defn(name='applypilot.integration.professional_send_policy.v1')
def professional_send_policy(request: ActivityRequest) -> Observation:
    return _run(request, 'professional_send_policy')


@activity.defn(name='applypilot.integration.publish_response.v1')
def publish_response(request: ActivityRequest) -> Observation:
    return _run(request, 'publish_response')


@activity.defn(name='applypilot.integration.follow_account.v1')
def follow_account(request: ActivityRequest) -> Observation:
    return _run(request, 'follow_account')


@activity.defn(name='applypilot.integration.send_message.v1')
def send_message(request: ActivityRequest) -> Observation:
    return _run(request, 'send_message')


@activity.defn(name='applypilot.integration.send_connection.v1')
def send_connection(request: ActivityRequest) -> Observation:
    return _run(request, 'send_connection')


@activity.defn(name='applypilot.integration.observe_reply.v1')
def observe_reply(request: ActivityRequest) -> Observation:
    return _run(request, 'observe_reply')


@activity.defn(name='applypilot.integration.integration_inventory.v1')
def integration_inventory(request: ActivityRequest) -> Observation:
    return _run(request, 'integration_inventory')


@activity.defn(name='applypilot.integration.generate_report.v1')
def generate_report(request: ActivityRequest) -> Observation:
    return _run(request, 'generate_report')


@activity.defn(name='applypilot.integration.relationship_state.v1')
def relationship_state(request: ActivityRequest) -> Observation:
    return _run(request, 'relationship_state')


@activity.defn(name='applypilot.integration.discover_person.v1')
def discover_person(request: ActivityRequest) -> Observation:
    return _run(request, 'discover_person')


@activity.defn(name='applypilot.integration.identify_hiring_manager.v1')
def identify_hiring_manager(request: ActivityRequest) -> Observation:
    return _run(request, 'identify_hiring_manager')


@activity.defn(name='applypilot.integration.identify_employee.v1')
def identify_employee(request: ActivityRequest) -> Observation:
    return _run(request, 'identify_employee')


@activity.defn(name='applypilot.integration.research_person.v1')
def research_person(request: ActivityRequest) -> Observation:
    return _run(request, 'research_person')


@activity.defn(name='applypilot.integration.identify_relationship_path.v1')
def identify_relationship_path(request: ActivityRequest) -> Observation:
    return _run(request, 'identify_relationship_path')


@activity.defn(name='applypilot.integration.update_relationship.v1')
def update_relationship(request: ActivityRequest) -> Observation:
    return _run(request, 'update_relationship')


@activity.defn(name='applypilot.integration.prepare_referral.v1')
def prepare_referral(request: ActivityRequest) -> Observation:
    return _run(request, 'prepare_referral')


@activity.defn(name='applypilot.integration.prepare_fallback.v1')
def prepare_fallback(request: ActivityRequest) -> Observation:
    return _run(request, 'prepare_fallback')


@activity.defn(name='applypilot.integration.draft_outreach.v1')
def draft_outreach(request: ActivityRequest) -> Observation:
    return _run(request, 'draft_outreach')


@activity.defn(name='applypilot.integration.process_response.v1')
def process_response(request: ActivityRequest) -> Observation:
    return _run(request, 'process_response')


@activity.defn(name='applypilot.integration.prepare_outreach.v1')
def prepare_outreach(request: ActivityRequest) -> Observation:
    return _run(request, 'prepare_outreach')


@activity.defn(name='applypilot.integration.account_send_policy.v1')
def account_send_policy(request: ActivityRequest) -> Observation:
    return _run(request, 'account_send_policy')


@activity.defn(name='applypilot.integration.send_email.v1')
def send_email(request: ActivityRequest) -> Observation:
    return _run(request, 'send_email')


@activity.defn(name='applypilot.integration.send_social.v1')
def send_social(request: ActivityRequest) -> Observation:
    return _run(request, 'send_social')


@activity.defn(name='applypilot.integration.observe_response.v1')
def observe_response(request: ActivityRequest) -> Observation:
    return _run(request, 'observe_response')


@activity.defn(name='applypilot.integration.observe_email.v1')
def observe_email(request: ActivityRequest) -> Observation:
    return _run(request, 'observe_email')


@activity.defn(name='applypilot.integration.follow_up.v1')
def follow_up(request: ActivityRequest) -> Observation:
    return _run(request, 'follow_up')


ACTIVITIES_BY_ROLE = {
    'email': [observe_warmup, domain_email_status, send_email, observe_email],
    'models': [prepare_rejection_feedback, draft_response, prepare_connection, prepare_referral, prepare_fallback, draft_outreach, process_response, prepare_outreach, follow_up],
    'network': [network_import, network_connections_page, owner_digest, cancel_outreach, integration_inventory, relationship_state, discover_person, identify_hiring_manager, identify_employee, research_person, identify_relationship_path, update_relationship, account_send_policy],
    'reporting': [career_invitation_inventory, classify_career_reply, generate_report],
    'social': [cancel_professional_action, social_inventory, discover_social_signal, research_author, professional_send_policy, publish_response, follow_account, send_message, send_connection, observe_reply, send_social, observe_response],
}

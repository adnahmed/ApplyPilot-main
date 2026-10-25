"""Opaque integration identities transported in Temporal histories."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AccountCommand:
    policy: dict[str, Any]
    account_id: str
    pending: list[dict[str, str]] = field(default_factory=list)
    dispatched_at: list[float] = field(default_factory=list)
    initialized: bool = False
    cancelled: list[str] = field(default_factory=list)
    pacing: dict[str, int | float] = field(default_factory=dict)
    pacing_revision: int = 0


PHASE_ROLES = {
    'network_import': 'network',
    'network_connections_page': 'network',
    'owner_digest': 'network',
    'cancel_outreach': 'network',
    'cancel_professional_action': 'social',
    'prepare_rejection_feedback': 'models',
    'career_invitation_inventory': 'reporting',
    'classify_career_reply': 'reporting',
    'observe_warmup': 'email',
    'domain_email_status': 'email',
    'social_inventory': 'social',
    'discover_social_signal': 'social',
    'research_author': 'social',
    'draft_response': 'models',
    'prepare_connection': 'models',
    'professional_send_policy': 'social',
    'publish_response': 'social',
    'follow_account': 'social',
    'send_message': 'social',
    'send_connection': 'social',
    'observe_reply': 'social',
    'integration_inventory': 'network',
    'generate_report': 'reporting',
    'relationship_state': 'network',
    'discover_person': 'network',
    'identify_hiring_manager': 'network',
    'identify_employee': 'network',
    'research_person': 'network',
    'identify_relationship_path': 'network',
    'update_relationship': 'network',
    'prepare_referral': 'models',
    'prepare_fallback': 'models',
    'draft_outreach': 'models',
    'process_response': 'models',
    'prepare_outreach': 'models',
    'account_send_policy': 'network',
    'send_email': 'email',
    'send_social': 'social',
    'observe_response': 'social',
    'observe_email': 'email',
    'follow_up': 'models',
}

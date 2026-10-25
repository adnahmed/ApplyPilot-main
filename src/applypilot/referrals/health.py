"""Local, read-only networking diagnostics. Never creates permissions or sends."""
from __future__ import annotations

from collections import Counter
import re

from . import store as s


def _error_code(value):
    """Only known categories from persisted state may appear in diagnostics."""
    if not isinstance(value, str):
        return 'other' if value else None
    known = {'ValueError', 'TypeError', 'RuntimeError', 'TimeoutError', 'GmailReadError',
             'GmailTransportError', 'LLMError', 'ProviderError', 'authentication', 'permission',
             'provider_error', 'mcp_error', 'invalid_json', 'invalid_schema', 'rate_limit',
             'invalid_page', 'not_found', 'invalid_message_list', 'message_identity_mismatch',
             'sync_errors', 'sync_incomplete', 'inbox_backlog', 'mailbox_busy',
             'shared_resource_capacity', 'search_not_ready', 'research_unavailable',
             'classifier_invalid_json', 'classifier_invalid_object', 'classifier_unsupported_event',
             'classifier_invalid_confidence', 'classifier_ungrounded_evidence'}
    return value if value in known else 'other' if value else None


def _inbox_health(conn, sender):
    """Separate successful Gmail scanning from unfinished message processing."""
    from applypilot.tracking.feedback_inbox import mailbox_backlog
    try:
        identity = s.digest(s.email_address(sender))
    except (ValueError, TypeError):
        return {'known_mailbox': False, 'reason_code': 'missing_sender'}
    mailbox = conn.execute('''SELECT last_complete_at,coverage_end,last_error
        FROM feedback_mailboxes WHERE mailbox_key=?''', (identity,)).fetchone()
    if mailbox is None:
        return {'known_mailbox': False, 'reason_code': 'mailbox_not_synced'}
    failures = []
    for row in conn.execute('''SELECT last_error,COUNT(*),MIN(attempts),MAX(attempts)
            FROM feedback_messages WHERE mailbox_key=? AND state='pending'
            GROUP BY last_error''', (identity,)):
        failures.append({'code': _error_code(row[0]), 'messages': row[1],
                         'min_attempts': row[2], 'max_attempts': row[3]})
    review = conn.execute("""SELECT COUNT(*) FROM feedback_messages WHERE mailbox_key=?
        AND state='review' AND link_reason='classifier_review'""", (identity,)).fetchone()[0]
    gate = {}
    backlog = mailbox_backlog(conn, identity)
    return {'known_mailbox': True, 'scan_complete': bool(mailbox[0]),
            'last_complete_at': mailbox[0], 'coverage_end': mailbox[1],
            'scan_error': _error_code(mailbox[2]), **backlog,
            'processing_errors': failures, 'classification_review': review,
            'last_write_gate': {'reason': _error_code(gate.get('reason')),
                                'errors': gate.get('errors') if type(gate.get('errors')) is int else 0}}


def diagnose(conn, *, cfg=None, gmail=None):
    from applypilot import config
    from applypilot.unattended.settings import load
    from applypilot.referrals.autonomy import grant
    from applypilot.onboarding.gmail_state import status
    from applypilot.onboarding.resolution import application_values
    cfg = load() if cfg is None else cfg
    gmail = status(config.APP_DIR, application_values(config.APP_DIR)[0]) if gmail is None else gmail
    policy = s.policy(conn)
    issues = []

    def issue(code, scope, action):
        issues.append({'code': code, 'scope': scope, 'action': action})

    # Identifiers here are literals, never operator- or website-supplied SQL.
    def grouped(table, column):
        return {row[0]: row[1] for row in conn.execute(
            f'SELECT {column},COUNT(*) FROM {table} GROUP BY {column}')}

    from applypilot.discovery.admission import current_admission_sql
    admission = current_admission_sql(stage='prepare')
    eligible = conn.execute(f"""SELECT COUNT(*),COUNT(*) FILTER (WHERE NOT EXISTS(
        SELECT 1 FROM referral_job_links l WHERE l.job_url=j.url))
        FROM jobs j WHERE {admission} AND j.fit_score>=?
        AND COALESCE(j.state,'')!='archived'""", (policy['min_fit_score'],)).fetchone()
    cases = grouped('referral_cases', 'state')
    outbox = grouped('referral_outbox', 'state')
    research = {}
    leads = grouped('referral_leads', 'state')
    contacts = conn.execute("""SELECT COUNT(*),
        COUNT(*) FILTER (WHERE blocked_at IS NULL),
        COUNT(*) FILTER (WHERE blocked_at IS NULL AND email_allowed=1 AND email IS NOT NULL
                        AND email_basis!='manual_only')
        FROM referral_contacts""").fetchone()
    with_candidates = sum(bool(s.obj(row[0]).get('candidates')) for row in conn.execute(
        "SELECT plan_json FROM referral_cases WHERE state IN ('research','draft')"))
    reasons = Counter()
    stalled_tasks = []

    if not eligible[0]:
        issue('no_currently_admitted_high_fit_jobs', 'preparation', 'Refresh/score eligible postings in the active search context. Do not bypass admission or fabricate eligibility.')
    elif not sum(cases.values()):
        issue('cases_not_planned', 'preparation', 'Run a bounded referrals plan pass after checking planning budgets; active applications remain protected.')
    if policy['mode'] == 'off' and not cfg['enabled']:
        issue('referrals_disabled', 'preparation', 'Outreach is off. Check APPLYPILOT_OUTREACH_ENABLED and the saved networking settings.')
    if any(policy[k] == 0 for k in ('plan_limit', 'daily_plan_limit', 'contacts_per_job')):
        issue('preparation_paused', 'preparation', 'Review zero planning/contact budgets in the saved referral policy.')
    if not cfg['enabled']:
        issue('automatic_discovery_disabled', 'research', 'Outreach is off. Discovery and applications can continue independently.')
    if any(v == 0 for v in (cfg['daily_research_limit'], cfg['research_jobs_per_cycle'], policy['daily_search_limit'])):
        issue('research_paused', 'research', 'Review zero research budgets; retry does not override them.')
    if sum(cases.values()) and not with_candidates:
        issue('no_ranked_contacts', 'preparation', 'Inspect research result counts, verify professional sources, or import reviewed contacts. Never guess emails.')
    if research.get('blocked') or any(code in reasons for code in ('no_people_found','no_verified_contacts','research_unavailable','empty','deferred')):
        issue('research_needs_attention', 'research', 'Repair the provider/account or import contacts, then use referrals retry-research --limit 20 for eligible empty cases.')
    if policy['mode'] == 'draft':
        issue('draft_only', 'delivery', 'Only drafts are being prepared. Start networking to use your saved outreach settings.')
    if policy['daily_send_limit'] == 0 or cfg['daily_send_limit'] == 0:
        issue('delivery_paused', 'delivery', 'Sending is paused by a zero daily message limit. Resume from Outreach settings.')
    g = grant(conn)
    sender = g['sender'] if g else gmail.get('mailbox', '')
    if not sender:
        try:
            profile = config.load_profile()
            sender = (profile.get('personal') or {}).get('email', '')
        except (OSError, ValueError, AttributeError):
            sender = ''
    inbox = _inbox_health(conn, sender)
    grant_state = 'missing'
    if g:
        rules = g['rules']
        if rules.get('version') == 2:
            grant_state = 'matches_policy' if rules.get('policy_hash') == s.digest(s.dump(cfg)) else 'policy_changed'
        else:
            grant_state = 'legacy_email_only'
    if cfg['enabled'] and grant_state in ('missing','policy_changed'):
        issue('networking_not_started', 'delivery', 'Start or restart networking to load your saved account settings. No separate authorization is needed.')
    if 'email' in cfg['channels']:
        if gmail.get('reconnect_required'):
            issue('gmail_reconnect_required', 'email', 'Stop workers and reconnect the intended account in Setup > Gmail, then verify.')
        elif not gmail.get('configured'):
            issue('gmail_not_configured', 'email', 'Configure Gmail through Setup using the intended profile mailbox.')
        elif not inbox.get('known_mailbox') or not inbox.get('last_complete_at'):
            issue('gmail_live_check_required', 'email', 'Local credential presence is not a health check. Run unattended check --live; no test email is sent.')
        if inbox.get('backlog'):
            issue('inbox_processing_backlog', 'email',
                  'Email delivery is paused until pending/retry messages are processed. Inspect processing_errors and retry times; a successful Gmail connection does not clear this backlog.')
        if inbox.get('known_mailbox') and not inbox.get('scan_complete'):
            issue('inbox_scan_incomplete', 'email', 'Resume the bounded mailbox sync to finish its saved scan window before email delivery.')
        if inbox.get('classification_review'):
            issue('inbox_classification_review', 'feedback',
                  'Review messages whose classification could not be grounded. No application outcome was inferred from them.')
    if outbox.get('sending') or outbox.get('uncertain'):
        issue('delivery_reconciliation_required', 'delivery', 'Reconcile existing attempted messages read-only. Never reset them to draft or resend blindly.')
    return {'read_only': True, 'sent': 0, 'mode': policy['mode'],
            'funnel': {'currently_admitted_high_fit_jobs': eligible[0], 'unplanned_admitted_high_fit_jobs': eligible[1],
                       'cases_by_state': cases, 'leads_by_state': leads,
                       'contacts_total': contacts[0], 'contacts_unblocked': contacts[1],
                       'professional_email_contacts': contacts[2], 'cases_with_ranked_contacts': with_candidates,
                       'research_by_state': research, 'research_results': dict(reasons),
                       'outbox_by_state': outbox},
            'standing_permission': grant_state,
            'gmail': {key: gmail.get(key) for key in ('configured','reconnect_required','verified','message')},
            'inbox_processing': inbox,
            'budgets': {**{k: policy[k] for k in ('plan_limit','daily_plan_limit','contacts_per_job','daily_send_limit','daily_search_limit')},
                        'unattended_daily_send_limit': cfg['daily_send_limit'],
                        'unattended_daily_research_limit': cfg['daily_research_limit']},
            'tasks_in_backoff': stalled_tasks, 'attention': issues}

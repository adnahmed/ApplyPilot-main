"""User-facing work descriptions derived from reported stages and states."""
from __future__ import annotations

from collections import Counter

from applypilot.stage_catalog import STAGES


_CATALOG = {stage: label for _, label, stage, *_ in STAGES}
_ACTIVITIES = {
    'applications': ('Applications', 'Applying for a job', 'Open the application and complete the employer’s form.'),
    'discovery': ('Job discovery', 'Finding job openings', 'Search connected sources for new opportunities.'),
    'enrich': ('Job research', 'Reading job requirements', 'Read the job posting and extract requirements and application details.'),
    'score': ('Scoring', 'Scoring job fit', 'Compare the role with your profile and score the match.'),
    'tailor': ('Résumé tailoring', 'Tailoring your résumé', 'Adapt your résumé to this role using your saved experience and evidence.'),
    'cover': ('Cover letters', 'Writing a cover letter', 'Prepare a cover letter for this role.'),
    'pdf': ('Documents', 'Generating application documents', 'Render the prepared application documents as PDFs.'),
    'brief': ('Interview preparation', 'Preparing an interview brief', 'Prepare talking points and background for this role.'),
    'company_research': ('Company research', 'Researching the company', 'Gather company background for your job search.'),
    'network_research': ('Networking research', 'Researching people to contact', 'Find and verify potential contacts for this opportunity.'),
    'referrals': ('Referral planning', 'Planning referral outreach', 'Prepare referral cases and outreach plans.'),
    'outreach': ('Outreach', 'Sending outreach', 'Process messages that are eligible for delivery.'),
    'tracking': ('Inbox tracking', 'Checking replies and application updates', 'Read your inbox for recruiter replies and application updates.'),
    'unattended': ('Networking maintenance', 'Checking networking follow-ups', 'Coordinate networking research, conversations, and due outreach.'),
    'backup': ('Backups', 'Backing up your workspace', 'Save a recovery copy of your workspace.'),
    'evidence': ('Repository evidence', 'Extracting repository evidence', 'Read repository source files and extract supported claims.'),
}
_STATES = {
    'running': ('running', 'Working'),
    'waiting_provider': ('waiting', 'Waiting for AI response'),
    'waiting_capacity': ('waiting', 'Waiting for AI capacity'),
    'queued': ('waiting', 'Queued'),
    'waiting': ('waiting', 'Waiting'),
    'idle': ('waiting', 'Waiting for next task'),
    'blocked': ('waiting', 'Blocked'),
    'unverified': ('unverified', 'Activity unknown'),
    'stopped': ('ended', 'Ended'),
}
_PHASES = {
    'working': 'Task in progress',
    'navigating': 'Opening the job page',
    'reading_page': 'Reading the page',
    'saving': 'Saving the result',
    'checking_inbox': 'Checking the inbox',
    'dask_running': 'Starting the application task',
    'queued': 'Waiting to start',
    'initializing': 'Starting the application worker',
    'selecting': 'Checking the next eligible job',
    'no_selectable_job': 'Waiting for an eligible job',
    'browser_unavailable': 'Waiting for browser access',
    'linkedin_cooldown': 'Waiting for the LinkedIn cooldown',
}
_GENERIC_OPERATIONS = {'job', 'document', 'case', 'cycle', 'run', 'sync', 'prepare', 'deliver', 'fetch_page'}


def activity(row):
    stage = row.get('stage') or row.get('component') or ''
    operation = row.get('operation') or ''
    if stage in _ACTIVITIES:
        category, title, purpose = _ACTIVITIES[stage]
        if stage == 'network_research' and operation == 'connections':
            title, purpose = 'Researching your connections', 'Read your network to find potential introductions.'
    else:
        category = (_CATALOG.get(stage) or str(stage).replace('_', ' ').capitalize()
                    or row.get('category') or 'Other work')
        title = (row.get('action') or (operation if operation not in _GENERIC_OPERATIONS else '')
                 or category)
        purpose = ''

    state = row.get('state') or 'unknown'
    if row.get('execution_alive') is False:
        state = 'stopped'
    elif row.get('fresh') is False and state != 'stopped':
        state = 'unverified'
    elif row.get('active') is False and state in {'running', 'waiting_provider', 'waiting_capacity'}:
        state = 'idle'
    bucket, status = _STATES.get(state, ('unverified', 'Activity unknown'))
    in_progress = state in {'running', 'waiting_provider', 'waiting_capacity'}
    detail = _PHASES.get(row.get('phase'), 'Task in progress') if state == 'running' else status
    if state == 'waiting_provider' and row.get('model_call_state') == 'deferred':
        detail = 'Waiting for an available AI provider'
    target = row.get('job_title') or row.get('target') or ''
    company = row.get('company') or ''
    if not in_progress and (row.get('last_job_title') or row.get('last_company')):
        target = row.get('last_job_title') or target
        company = row.get('last_company') or company
    return {'category': category, 'title': title, 'purpose': purpose, 'state': state,
            'bucket': bucket, 'status': status, 'detail': detail, 'in_progress': in_progress,
            'target': target, 'company': company}


def activity_counts(rows):
    """Count tasks in progress, including model waits, by their actual purpose."""
    return dict(Counter(item['category'] for row in rows
                        if (item := activity(row))['in_progress']))

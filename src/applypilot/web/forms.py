"""Server-rendered forms for the existing dashboard action services."""
from __future__ import annotations

import json


def field(name, label=None, value='', kind='text', options=None, required=False, **attributes):
    if kind == 'list' and isinstance(value, (list, tuple)):
        value = '\n'.join(str(item) for item in value)
    return {'name': name, 'label': label or name.replace('_', ' ').split('.')[-1].capitalize(),
            'value': value if value is not None else '', 'kind': kind, 'options': options or [],
            'group': (name.split('.')[1].replace('_', ' ').title() if name.startswith('profile.') and '.' in name[8:] else ''),
            'nullable': value is None, 'required': required, **attributes}


def form(identifier, title, route, fields=(), payload=None, *, submit='Save changes', note='', confirm='', advanced=False):
    return {'id': identifier, 'title': title, 'route': route, 'fields': list(fields),
            'payload': payload or {}, 'submit': submit, 'note': note, 'confirm': confirm, 'advanced': advanced}


def object_fields(value, prefix='', *, depth=0):
    output = []
    for key, item in value.items():
        name = prefix + key
        if isinstance(item, dict) and depth < 3:
            output.extend(object_fields(item, name + '.', depth=depth + 1))
        elif isinstance(item, bool):
            output.append(field(name, name.replace('_', ' ').replace('.', ' / '), item, 'bool'))
        elif isinstance(item, (int, float)):
            output.append(field(name, name.replace('_', ' ').replace('.', ' / '), item, 'number'))
        elif isinstance(item, list) and all(isinstance(x, str) for x in item):
            output.append(field(name, name.replace('_', ' ').replace('.', ' / '), '\n'.join(item), 'list'))
        elif isinstance(item, (list, dict)):
            output.append(field(name, name.replace('_', ' ').replace('.', ' / '), json.dumps(item, indent=2), 'json'))
        else:
            output.append(field(name, name.replace('_', ' ').replace('.', ' / '), item,
                                'textarea' if isinstance(item, str) and ('\n' in item or len(item) > 150) else 'text'))
    return output


def profile_forms(profile, notes):
    from applypilot.compensation import PAY_PERIODS
    from applypilot.onboarding.planning import availability_preferences
    from applypilot.discovery.countries import COUNTRY_CODES, COUNTRY_NAMES
    from applypilot.profile import citizenship_countries
    country_labels = {code: name.title() for name, code in reversed(list(COUNTRY_NAMES.items()))}
    def item(section, key, label, kind='text', **attrs):
        value = (profile.get(section) or {}).get(key)
        if section == 'experience' and key == 'current_job_title' and value is None:
            value = (profile.get(section) or {}).get('current_title')
        if kind == 'list':
            value = '\n'.join(value or []) if isinstance(value, list) else value or ''
        return field(f'profile.{section}.{key}', label, value, kind, **attrs)
    def group(identifier, title, fields, **kwargs):
        return form(identifier, title, 'setup/save-profile', fields, **kwargs)
    identity = [field('profile.personal.citizenship', 'Nationality / citizenship',
        list(citizenship_countries((profile.get('personal') or {}).get('citizenship'))),
        'multiselect', sorted([(code, country_labels.get(code, code)) for code in COUNTRY_CODES], key=lambda pair: pair[1]),
        group='Nationality',
        help='Choose every country where you hold citizenship. Saving adds editable defaults: authorized to work, with no sponsorship needed. Your existing country answers are kept.')]
    identity.extend(item('personal', key, label, kind, group='Personal details') for key, label, kind in (
        ('full_name', 'Full name', 'text'), ('preferred_name', 'Preferred name', 'text'),
        ('email', 'Email address', 'email'), ('phone', 'Phone', 'tel')))
    identity.extend(item('personal', key, label, kind, group='Where you live') for key, label, kind in (
        ('city', 'City', 'text'), ('province_state', 'State or province', 'text'),
        ('country', 'Country of residence', 'text')))
    identity.extend(item('personal', key, label, 'url', group='Public profiles') for key, label in (
        ('linkedin_url', 'LinkedIn profile'), ('github_url', 'GitHub profile'), ('portfolio_url', 'Portfolio')))
    experience = [item('experience', key, label, kind) for key, label, kind in (
        ('current_job_title', 'Current or most recent role', 'text'), ('current_company', 'Company', 'text'),
        ('years_of_experience_total', 'Years of professional experience', 'text'),
        ('education_level', 'Education', 'text'), ('professional_summary', 'Professional background', 'textarea'),
        ('capabilities', 'Capabilities', 'list'), ('domains', 'Areas of experience', 'list'))]
    for key, label in (('programming_languages', 'Programming languages'), ('frameworks', 'Frameworks'), ('tools', 'Tools')):
        experience.append(item('skills_boundary', key, label, 'list'))
    pay = [item('compensation', 'current_pay_amount', 'Current actual pay', 'number', min=0, step='any'),
           item('compensation', 'current_pay_currency', 'Pay currency', placeholder='e.g. PKR or USD', maxlength=3),
           item('compensation', 'current_pay_period', 'Pay period', 'select',
                options=[('', 'Choose a period')] + [(x, x.capitalize()) for x in PAY_PERIODS]),
           item('compensation', 'market_flexibility_percent', 'Negotiation room below the job’s market estimate (%)',
                'number', min=0, max=100, step='any')]
    available = availability_preferences(profile.get('availability'))
    availability = [field('profile.availability.mode', 'When can you start?', available.get('mode', ''), 'select',
                        [('', 'Choose availability'), ('immediate', 'Immediately'), ('date', 'On a specific date'),
                         ('notice', 'After a notice period')]),
                    field('profile.availability.start_date', 'Earliest start date', available.get('start_date'), 'date',
                          when={'field': 'profile.availability.mode', 'value': 'date'}),
                    field('profile.availability.notice_days', 'Notice period in days', available.get('notice_days'), 'number', min=1, step=1,
                          when={'field': 'profile.availability.mode', 'value': 'notice'})]
    answers = [('', 'Not recorded'), ('yes', 'Yes'), ('no', 'No')]
    work = profile.get('work_authorization') or {}
    work_fields = []
    for code, facts in (work.get('by_country') or {}).items():
        if code not in COUNTRY_CODES or not isinstance(facts, dict):
            continue
        for key, alias, label in (('legally_authorized_to_work', 'legally_authorized', 'Authorized to work'),
                                  ('require_sponsorship', 'needs_sponsorship', 'Needs sponsorship')):
            value = facts.get(key, facts.get(alias))
            value = 'yes' if value is True else 'no' if value is False else value or ''
            work_fields.append(field(f'profile.work_authorization.by_country.{code}.{key}', label, value, 'select', answers,
                group=country_labels.get(code,code),
                help='Default from your citizenship. You can change this.' if key in (facts.get('citizenship_defaults') or []) else ''))
    if not work_fields:
        for key, alias, label in (('legally_authorized_to_work', 'legally_authorized', 'Previously recorded authorization (country unspecified)'),
                                  ('require_sponsorship', 'needs_sponsorship', 'Previously recorded sponsorship need (country unspecified)')):
            if key not in work and alias not in work:
                continue
            value = work.get(key, work.get(alias))
            value = 'yes' if value is True else 'no' if value is False else value or ''
            work_fields.append(field(f'profile.work_authorization.{key}', label, value, 'select', answers))
    result = [group('profile-identity', 'About you', identity,
                    note='Missing details fill automatically from your saved material. Your own answers always take priority.'),
              group('profile-experience', 'Experience and skills', experience),
              group('profile-pay', 'Pay and flexibility', pay,
                    note='Current pay is a fact about your present compensation. Expected pay is estimated for each role and market. Flexibility sets how far below that estimate you are willing to negotiate; it does not lower your opening expectation.'),
              group('profile-availability', 'Availability', availability,
                    note=('Saved availability: ' + str(available.get('earliest_start_date'))
                          if not available.get('mode') and available.get('earliest_start_date') else '')),
              group('profile-authorization', 'Work authorization', work_fields,
                    note='Citizenship adds editable authorization and sponsorship defaults. Add other countries below to record their work permissions.'),
              group('profile-background', 'Background and experience notes',
                    [field('resume_text', 'Background notes', notes, 'textarea')],
                    note='One input alongside your profile, achievements, and project evidence. Each application gets its own tailored résumé.')]
    if not work_fields:
        result = [row for row in result if row['id'] != 'profile-authorization']
    result.insert(-1, form('authorization-country', 'Add a country', 'setup/work-authorization', [
        field('country', 'Country', '', 'select', [('', 'Choose a country')] + sorted([(code, country_labels.get(code, code)) for code in COUNTRY_CODES if code not in (work.get('by_country') or {})], key=lambda pair: pair[1]), required=True),
        field('legally_authorized_to_work', 'Can you legally work there now?', '', 'select', answers),
        field('require_sponsorship', 'Would an employer need to sponsor you?', '', 'select', answers)],
        submit='Save country authorization', note='Keep a separate answer for each country. Choose “Not recorded” when you do not know.'))
    return result


def build(view, values):
    if view == 'setup':
        return []
    output = []
    state = values.get('setup', {})
    draft = state.get('draft', {})
    env = values.get('setup/connections', {})
    settings = values.get('settings', {})
    if view in {'settings', 'outreach'}:
        output.append(form('outreach-settings', 'Outreach limits', 'settings', [
            field(key, label, settings.get(key, -1), 'number' if key == 'send_grace_seconds' else 'limit', min=0) for key, label in (
                ('daily_send_limit', 'Messages per day'), ('daily_research_limit', 'Research jobs per day'),
                ('contacts_per_job', 'Contacts per opportunity'), ('send_grace_seconds', 'Cancellation window in seconds'))],
            {'revision': settings.get('revision')}, note='Choose uncapped, paused, or a specific limit.'))
    if view == 'profile':
        saved = values.get('setup/profile', draft)
        output.extend(profile_forms(saved.get('profile') or {}, saved.get('resume_text', '')))
    if view == 'searches':
        from .search_forms import search_forms
        saved = values.get('setup/searches', draft)
        output.extend(search_forms(saved))
        policy = values.get('setup/runtime-policy', {})
        runtime = policy.get('policy') or {}
        research = runtime.get('research') or {}
        output.append(form('runtime-policy', 'Discovery and research limits', 'setup/runtime-policy',
            [field('policy.' + key, label, runtime.get(key, -1), 'limit') for key, label in (
                ('request_budget', 'Total requests per discovery pass'), ('http_budget', 'Direct page requests'),
                ('browser_budget', 'Browser requests'), ('model_budget', 'AI requests'))] + [
                field('policy.research.enabled', 'Research companies', research.get('enabled', True), 'bool'),
                field('policy.research.companies', 'Companies per research pass', research.get('companies', -1), 'limit')],
            {'revision': policy.get('revision')}, note='Uncapped keeps working while requests are available. Paused stops that type of work.'))
        options = values.get('autopilot/options', {})
        output.append(form('fitness-score', 'Which opportunities should move forward?', 'autopilot/options', [
            field('min_score', 'Minimum job fit score', options.get('min_score', 8), 'number', min=1, max=10, step=1)],
            note='Opportunities below this score stay in your history. New preparation and applications use the threshold on the next start or resume.'))
    if view in {'evidence', 'setup'}:
        output.append(form('intake', 'Import your material', 'setup/task/intake', [
            field('upload', 'PDF, DOCX, or text file', kind='file'),
            field('text', 'Or paste your material', kind='textarea'),
            field('auto_suggest', 'Suggest profile and search improvements', True, 'bool'),
            field('share_with_model', 'Share with the configured model', True, 'bool')],
            payload={'kind': 'supporting'}, submit='Import material',
            note='Add a résumé, project notes, a review, or other evidence of your work. Choose a file or paste text.'))
        identity = values.get('setup/profile', {})
        output.append(form('github-material', 'Import GitHub project evidence', 'setup/task/github', [
            field('identity', 'GitHub username or URL', identity.get('github_identity') or (identity.get('profile', {}).get('personal') or {}).get('github_url', ''), help='Uses your connected account when left empty.'), field('limit', 'Repositories to inspect',3,'number', min=1),
            field('workers', 'Repositories at once',2,'number'),
            field('include_private','Include private repositories',True,'bool'),field('use_ai','Analyze with AI',True,'bool'),
            field('share_with_model','Share with configured model',True,'bool')], submit='Import GitHub projects'))
    if view in {'connections', 'capacity', 'setup'}:
        selected = [item for item in env.get('fields', [])
                    if not item.get('external') and not item.get('local_only')
                    and item['key'] != 'APPLYPILOT_DB_POOL_SIZE'
                    and not item['key'].startswith('APPLYPILOT_BACKUP_')]
        capacity_keys = {'APPLYPILOT_RESOURCE_WORKERS': 'Shared task workers',
                         'APPLYPILOT_BROWSER_WORKERS': 'Simultaneous browser tasks',
                         'APPLYPILOT_PREPARATION_MAX_PENDING': 'Maximum pending preparation tasks'}
        if view == 'capacity':
            selected = [x for x in selected if x['key'] in capacity_keys]
        else:
            connection_keys = {'LLM_URL', 'LLM_API_KEY', 'BROWSEROS_MCP_URL', 'APPLYPILOT_BROWSER_RUNTIME',
                'APPLYPILOT_AGENT_BASE_URL', 'APPLYPILOT_AGENT_API_KEY', 'APPLYPILOT_SEARCH_BACKEND',
                'APPLYPILOT_CRAWLER', 'SEARXNG_URL', 'APPLYPILOT_ALERT_WEBHOOK',
                'FREEHIRE_ENABLED', 'FREEHIRE_TOKEN', 'FREEHIRE_API_URL'}
            selected = [x for x in selected if x['key'] in connection_keys]
        groups = {}
        core = {'LLM_URL', 'APPLYPILOT_AGENT_BASE_URL', 'APPLYPILOT_BROWSER_RUNTIME',
                'APPLYPILOT_BROWSEROS_URL', 'BROWSEROS_URL', 'BROWSEROS_MCP_URL'}
        for item in selected:
            key = item['key']
            group = 'Concurrent work' if view == 'capacity' else 'FreeHire' if key.startswith('FREEHIRE_') else 'Core connections' if key in core else {
                'llm': 'Model gateway', 'browser': 'Application browser', 'gmail': 'Gmail',
                'github': 'GitHub', 'sources': 'Search and profile readers', 'alerts': 'Notifications',
            }.get(item.get('group'), 'Other connections')
            choices = [(value, value.capitalize()) for value in item.get('choices', [])]
            kind = 'number' if key in capacity_keys else 'password' if item.get('secret') else 'select' if choices else 'text'
            groups.setdefault(group, []).append(field('values.' + key, capacity_keys.get(key) or item.get('label') or key,
                '' if item.get('secret') else item.get('value', ''), kind, options=choices,
                min=1 if key in capacity_keys else None, step=1,
                placeholder=item.get('placeholder', ''), help=item.get('hint', '') if key in capacity_keys else ''))
        for group, fields in sorted(groups.items(), key=lambda pair: (pair[0] != 'Core connections', pair[0])):
            output.append(form('connections-' + group.lower().replace(' ', '-'), group, 'setup/connections',
                fields, {'revision': env.get('revision')}, advanced=group not in {'Core connections','Concurrent work'},
                note='Leave a password empty to keep its saved value.' if any(f['kind'] == 'password' for f in fields) else ''))
    if view in {'connections', 'setup'}:
        for provider, label in [('linkedin_scraper', 'LinkedIn'), ('twikit', 'X / Twitter')]:
            output.append(form('connect-' + provider, label, 'setup/task/social_connect',
                [field('reconnect', 'Replace saved login', False, 'bool')],
                {'provider': provider, 'browser_backend': 'cloakbrowser', 'consent': True}, submit='Connect ' + label))
            output.append(form('import-' + provider, 'Import ' + label + ' session', 'setup/import-session',
                [field('upload', 'Session file', kind='file')], {'provider': provider, 'consent': True}, submit='Import session'))
        output.append(form('gmail', 'Gmail', 'setup/task/gmail_connect',
                           [field('email', 'Gmail address', state.get('gmail', {}).get('mailbox', ''), 'email')],
                           {'consent': True}, submit='Connect Gmail'))
        output.append(form('gmail-client', 'Gmail OAuth client', 'setup/gmail-client',
                           [field('upload', 'Desktop OAuth JSON file', kind='file')], submit='Save client'))
        output.append(form('github', 'GitHub account', 'setup/task/github_connect',
                           [field('reconnect', 'Sign in again', False, 'bool')], {'consent': True}, submit='Connect GitHub'))
        output.append(form('mcporter', 'Discover configured MCP tools', 'setup/task/mcporter_scan', submit='Scan tools'))
        output.append(form('mcp-auth', 'Sign in to an MCP service', 'setup/task/mcporter_auth',
                           [field('server', 'Configured server name', required=True)], {'consent': True}, submit='Connect service'))
        output.append(form('mcp-bind', 'Bind MCP capability', 'setup/mcp-binding', [
            field('provider', 'Provider'), field('operation', 'Capability'), field('tool', 'Configured tool name')]))
        for component, label in (('llm', 'model'), ('gmail', 'Gmail'), ('sources', 'sources'), ('linkedin','LinkedIn'), ('x','X'),
                                 ('browseros', 'browser'), ('github', 'GitHub')):
            output.append(form('verify-' + component, 'Verify ' + label, 'setup/task/verify',
                               payload={'component': component}, submit='Verify connection'))
    if view == 'capacity':
        queue = values.get('queue', {})
        buffer_fields = [field('buffer.revalidate_after_days', 'Recheck posting availability after (days)',
                              (queue.get('limits') or {}).get('revalidate_after_days', 7), 'number', min=1, step=1)]
        flow = queue.get('flow_settings') or {}
        buffers = flow.get('buffers') or {}
        mode = flow.get('mode', 'automatic')
        buffer_fields.append(field('flow_control.mode', 'Preparation capacity', mode, 'select', options=[
            ('automatic', 'Automatic — use available execution capacity'),
            ('watermarks', 'Manual — use queue watermarks')]))
        for name, label in (('score', 'Scored opportunities'), ('qualified', 'Qualified opportunities'),
                            ('cover', 'Cover letters'), ('documents', 'Application documents'), ('apply', 'Ready applications')):
            for key, action in (('low', 'Resume preparation below'), ('high', 'Pause preparation at')):
                if mode == 'watermarks' and key in buffers.get(name, {}):
                    buffer_fields.append(field(f'flow_control.buffers.{name}.{key}', f'{label}: {action.lower()}',
                                               buffers[name][key], 'number', min=0, max=100000, step=1))
        output.append(form('queue', 'Preparation buffers', 'queue/settings',
                           buffer_fields, {'revision': queue.get('revision'), 'flow_control': {'buffers': buffers}},
                           note='Automatic mode uses available execution capacity. Manual queue watermarks are optional; save manual mode to edit them.'))
    if view == 'backups':
        backup = values.get('backups', {})
        config = backup.get('config') or {}
        backup_fields = [field('enabled', 'Back up automatically', config.get('enabled', False), 'bool'),
            field('repository', 'Private GitHub repository', config.get('repository', ''), placeholder='owner/repository…', help='Leave empty to create applypilot-backup in your connected account when you enable backups.'),
            field('static_minutes', 'Save profile and settings every (minutes)', config.get('static_minutes', 5), 'number', min=1, max=1440),
            field('snapshot_hours', 'Take a full snapshot every (hours)', config.get('snapshot_hours', 6), 'number', min=1, max=168),
            field('keep_snapshots', 'Recovery points to keep', config.get('keep_snapshots', 30), 'number', min=3, max=365),
            field('max_snapshot_gb', 'Maximum snapshot size (GB)', config.get('max_snapshot_gb', 64), 'number', min=1, max=512),
            field('include_documents', 'Include generated documents', config.get('include_documents', True), 'bool'),
            field('auth_mode', 'GitHub sign-in', config.get('auth_mode', 'auto'), 'select',
                [('auto', 'Use connected account'), ('github_cli', 'GitHub CLI account'), ('token', 'Saved backup token')]),
            field('github_account', 'GitHub account (optional)', config.get('github_account', ''))]
        output.append(form('backup-settings', 'Backup settings', 'backups/settings',
                           backup_fields + [
                               field('confirm_private_upload', 'Authorize uploading private workspace data to this repository', False, 'bool')] ))
        for action, label in [('check','Check repository'), ('sync','Back up now'), ('points','List recovery points'), ('rollback','Roll back interrupted recovery')]:
            output.append(form('backup-' + action, label, 'backups/' + action, submit=label))
        output.append(form('backup-restore', 'Restore workspace', 'backups/restore', [
            field('tag', 'Recovery point (empty for latest)'), field('confirm_restore', 'Type RESTORE to confirm', required=True)],
            submit='Restore workspace', confirm='Replace this workspace with the selected recovery point?'))
    if view == 'diary':
        output.append(form('diary-note', 'Record your work', 'diary/note', [field('note', 'What happened?', kind='textarea', required=True)], submit='Save note'))
    if view == 'providers':
        value = values.get('providers', {})
        providers = value.get('providers', [])
        selected_provider = next((p for p in providers if p.get('id') == value.get('selected_provider')), next(iter(providers), {}))
        output.append(form('provider-selection', 'Provider preference', 'providers/selection', [
            field('enabled', 'Use this provider', selected_provider.get('enabled', True), 'bool')],
            {'provider': selected_provider.get('id',''), 'revision': value.get('revision')}, submit='Save provider preference'))
        if selected_provider:
            pid = selected_provider['id']
            credentials = selected_provider.get('credentials', [])
            configured = [row for row in credentials if row.get('configured')]
            models = sorted({row['id'] for credential in configured for row in credential.get('models',[]) if row.get('id')})
            if configured:
                output.append(form('probe-' + pid, 'Check a saved credential', 'providers/check', [
                    field('credential', 'Saved credential', configured[0]['slot'], 'select', [(row['slot'],row['slot']) for row in configured]),
                    field('model', 'Model', models[0] if models else '', 'select', [(model,model) for model in models], required=True)],
                    {'provider':pid}, submit='Check provider'))
            if credentials:
                output.append(form('credential-' + pid, 'Add or replace a credential', 'providers/credential', [
                    field('credential', 'Credential', credentials[0]['slot'], 'select',
                          [(row['slot'], row['slot'] + (' (saved)' if row.get('configured') else ' (not configured)')) for row in credentials]),
                    field('value', 'New credential value', kind='password', required=True)],
                    {'provider':pid, 'revision':value.get('credential_revision')}, submit='Save credential',
                    note='Saved values stay hidden. Select the credential you want to add or replace.'))
        output.append(form('reload-providers', 'Reload gateway', 'providers/reload',payload={'confirm':True},submit='Reload gateway'))
    if view == 'operations':
        from datetime import datetime, timezone
        runs = values.get('work/runs', {}).get('items', [])
        options = [('', 'All workspace activity')] + [(row['id'],
            datetime.fromtimestamp(row['started_at'], timezone.utc).astimezone().strftime('%d %b %Y, %H:%M %Z')
            + ' · ' + str(row.get('role') or 'Run') + ' · ' + row['id'][:8]) for row in runs]
        output.append(form('support', 'Download support ZIP', 'operations/support-bundle',
            [field('run_id', 'Include a run report (optional)', kind='select', options=options)], submit='Prepare support ZIP',
            note='Includes application and apply-worker logs, tool calls, agent traces, model failures, saved diagnostics, and database records. Selecting a run adds its report and prioritizes its logs. Credentials are redacted; collection limits and missing files are listed in the ZIP.'))
        value = values.get('operations', {})
        if isinstance(value.get('settings'), dict):
            output.append(form('operations-settings', 'Recorder settings', 'operations/settings', object_fields(value['settings'])))
        incidents = [item for item in value.get('incidents', [])
                     if item.get('fingerprint') and item.get('last_at')
                     and item['last_at'] > (item.get('acknowledged_at') or 0)]
        if incidents:
            options = [('', 'Choose an issue')] + [(
                json.dumps([{'fingerprint': item['fingerprint'], 'last_at': item['last_at']}]),
                (str(item.get('component') or item.get('stage') or 'System') + ': '
                 + str(item.get('message') or item.get('error_kind') or 'Recorded issue'))[:160]
            ) for item in incidents]
            selection = field('items', 'Issue', kind='select', options=options, required=True)
            selection['decode'] = 'json'
            output.append(form('incident-resolution', 'Review a recorded issue', 'operations/incident',
                [selection], {'action': 'acknowledge'}, submit='Mark reviewed', advanced=True,
                note='Marks this occurrence reviewed. New occurrences remain visible.'))
    if view == 'career':
        from applypilot.career_ops.store import MODES
        career = values.get('career-ops', {})
        selected = career.get('selected') or {}
        active_report = career.get('active_report') or {}
        continuation = active_report if active_report.get('state') == 'needs_input' else {}
        saved_pay = career.get('profile_pay', {})
        pay_note = ('Saved current pay: ' + saved_pay['current_amount'] + ' '
                    + saved_pay['current_currency'] + ' / ' + saved_pay['current_period'] + '. '
                    if all(saved_pay.get('current_' + key) for key in ('amount', 'currency', 'period'))
                    else 'Add current pay in Profile or supply it under compensation below. ')
        output.append(form('career-report', 'Complete ' + MODES[continuation['mode']] if continuation else 'Create a career report', 'career-ops/run', [
            field('retry_report_id', value=continuation.get('id', ''), kind='hidden'),
            field('mode', 'Workflow', continuation.get('mode', career.get('mode', 'evaluate')), 'hidden' if continuation else 'select', list(MODES.items()), required=True),
            field('job_url', 'Opportunity', selected.get('url', ''), 'opportunity',
                  [(row['url'], row['label']) for row in career.get('items', [])],
                  help='Uses its saved company, job description and application evidence. Search by role, company or URL.'),
            field('company_name', 'Company (custom opportunity)', when={'field': 'job_url', 'value': ''}, required=True),
            field('job_description', 'Job description', kind='textarea', maxlength=60000, rows=5,
                  when=None if continuation and not selected.get('has_description') else {'field': 'job_url', 'value': ''}),
            field('instructions', 'Your focus or application questions', kind='textarea', maxlength=16000, rows=3),
            field('notes', 'Interview notes or observed feedback', kind='textarea', maxlength=16000, rows=4),
            field('interview_date', 'Interview date (optional)', kind='date'),
            field('minutes', 'Preparation time in minutes', 60, 'number', min=15, max=720),
            field('offer_text', 'Offer or contract text (for offer review)', kind='textarea', maxlength=100000, rows=6),
            *[item for prefix, label in (('current', 'Current'), ('desired', 'Desired'), ('advertised', 'Advertised'), ('offer', 'Offered'))
              for item in (field(prefix + '_amount', label + ' pay amount (optional)', group='compensation',
                                 help='Leave all three current-pay fields empty to use your saved profile.' if prefix == 'current' else ''),
                           field(prefix + '_currency', label + ' currency (e.g. PKR, AED, USD)', group='compensation'),
                           field(prefix + '_period', label + ' pay period', '', 'select',
                                 [('', 'Unknown'), ('year', 'Year'), ('month', 'Month'), ('week', 'Week'), ('day', 'Day'), ('hour', 'Hour')], group='compensation'))]],
            submit='Prepare report', note='Your saved profile and evidence are used automatically. ' + pay_note
            + 'Optional compensation overrides apply only to this report.'))
    if view == 'company-discovery':
        from applypilot.company_discovery.store import KINDS
        settings = values.get('company-discovery', {}).get('settings', {})
        if settings:
            output.append(form('company-discovery', 'Company discovery', 'company-discovery/settings', [
                field('enabled', 'Discover companies automatically', settings['enabled'], 'bool'),
                field('discover_sources', 'Find new company sources', settings['discover_sources'], 'bool'),
                field('companies_per_source', 'Companies per source', settings['companies_per_source'], 'number', min=1, max=25, step=1),
                field('predict_enabled', 'Research future hiring needs', settings.get('predict_enabled', True), 'bool'),
                field('predict_drafts_per_cycle', 'Founder drafts per predictive research pass', settings.get('predict_drafts_per_cycle', 2), 'number', min=1, max=3, step=1),
            ], {'revision': settings['revision']}))
        output.append(form('company-source', 'Add a company source', 'company-discovery/source', [
            field('name', 'Name', required=True), field('url', 'Directory or feed URL', kind='url', required=True),
            field('kind', 'Source type', 'portfolio_directory', 'select', [(key, key.replace('_', ' ').capitalize()) for key in KINDS]),
            field('market', 'Market (optional)'),
        ], submit='Add source', note='New sources are checked for relevant companies and current evidence before use.'))
    if view == 'rlm':
        status = values.get('rlm/status', {})
        configured = status.get('configured', {})
        if configured:
            output.append(form('rlm', 'RLM settings', 'rlm/settings', [
                field('enabled', 'Enable recursive context analysis', configured['enabled'], 'bool'),
                field('threshold', 'Context threshold (characters)', configured['threshold'], 'number', min=1000, max=1000000, step=1),
                field('max_calls', 'Gateway calls per invocation', configured['max_calls'], 'number', min=1, max=64, step=1),
                field('max_iterations', 'Iterations', configured['max_iterations'], 'number', min=1, max=64, step=1),
                field('parallel_calls', 'Parallel subcalls', configured['parallel_calls'], 'number', min=1, max=4, step=1),
                field('timeout', 'Timeout (seconds; 0 = no deadline)', configured['timeout'], 'number', min=0, step=1),
            ], {'revision': status['revision']}, note='Existing workers use these settings after restart. Learning also has a shared round budget.'))
    if view == 'settings':
        learning = values.get('learning/status', {})
        learning_settings = learning.get('settings') or {}
        learning_mode = ('capture' if learning_settings.get('enabled') is False else
                         'optimize' if learning_settings.get('auto_promote') is False else 'full')
        output.append(form('learning', 'Learning', 'learning/settings',
                           [field('mode', 'Learning mode', learning_mode, 'select',
                                  [('full','Learn and promote'),('optimize','Learn; promote manually'),
                                   ('capture','Record examples only')])], {'confirm': True}))
        output.append(form('learning-import', 'Review a saved learning policy', 'learning/preview-policy', [
            field('upload', 'ApplyPilot policy export (.json)', kind='file', required=True)], submit='Preview changes',
            note='Policies are learned instructions for tasks such as scoring and writing. Upload an ApplyPilot export to compare it with your installed instructions before replacing anything.'))
    advanced = {'reset-draft','restore-setup','backup-restore','backup-rollback',
                'mcp-auth','mcp-bind','mcporter','gmail-client','operations-settings'}
    for item in output:
        if item['route'] == 'setup/draft' and draft:
            item['payload'].setdefault('revision', draft.get('revision'))
        if item['id'] in advanced or item['id'].startswith('import-'):
            item['advanced'] = True
    return output

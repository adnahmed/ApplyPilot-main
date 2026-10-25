"""Recipient-facing exports from existing disclosure-approved evidence."""
from __future__ import annotations

from urllib.parse import urlsplit

from applypilot.discovery.repository import transaction

from . import store


def _link(value):
    value = str(value or '').strip()
    try:
        parsed = urlsplit(value)
        if (parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username
                and not parsed.password and not any(char.isspace() for char in value)):
            return value
    except ValueError:
        pass
    return ''


def build_brief(conn, job_url, profile=None, *, introduction='', records=None):
    """A fresh preview; missing permission or ownership never produces a claim."""
    from applypilot import config
    from applypilot.career.documents import document_context
    from applypilot.impact.ledger import default_repository
    from applypilot.tracking.feedback_store import obj

    if not isinstance(introduction, str) or len(introduction) > 4000:
        raise ValueError('Use at most 4,000 characters for the introduction')
    row = conn.execute('SELECT * FROM jobs WHERE url=?', (job_url,)).fetchone()
    if row is None:
        raise ValueError('Select an existing opportunity for the forwardable brief')
    job = dict(row)
    profile = config.load_profile() if profile is None else profile
    records = default_repository().list_records() if records is None else records
    sources = document_context('', profile, records, job)['sources']
    selected = {str(record['id']) for record in obj(job.get('selected_evidence')).get('records', [])
                if isinstance(record, dict) and record.get('id')}
    linked = {str(key) for requirement in obj(job.get('fit_requirements'), [])
              if isinstance(requirement, dict) for key in requirement.get('source_ids', [])}
    ranked = [(key, value) for key, value in sources.items()
              if key.startswith(('repository:', 'impact:', 'career:')) and value.get('text')]
    ranked.sort(key=lambda item: (item[0] not in linked, str(item[1].get('record_id')) not in selected))
    examples, seen = [], set()
    for key, source in ranked:
        identity = source.get('record_id') or key
        if identity in seen:
            continue
        seen.add(identity)
        examples.append({'source_id': key, 'title': source.get('header') or 'Work example',
                         'text': source['text'],
                         'attribution': str(source.get('attribution') or '').replace('_', ' '),
                         'url': _link(source.get('url')), 'revision': source.get('record_revision', ''),
                         'kind': key.split(':', 1)[0]})
        if len(examples) == 3:
            break
    name = str((profile.get('personal') or {}).get('full_name') or 'the candidate')
    title, company = str(job.get('title') or 'the role'), str(job.get('company') or 'the employer')
    intro = introduction.strip() or (
        f"I'd like to introduce {name} for {title} at {company}. "
        'The work examples below describe relevant contributions. Would you be open to a conversation?')
    fit = [example['text'] for example in examples]
    lines = [f'{name} — {title} at {company}', '', f'Role: {title}', f'Employer: {company}']
    if _link(job_url):
        lines.append(f'Posting: {_link(job_url)}')
    lines.extend(['', 'Why this work is relevant'])
    lines.extend(f'- {claim}' for claim in fit)
    if not fit:
        lines.append('No relevant work examples have been selected for this brief.')
    lines.extend(['', 'Supporting examples'])
    for index, example in enumerate(examples, 1):
        lines.extend(['', f"{index}. {example['title']}", example['text']])
        if example['attribution']:
            lines.append('Attribution: ' + example['attribution'])
        if example['url']:
            lines.append('Work link: ' + example['url'])
    lines.extend(['', 'Introduction (editable)', intro, ''])
    return {'format': 'applypilot-forwardable-brief/1', 'job_url': job_url,
            'title': title, 'company': company, 'candidate': name,
            'fit': fit, 'examples': examples, 'introduction': intro,
            'introduction_origin': 'candidate' if introduction.strip() else 'template',
            'missing_materials': [] if examples else ['No approved work examples are available.'],
            'source_ids': [example['source_id'] for example in examples],
            'document_context': {key: [example['source_id'] for example in examples]
                                 for key in ('supplied_source_ids', 'used_source_ids')},
            'markdown': '\n'.join(lines), 'generated_at': store.now()}


def save_brief(conn, job_url, profile=None, *, introduction=''):
    """Keep the exact exported prose in the existing report archive."""
    brief = build_brief(conn, job_url, profile, introduction=introduction)
    with transaction(conn, resource='career-ops'):
        report = store.start(conn, 'forwardable_brief', {'job_url': job_url, 'introduction': introduction})
        return store.finish(conn, report, brief)


def read_brief(conn, report_id):
    """Return the exact saved export, including its candidate-edited introduction."""
    from applypilot.career.documents import revoked_document_sources

    report = store.get(conn, report_id)
    if report['mode'] != 'forwardable_brief' or report['state'] != 'complete':
        raise ValueError('Select a completed forwardable brief')
    if revoked_document_sources(report['result'].get('document_context')):
        raise ValueError('Permission to share evidence in this brief was withdrawn; create a fresh brief')
    return report['result']

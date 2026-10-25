"""Complete empty profile fields from saved material, with visible task state."""
from __future__ import annotations

from datetime import UTC, datetime
from copy import deepcopy

from .storage import digest, now, read_json, setup_lock, write_json
from .workspace import material_sources, profile_value


FIELDS = (
    'personal.full_name', 'personal.preferred_name', 'personal.email', 'personal.phone',
    'personal.city', 'personal.province_state', 'personal.country',
    'personal.linkedin_url', 'personal.github_url', 'personal.portfolio_url',
    'experience.current_job_title', 'experience.current_company',
    'experience.education_level', 'experience.years_of_experience_total',
    'experience.professional_summary', 'experience.capabilities', 'experience.domains',
    'skills_boundary.programming_languages', 'skills_boundary.frameworks', 'skills_boundary.tools',
)


def sources_fingerprint(root):
    sources = [row for row in material_sources(root) if row.get('kind') in {'resume', 'supporting'}
               and (not row.get('evidence') or row.get('profile_eligible'))]
    content = [{'id': 'saved-profile-facts', 'facts': {}}]
    for row in sources:
        if row.get('typed_profile'):
            from .planning import FACT_PATHS
            from .workspace import PROFILE_ALIASES, canonical_profile
            facts = canonical_profile(deepcopy(row['payload']))
            excluded = set(FACT_PATHS)
            for path in FACT_PATHS:
                excluded.update(PROFILE_ALIASES.get(path, ()))
            for path in excluded:
                section, name = path.split('.', 1)
                if isinstance(facts.get(section), dict):
                    facts[section].pop(name, None)
            # Completion's own destinations cannot become a new source revision.
            facts = {section: values for section, values in facts.items() if values}
            content[0]['facts'] = facts
        else:
            content.append({'id': row['id'], 'text': row['text']})
    return digest({'fields': FIELDS, 'sources': content}) if sources else None


def remember(root, task_id, fingerprint=None):
    source_revision = fingerprint or sources_fingerprint(root)
    with setup_lock(root):
        saved = read_json(root / 'setup' / 'profile-completion.json', {})
        previous_id = saved.get('task_id')
        previous_task = read_json(root / 'setup' / 'tasks' / (previous_id + '.state.json'), {}) if previous_id else {}
        previous_result = previous_task.get('result') or saved.get('result') or saved.get('previous_result')
        write_json(root / 'setup' / 'profile-completion.json',
                   {'fingerprint': source_revision, 'task_id': task_id, 'started_at': now(),
                    **({'previous_result': previous_result} if isinstance(previous_result, dict) else {})})


def retryable_lists(root, profile, requested_paths):
    """Append only to a last automatic write that still exactly matches live data."""
    from .planning import LIST_FACTS
    saved = read_json(root / 'setup' / 'profile-completion.json', {})
    records = []
    # Older dashboard processes do not preserve previous_result when queuing.
    # Retained task results provide the same write provenance during that transition.
    paths = sorted((root / 'setup' / 'tasks').glob('*.state.json'),
                   key=lambda path: path.stat().st_mtime, reverse=True)
    for path in paths:
        record = read_json(path, {})
        result = record.get('result')
        if not isinstance(result, dict):
            continue
        if isinstance(result.get('suggestions'), dict):
            result = result['suggestions']
        if result.get('profile_filled'):
            records.append(result)
    for key in ('result', 'previous_result'):
        if isinstance(saved.get(key), dict):
            records.append(saved[key])
    eligible, seen = {}, set()
    for result in records:
        diagnostics = result.get('grounding_diagnostics') or []
        for change in result.get('profile_filled') or []:
            if not isinstance(change, dict):
                continue
            path = change.get('path')
            if path in seen or path not in requested_paths or path not in LIST_FACTS:
                continue
            seen.add(path)
            rejected = [row for row in diagnostics if isinstance(row, dict) and row.get('path') == path]
            value = change.get('value')
            if rejected and isinstance(value, list) and value and profile_value(profile, path) == value:
                eligible[path] = {'value': deepcopy(value), 'support': deepcopy(change.get('support') or []),
                                  'diagnostics': deepcopy(rejected)}
    return eligible


def completion(root, profile, jobs=None):
    missing = [path for path in FIELDS if profile_value(profile, path) in (None, '', [], {})]
    repair_fields = sorted(retryable_lists(root, profile, FIELDS))
    saved = read_json(root / 'setup' / 'profile-completion.json', {})
    tasks = jobs.status() if jobs is not None else []
    task_id = saved.get('task_id')
    task = read_json(root / 'setup' / 'tasks' / (task_id + '.state.json'), {}) if task_id else {}
    terminal = {'completed', 'failed', 'timed_out', 'interrupted', 'cancelled'}
    if task.get('state') in terminal and jobs is not None:
        cached = {key: task.get(key) for key in ('state', 'message', 'result', 'finished_at')}
        if any(saved.get(key) != value for key, value in cached.items()):
            with setup_lock(root):
                latest = read_json(root / 'setup' / 'profile-completion.json', {})
                if latest.get('task_id') == task_id:
                    saved.update(cached)
                    write_json(root / 'setup' / 'profile-completion.json', saved)
    state = task.get('state') or saved.get('state') or ('expired' if task_id else 'idle')
    result = {'missing_fields': missing, 'repair_fields': repair_fields, 'task_id': task_id if task else None, 'state': state,
              'message': task.get('message') or saved.get('message') or (
                  'Missing details will update automatically when new material is available.' if state == 'expired' else ''),
              'result': task.get('result') or saved.get('result') or {}}
    if (not missing and not repair_fields) or jobs is None:
        return result
    fingerprint = sources_fingerprint(root)
    if not fingerprint:
        return result
    retry = False
    if state in {'failed', 'timed_out', 'interrupted'}:
        try:
            finished = datetime.fromisoformat(task.get('finished_at') or saved.get('finished_at') or saved['started_at'])
            retry = (datetime.now(UTC) - finished).total_seconds() >= 300
        except (ValueError, TypeError, KeyError):
            retry = True
    if saved.get('fingerprint') == fingerprint and not retry:
        return result
    if any(row.get('state') in {'queued', 'running'} for row in tasks):
        return result | {'state': 'waiting', 'message': 'Profile completion is waiting for the current setup action to finish.'}
    try:
        queued = jobs.launch('suggest', {'use_ai': True, 'share_with_model': True, 'apply_profile': True})
    except ValueError as exc:
        return result | {'state': 'waiting', 'message': str(exc)}
    remember(root, queued['task_id'], fingerprint)
    return result | {'task_id': queued['task_id'], 'state': queued['state'],
                     'message': 'Filling missing profile fields from your saved material…'}

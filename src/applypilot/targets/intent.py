"""Target overlays remain part of the single revisioned discovery intent."""
from dataclasses import replace
import json

from . import store


def load_snapshot():
    from applypilot.config import DB_PATH
    from applypilot import postgres
    if not DB_PATH.exists():
        return "[]"
    conn = postgres.connect(DB_PATH, readonly=True)
    try:
        return store.snapshot(conn)
    finally:
        conn.close()


def matching_targets(candidate, policies, origins=()):
    from applypilot.referrals.store import company_key
    explicit = {x.split(':', 1)[1] for x in origins if x.startswith('target:')}
    company = company_key(candidate.company)
    urls = {str(getattr(candidate, key, '') or '') for key in ('url', 'listing_url', 'application_url')}
    identity = getattr(candidate, 'identity', None)
    if identity:
        urls.update(str(getattr(identity, key, '') or '') for key in ('listing_url', 'canonical_apply_url'))
    result = []
    for target in policies:
        if target['id'] in explicit or (target.get('job_url') and target['job_url'] in urls):
            result.append(target)
        elif not target.get('job_url') and company and company_key(target.get('company')) == company:
            result.append(target)
    return result


def overlay(intent, target):
    from applypilot.discovery.models import RoleFilter, QuerySpec, WorkMode, EmploymentType
    from applypilot.discovery.normalization import country_code
    changes = {'target_policies_json': '[]'}
    if target.get('roles'):
        changes.update(role_filter=RoleFilter(include_any=tuple(target['roles'])), exclude_titles=(),
                       queries=tuple(QuerySpec(role, 1) for role in target['roles']), selected_tiers=(1,))
    markets = []
    for market in intent.markets:
        values = {}
        if target.get('work_arrangements'):
            values['work_modes'] = tuple(WorkMode(x) for x in target['work_arrangements'])
        if target.get('employment_types'):
            values['employment_types'] = tuple(EmploymentType(x) for x in target['employment_types'])
        if target.get('locations'):
            countries = tuple(filter(None, (country_code(x) for x in target['locations'])))
            cities = tuple(x for x in target['locations'] if not country_code(x))
            values.update(countries=countries, cities=cities, regions=(), location=', '.join(target['locations']))
        markets.append(replace(market, **values))
    changes['markets'] = tuple(markets)
    return replace(intent, **changes)


def assess(candidate, intent, applicant, origins, admission_policy, role_evaluation):
    from applypilot.discovery.matching import assess_candidate
    policies = json.loads(intent.target_policies_json)
    matches = matching_targets(candidate, policies, origins)
    # All matching directives must permit action. The last explicit edit supplies
    # preference values; a restriction is never erased by an unrelated source.
    effective = replace(intent, target_policies_json='[]')
    for target in sorted(matches, key=lambda t: (t['revision'], t['id'])):
        effective = overlay(effective, target)
    result = assess_candidate(candidate, effective, applicant, origins,
                              admission_policy=admission_policy, role_evaluation=role_evaluation)
    allowed = all(t['state'] == 'active' and t['action'] in {'watch', 'apply', 'both'} for t in matches)
    return replace(result, intent_fingerprint=intent.fingerprint(),
                   prepare_allowed=result.prepare_allowed and allowed,
                   apply_allowed=result.apply_allowed and allowed)


def link(conn, candidate, intent, origins, job_url):
    for target in matching_targets(candidate, json.loads(intent.target_policies_json), origins):
        current = store.get(conn, target['id'])
        if current['revision'] != target['revision']:
            raise ValueError('Target changed during discovery; reassessment required')
        conn.execute("""INSERT INTO company_target_jobs(target_id,job_url,revision,created_at)
            VALUES(?,?,?,?) ON CONFLICT(target_id,job_url) DO UPDATE SET revision=excluded.revision""",
            (target['id'], job_url, target['revision'], store.now()))


def tasks(context):
    from applypilot.company_discovery.source_policy import target_research_allowed
    from applypilot.discovery.models import SearchTask, SourceTarget
    from applypilot.config import DB_PATH
    from applypilot import postgres
    if not DB_PATH.exists():
        return ()
    conn = postgres.connect(DB_PATH, readonly=True)
    try:
        if not store.available(conn):
            return ()
        output = []
        for row in conn.execute("SELECT id,revision,priority,origin FROM company_targets WHERE state='active' ORDER BY id"):
            if not target_research_allowed(conn, dict(row)):
                continue
            target = SourceTarget('company_targets', row['id'], 'company_target',
                                  (('revision', row['revision']), ('priority', row['priority'])),
                                  ('target:' + row['id'],), 'preferred')
            output.append(SearchTask(logical_request_key=target.request_key(), target=target,
                                     intent_fingerprint=context.intent.fingerprint(), profile_version=context.applicant.fingerprint()))
        return tuple(output)
    finally:
        conn.close()

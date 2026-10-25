"""Finite evidence, diary and profile semantics for explicit owner requests."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import json
from pathlib import Path

from .contracts import Observation, digest
from . import store


def _invalid(message):
    from temporalio.exceptions import ApplicationError
    raise ApplicationError(message, type="InvalidUtilityInput", non_retryable=True)


def _clean(value):
    def encode(item):
        if hasattr(item, 'to_dict'):
            return item.to_dict()
        if is_dataclass(item):
            return asdict(item)
        if isinstance(item, Path):
            return str(item)
        raise TypeError('Unsupported utility result type')
    return json.loads(json.dumps(value, default=encode, ensure_ascii=False, allow_nan=False))


def receipt_key(request):
    return digest(['utility', request.reference, request.phase, request.sequence,
                   request.options.get('previous_result')])


def execute(request, stop, heartbeat):
    from applypilot import config
    from applypilot.database import get_connection
    conn = get_connection()
    key = receipt_key(request)
    prior = store.receipt(conn, key)
    if prior:
        return Observation(**prior)
    value = store.get(conn, request.reference)
    result, detail, state = perform(request, value, config.APP_DIR, stop, heartbeat)
    conn = get_connection()
    reference = store.put(conn, {'result': _clean(result)})
    response = Observation(state, {'reference': reference, **detail})
    store.save_receipt(conn, key, asdict(response))
    conn.commit()
    return response


def perform(request, value, root, stop, heartbeat):
    phase = request.phase
    data = value.get('payload', {})

    def progress(**event):
        if stop.is_set():
            from applypilot.execution import ExecutionStopped
            raise ExecutionStopped('Utility request cancelled')
        heartbeat(phase)

    progress()
    if phase in {'referral_inventory', 'referral_case_prepare', 'referral_case_revise', 'referral_prepare_finish'}:
        from .referral_preparation import perform as prepare_referrals
        return prepare_referrals(request, value)
    if phase == 'referral_reconcile':
        import asyncio
        from applypilot.database import get_connection
        from applypilot.referrals.outreach import ReferralMailbox, reconcile
        from applypilot.tracking.gmail_client import open_feedback_mailbox
        async def observe():
            async with open_feedback_mailbox() as mailbox:
                return await reconcile(get_connection(), ReferralMailbox(mailbox, allow_send=False), data['outbox_id'])
        return {'state': asyncio.run(observe()), 'send_retried': False}, {}, 'complete'
    if phase == 'github_person':
        from applypilot.referrals.discovery import github_person
        return github_person(data['username']), {}, 'complete'
    if phase == 'referral_discovery':
        from applypilot.database import get_connection
        from applypilot.referrals.discovery import discover
        result = discover(get_connection(), data['case_id'], provider=data['provider'],
            limit=data['limit'], authorized=data.get('authorized') is True, inputs=data.get('inputs'),
            operation=request.options['discovery_operation'], run_id=receipt_key(request))
        return result, {}, result['status']
    if phase == 'referral_revise':
        from applypilot.database import get_connection
        from applypilot.config import load_profile
        from applypilot.integrations.referrals import ai_revise
        return ai_revise(get_connection(), data['outbox_id'], load_profile(), operation_id=receipt_key(request)), {}, 'complete'
    if phase in {'impact_source_draft', 'impact_bundle_draft'}:
        from applypilot.impact import draft
        if phase == 'impact_source_draft' and not str(data.get('source_text') or '').strip():
            _invalid('Provide source text for impact drafting')
        if phase == 'impact_bundle_draft' and not (data.get('bundle') or {}).get('sources'):
            _invalid('Provide source material for impact drafting')
        options = {key: data[key] for key in ('skip_interview', 'no_approve_review') if key in data}
        result = (draft.draft_records_from_source(data['source_text'], source_name=data['source_name'], **options)
                  if phase == 'impact_source_draft' else draft.draft_records_from_bundle(data['bundle'], **options))
        if result.get('errors'):
            raise RuntimeError('Impact drafting did not complete')
        return result, {}, 'complete'
    if phase == 'impact_dedup':
        from applypilot.impact.draft import dedup_ledger
        return dedup_ledger(dry_run=data.get('dry_run', True)), {}, 'complete'
    if phase in {'impact_remeasure', 'impact_reconcile'}:
        from applypilot.impact import draft
        result = (draft.remeasure_ledger(dry_run=data.get('dry_run', False), limit=data.get('limit'))
                  if phase == 'impact_remeasure' else draft.reconcile_ledger(dry_run=data.get('dry_run', False)))
        return result, {}, 'complete'
    if phase == 'impact_brief':
        from applypilot.impact.models import ImpactRecord
        from applypilot.impact.defense import build_defense_facts
        from applypilot.impact.brief import generate_interview_brief, load_cached_brief, render_defense_card, save_brief_cache, _fallback_questions
        record, job = ImpactRecord.from_dict(data['record']), data.get('job')
        facts = build_defense_facts(record, job=job)
        brief = load_cached_brief(record, job)
        if not brief:
            brief = generate_interview_brief(facts, record, job=job, seniority='senior')
            save_brief_cache(record, job, brief)
        if not brief.get('director_followups'):
            brief['director_followups'] = _fallback_questions(facts)
        return {'card': render_defense_card(facts, record, job), 'brief': brief}, {}, 'complete'
    if phase == 'evidence_prune':
        from applypilot.evidence.draft import judge_entries_for_prune
        judged, error = judge_entries_for_prune(data['entries'])
        if error:
            raise RuntimeError('Evidence audit did not produce validated results')
        return {'judged': judged}, {}, 'complete'
    if phase == 'impact_extract':
        import re
        from applypilot.onboarding.storage import read_json, write_json
        from applypilot.impact.draft import draft_records_from_source
        if data.get('confirm_model') is not True or not re.fullmatch('[a-f0-9]{24}', str(data.get('id', ''))):
            _invalid('Confirm model use and select an existing diary note')
        from applypilot.backup.common import lock
        # This protects note/edit consistency with diary_edit; it owns no work
        # admission, worker identity or retry clock.
        with lock(root, 'impact-extract'):
            path = root / 'diary' / (data['id'] + '.json')
            note = read_json(path, {})
            if not note:
                _invalid('Save the note before extracting impact evidence')
            if note.get('impact_state') == 'completed':
                return note.get('impact_result', {}), {}, 'complete'
            result = _clean(draft_records_from_source(note['note'], source_name='diary-' + data['id']))
            if result.get('errors'):
                raise RuntimeError('Diary extraction did not produce validated evidence')
            write_json(path, {**note, 'impact_state': 'completed', 'impact_result': result})
            from applypilot.backup.runtime import request_checkpoint
            request_checkpoint(root)
            return result, {}, 'complete'
    if phase == 'impact_revise':
        from applypilot.backup.diary_edit import revise, detail
        if data.get('confirm_model') is not True:
            _invalid('Confirm using the configured model for this revision')
        try:
            current = detail(root, data.get('kind'), data.get('id'))
        except ValueError:
            _invalid('Choose an active diary item')
        if not isinstance(data.get('instruction'), str) or not 1 <= len(data['instruction'].strip()) <= 4000:
            _invalid('Describe the revision in 1–4000 characters')
        if data.get('revision') != current['revision']:
            return {'state': 'superseded', 'message': 'The diary item changed; request a new revision.'}, {}, 'superseded'
        return revise(root, data, progress), {}, 'complete'
    if phase == 'suggest':
        from applypilot.onboarding.planning import propose, load_draft
        if (data.get('use_ai') is True or data.get('auto_suggest') is True) and data.get('share_with_model') is not True:
            _invalid('Authorize sharing profile material with the configured model')
        previous = request.options.get('previous_result')
        if previous:
            from applypilot.database import get_connection
            data = {**data, 'revision': load_draft(root)['revision'], 'apply_profile': True,
                    'use_ai': data.get('use_ai') is True or data.get('auto_suggest') is True}
            result = store.get(get_connection(), previous)['result']
            result['suggestions'] = propose(root, data, progress=progress)
        else:
            if data.get('revision') is not None and data['revision'] != load_draft(root)['revision']:
                return {'state': 'superseded', 'message': 'The profile draft changed; request new suggestions.'}, {}, 'superseded'
            result = propose(root, data, progress=progress)
        return result, {}, 'complete'
    if phase == 'intake':
        from applypilot.onboarding.documents import intake
        result = intake(root, data, progress=progress)
        suggest = data.get('auto_suggest') is True and data.get('share_with_model') is True
        return result, {'suggest_after': suggest}, 'complete'
    if phase == 'material_url':
        from applypilot.onboarding.materials import collect_url
        from urllib.parse import urlsplit
        if urlsplit(str(data.get('url') or '')).hostname in {'github.com', 'www.github.com'}:
            _invalid('GitHub material must use its repository workflow')
        return collect_url(root, data, progress), {'suggest_after': True}, 'complete'
    if phase == 'github_inventory':
        from applypilot.evidence.github import GitHubProvider
        from applypilot.onboarding.github import identity
        from applypilot.onboarding.workspace import connected_identity
        from applypilot.database import get_connection
        if data.get('use_ai') is True and data.get('share_with_model') is not True:
            _invalid('Authorize sharing repository evidence with the configured model')
        limit = data.get('limit', 3)
        if limit is not None and (type(limit) is not int or limit < 1):
            _invalid('Repository limit must be positive or null')
        provider = GitHubProvider(cache_root=root, cancel_event=stop)
        try:
            selected_identity = identity(data.get('identity') or connected_identity(root, 'github'))
        except ValueError:
            _invalid('Choose a GitHub username or repository')
        candidates = provider.discover(selected_identity,
            limit=limit, include_private=data.get('include_private') is True)
        if data.get('dry_run') is True:
            return {'dry_run': True, 'repositories': len(candidates), 'candidates': [item.ref for item in candidates],
                    'live_profile_changed': False}, {}, 'dry_run'
        if not candidates:
            _invalid('No eligible repositories were found')
        conn = get_connection()
        collection = store.put(conn, {**value, 'candidates': [asdict(candidate) for candidate in candidates]})
        conn.commit()
        return {}, {'reference': collection}, 'inventoried'
    if phase == 'github_repository':
        if request.sequence >= len(value['candidates']):
            return {}, {}, 'complete'
        from queue import SimpleQueue
        from applypilot.evidence.base import CandidateSource
        from applypilot.evidence.github import GitHubProvider
        from applypilot.onboarding.github import _collect_repository, _save_collected, publish_grounded
        from applypilot.onboarding.storage import read_json
        candidate = CandidateSource(**value['candidates'][request.sequence])
        prior = next((row for row in read_json(root / 'setup' / 'github.json', [], limit=32_000_000)
                      if row.get('ref') == candidate.ref), {})
        provider = GitHubProvider(cache_root=root, cancel_event=stop)
        result = _collect_repository(root, candidate, prior, data, provider.budget, stop,
                                     SimpleQueue(), propagate_errors=True)
        entry = result['entry']
        if entry.get('pack_status') == 'incomplete' or entry.get('extraction_status') == 'incomplete':
            raise RuntimeError('Repository evidence did not complete; Temporal will retry this repository')
        _save_collected(root, [entry])
        result.update(publish_grounded(root))
        return result, {}, 'processed'
    if phase == 'github_finish':
        from applypilot.database import get_connection
        from dataclasses import replace
        conn = get_connection()
        results = []
        for index in range(len(value['candidates'])):
            saved = store.receipt(conn, receipt_key(replace(request, phase='github_repository', sequence=index)))
            if not saved:
                raise ValueError('Repository evidence result is missing')
            results.append(store.get(conn, saved['detail']['reference'])['result'])
        entries = [result['entry'] for result in results]
        result = {'repositories': len(entries), 'repositories_reused': sum(bool(item['reused']) for item in results),
            'repositories_packed': sum(item.get('pack_status') == 'packed' for item in entries),
            'repositories_incomplete': sum(item.get('pack_status') == 'incomplete' for item in entries),
            'repositories_empty': sum(item.get('pack_status') == 'empty' for item in entries),
            'extractions_incomplete': sum(item.get('extraction_status') in {'incomplete', 'deferred'} for item in entries),
            'extractions_failed': sum(item.get('extraction_status') == 'incomplete' for item in entries),
            'claims_extracted': sum(item['claims_extracted'] for item in results),
            'claims_published': sum(item['claims_published'] for item in results),
            'live_profile_changed': any(item['live_profile_changed'] for item in results),
            'warnings': [warning for item in results for warning in item['warnings']]}
        suggest = value['operation'] == 'github' and data.get('use_ai') is True and data.get('share_with_model') is True
        return result, {'suggest_after': suggest}, 'complete'
    _invalid('Unknown utility operation')

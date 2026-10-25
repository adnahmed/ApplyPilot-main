"""Owner edits retain repository provenance and invalidate automatic claim approval."""
from __future__ import annotations

from copy import deepcopy
import secrets

from .storage import digest, now, read_json, revision, setup_lock, write_json


def evidence_id(row):
    return str(row.get('id') or 'evidence-' + digest({key: row.get(key) for key in
               ('provider', 'ref', 'url', 'title')})[:20])


def claims(row):
    saved = row.get('claims')
    if not isinstance(saved, list) or not saved:
        saved = [{'allowed_resume_claim': text, 'resume_eligible': row.get('resume_eligible') is True}
                 for text in row.get('allowed_claims') or [] if isinstance(text, str)]
    return [dict(claim, id=str(claim.get('id') or f'claim-{index}'))
            for index, claim in enumerate(saved) if isinstance(claim, dict)]


def detail(root, identifier):
    profile = read_json(root / 'profile.json', {}, limit=8_000_000)
    row = next((item for item in profile.get('public_evidence') or []
                if isinstance(item, dict) and evidence_id(item) == identifier), None)
    if row is None:
        raise ValueError('This evidence is no longer available. Refresh your accomplishments.')
    return {'id': identifier, 'item': row, 'claims': claims(row), 'revision': digest(row)}


def edit(root, data):
    allowed = {'id', 'revision', 'title', 'scope', 'claims', 'resume_eligible'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('Unknown evidence field')
    identifier = data.get('id')
    if not isinstance(identifier, str) or not identifier or len(identifier) > 180:
        raise ValueError('Choose a saved evidence entry')
    with setup_lock(root):
        current = detail(root, identifier)
        before = current['item']
        if data.get('revision') != current['revision']:
            raise ValueError('This evidence changed elsewhere. Reload before saving.')
        after = deepcopy(before)
        for key in ('title', 'scope'):
            if key in data:
                value = data[key]
                if not isinstance(value, str) or not 1 <= len(value.strip()) <= 1200:
                    raise ValueError('Use 1–1,200 characters for the evidence title or contribution description')
                after[key] = value.strip()
                if key == 'title':
                    after['owner_title'] = value.strip()
        if 'resume_eligible' in data:
            if type(data['resume_eligible']) is not bool:
                raise ValueError('Choose whether this evidence may be used in applications')
            after['resume_eligible'] = data['resume_eligible']
            after['review_status'] = 'user_approved' if data['resume_eligible'] else 'revoked'
            if data['resume_eligible']:
                after.pop('revoked_at', None)
            else:
                after['revoked_at'] = now()
        if 'claims' in data:
            supplied = data['claims']
            if not isinstance(supplied, list) or len(supplied) > 100:
                raise ValueError('Claims must be a list of at most 100 saved statements')
            existing = {claim['id']: claim for claim in current['claims']}
            seen = set()
            for patch in supplied:
                if not isinstance(patch, dict) or set(patch) - {'id', 'text', 'resume_eligible'}:
                    raise ValueError('Unknown statement field')
                key = patch.get('id')
                if key not in existing or key in seen:
                    raise ValueError('A statement changed or was repeated. Reload before saving.')
                seen.add(key)
                claim = existing[key]
                if 'text' in patch:
                    text = patch['text']
                    if not isinstance(text, str) or not 10 <= len(text.strip()) <= 1500:
                        raise ValueError('Write each statement in 10–1,500 characters')
                    if text.strip() != claim.get('allowed_resume_claim'):
                        claim['original_allowed_resume_claim'] = claim.get('original_allowed_resume_claim') or claim.get('allowed_resume_claim')
                        claim.update(allowed_resume_claim=text.strip(), owner_edited_at=now(),
                                     evidence_class='USER_ATTESTED', review_status='user_approved', attested_at=now())
                if 'resume_eligible' in patch:
                    if type(patch['resume_eligible']) is not bool:
                        raise ValueError('Choose whether the statement may be used in applications')
                    claim['resume_eligible'] = patch['resume_eligible']
                    claim['review_status'] = 'user_approved' if patch['resume_eligible'] else 'revoked'
                    if patch['resume_eligible']:
                        claim.pop('revoked_at', None)
                    else:
                        claim['revoked_at'] = now()
            after['claims'] = list(existing.values())
            after['allowed_claims'] = [claim['allowed_resume_claim'] for claim in after['claims']
                                       if claim.get('resume_eligible') is True and claim.get('allowed_resume_claim')]
        if after == before:
            return {'saved': True, 'message': 'No changes to save.', 'revision': digest(after)}
        after['updated_at'] = now()
        profile = read_json(root / 'profile.json', {}, limit=8_000_000)
        profile['public_evidence'] = [after if evidence_id(row) == identifier else row
                                      for row in profile.get('public_evidence') or []]
        history_id = secrets.token_hex(12)
        write_json(root / 'evidence' / 'history' / (history_id + '.json'),
                   {'id': history_id, 'evidence_id': identifier, 'before': before, 'after': after, 'created_at': now()})
        write_json(root / 'profile.json', profile)
        draft = read_json(root / 'setup' / 'draft.json', None, limit=8_000_000)
        if isinstance(draft, dict):
            draft.setdefault('profile', {})['public_evidence'] = deepcopy(profile['public_evidence'])
            draft['base_revision'] = revision(root)
            draft.pop('revision', None)
            write_json(root / 'setup' / 'draft.json', draft)
    from applypilot.backup.runtime import request_checkpoint
    request_checkpoint(root)
    return {'saved': True, 'revision': digest(after), 'history_id': history_id,
            'message': 'Evidence saved. Future applications use your reviewed statements; source references and edit history are retained.'}

"""Source observations -> review queue -> verified professional contacts.

Import is offline and never sends, grants consent, verifies employment, or infers
familiarity from a first-degree connection. Identity is exact, not fuzzy names.
"""
from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from datetime import timedelta
import re
from uuid import uuid4

from . import store as s
from .identity import profile_key

MAX_ROWS = 1000


def _text(value, maximum=1000):
    return value.strip()[:maximum] if isinstance(value, str) else ''


def _rows(data):
    for _ in range(5):
        if isinstance(data, list):
            flattened = []
            for row in data:
                if isinstance(row, dict) and isinstance(row.get('items'), list):
                    for wrapper in row['items'][:MAX_ROWS]:
                        item = wrapper.get('item') if isinstance(wrapper, dict) else None
                        entity = item.get('entityResult') if isinstance(item, dict) else None
                        if isinstance(entity, dict):
                            title = entity.get('title') or {}
                            subtitle = entity.get('primarySubtitle') or {}
                            flattened.append({'name': title.get('text') if isinstance(title, dict) else title,
                                'profile_url': entity.get('navigationUrl'),
                                'role_function': subtitle.get('text') if isinstance(subtitle, dict) else subtitle})
                        else:
                            flattened.append({'unrecognized_search_item': True})
                else:
                    flattened.append(row)
                if len(flattened) > MAX_ROWS:
                    raise ValueError('split source responses into batches of at most 1000 people')
            return flattened
        if not isinstance(data, dict):
            break
        if data.get('error') or data.get('success') is False or data.get('ok') is False:
            raise ValueError('source reported failure; not an empty network')
        for key in ('elements', 'contacts', 'people', 'connections', 'results', 'items', 'data'):
            if isinstance(data.get(key), (dict, list)):
                data = data[key]
                break
        else:
            if any(k in data for k in ('name', 'firstName', 'miniProfile', 'profile_url')):
                return [data]
            break
    raise ValueError('expected structured people rows; aggregate text needs source review/enrichment first')


def read_network_file(path, *, max_rows=MAX_ROWS):
    """LinkedIn Connections.csv (including preamble/BOM), or structured JSON."""
    if type(max_rows) is not int or not 1 <= max_rows <= 50000:
        raise ValueError('max_rows must be 1..50000')
    path = Path(path)
    if path.stat().st_size > 5_000_000:
        raise ValueError('network export exceeds 5 MB')
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix.casefold() != '.csv':
        value=json.loads(text)
        rows=value if isinstance(value,list) else _rows(value)
        if len(rows)>max_rows: raise ValueError('network export exceeds row limit')
        return rows
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines[:25]):
        fields = next(csv.reader([line]), [])
        if 'First Name' in fields and 'Last Name' in fields and 'URL' in fields:
            start = i
            break
    if start is None:
        raise ValueError('CSV requires LinkedIn First Name, Last Name and URL columns')
    rows = []
    for row in csv.DictReader(io.StringIO(''.join(lines[start:]))):
        if len(rows) >= max_rows:
            raise ValueError('network export exceeds row limit')
        rows.append({'name': ' '.join(filter(None, [_text(row.get('First Name')), _text(row.get('Last Name'))])),
                     'profile_url': row.get('URL'), 'company': row.get('Company'),
                     'role_function': row.get('Position'), 'connection_degree': 'first',
                     'connected_on': row.get('Connected On')})
        # Exported email is deliberately not a new contact permission or recipient.
    return rows


def normalize_people(data, *, provider, company_hint='', observed_at=None, operation='people', limit=MAX_ROWS):
    if type(limit) is not int or not 1 <= limit <= MAX_ROWS:
        raise ValueError('people limit must be 1..1000')
    when = s.iso(observed_at)
    if s.utc(when) > s.utc() + timedelta(minutes=5):
        raise ValueError('observation cannot be in the future')
    rows = _rows(data)
    leads, rejected = [], []
    for index, row in enumerate(rows[:limit]):
        if not isinstance(row, dict):
            rejected.append({'row': index, 'reason': 'not a person object'})
            continue
        person = row.get('miniProfile') if isinstance(row.get('miniProfile'), dict) else row
        name = _text(person.get('name') or person.get('full_name')) or ' '.join(filter(None, [
            _text(person.get('firstName') or person.get('first_name')),
            _text(person.get('lastName') or person.get('last_name'))]))
        url = _text(person.get('profile_url') or person.get('profileUrl') or person.get('linkedin_url') or person.get('url'))
        slug = _text(person.get('publicIdentifier'))
        if not url and provider.startswith('linkedin') and re.fullmatch(r'[\w%.-]{1,200}', slug):
            url = 'https://www.linkedin.com/in/' + slug
        try:
            identity = profile_key(url)
            if not identity or not name:
                raise ValueError('missing explicit name or profile identity')
        except ValueError as exc:
            rejected.append({'row': index, 'reason': str(exc)})
            continue
        role = _text(person.get('role_function') or person.get('position') or person.get('headline') or person.get('occupation'))
        company = _text(person.get('company') or person.get('company_name'))
        degree = 'first' if operation == 'connections' else _text(person.get('connection_degree'))
        observation = {'provider': _text(provider, 100), 'operation': operation,
                       'url': url, 'observed_at': when, 'name': name, 'company_claim': company,
                       'role_claim': role, 'target_company_hint': _text(company_hint, 250),
                       'connection_degree': degree, 'connected_on': _text(person.get('connected_on')),
                       'quote': _text(person.get('quote') or person.get('source_note') or role, 2500),
                       'source_url': _text(person.get('source_url') or url, 2000),
                       'trust': 'unverified-source-observation'}
        leads.append({'identity_key': identity, 'profile_url': url, 'name': name,
                      'company_hint': company or _text(company_hint, 250), 'role_hint': role,
                      'observation': observation})
    return {'leads': leads, 'rejected': rejected, 'unprocessed': max(0, len(rows) - limit)}


def stage_people(conn, data, *, provider, company_hint='', observed_at=None, operation='people', limit=MAX_ROWS):
    normalized = normalize_people(data, provider=provider, company_hint=company_hint,
                                  observed_at=observed_at, operation=operation, limit=limit)
    ids, suppressed = [], 0
    with s.transaction(conn):
        for lead in normalized['leads']:
            if conn.execute('SELECT 1 FROM referral_contacts WHERE profile_key=? AND blocked_at IS NOT NULL',
                            (lead['identity_key'],)).fetchone():
                suppressed += 1
                continue
            old = conn.execute('SELECT * FROM referral_leads WHERE identity_key=?', (lead['identity_key'],)).fetchone()
            lid = old['id'] if old else uuid4().hex
            evidence = s.obj(old['evidence_json'], []) if old else []
            obs = lead['observation']
            # Same export is idempotent even when imported at a different time.
            def sig(item):
                return s.dump({k: v for k, v in item.items() if k != 'observed_at'})
            if sig(obs) not in {sig(item) for item in evidence}:
                evidence.append(obs)
            conn.execute('''INSERT INTO referral_leads
                (id,identity_key,profile_url,name,company_hint,role_hint,evidence_json,first_seen_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET
                evidence_json=excluded.evidence_json,last_seen_at=excluded.last_seen_at''',
                (lid, lead['identity_key'], lead['profile_url'], lead['name'], lead['company_hint'], lead['role_hint'],
                 s.dump(evidence[-20:]), s.iso(), s.iso()))
            # Never reset a rejection or mutate a promoted contact after a source refresh.
            ids.append(lid)
    return {'lead_ids': list(dict.fromkeys(ids)), 'rejected_rows': normalized['rejected'],
            'unprocessed': normalized['unprocessed'], 'suppressed': suppressed,
            'review_required': True, 'contacts_imported': 0, 'sent': 0}


def list_leads(conn, *, company=None, state='review', limit=100):
    if state not in {'review', 'promoted', 'rejected', 'all'} or type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid lead state/limit')
    clauses, params = [], []
    if state != 'all':
        clauses.append('state=?')
        params.append(state)
    if company:
        # Exact hints only; no substring merging of subsidiaries or similar brands.
        clauses.append('lower(trim(company_hint))=?')
        params.append(s.company_key(company))
    rows = conn.execute('SELECT * FROM referral_leads' + (' WHERE ' + ' AND '.join(clauses) if clauses else '')
                        + ' ORDER BY last_seen_at DESC,id LIMIT ?', [*params, limit]).fetchall()
    return [dict(row, evidence_json=s.obj(row['evidence_json'], [])) for row in rows]


def promote_lead(conn, lead_id, *, reviewed, company, role_function, evidence,
                 relationship='cold', relationship_note='', team='', kind='employee', name=None):
    """Review is required independently of source claims. No email permission here."""
    if reviewed is not True or not _text(company) or not _text(role_function) or not _text(evidence):
        raise ValueError('review requires current employer, function and your evidence note')
    with s.transaction(conn):
        lead = conn.execute('SELECT * FROM referral_leads WHERE id=?', (lead_id,)).fetchone()
        if not lead or lead['state'] == 'rejected':
            raise ValueError('unknown or rejected lead')
        matches = conn.execute('SELECT * FROM referral_contacts WHERE profile_key=?', (lead['identity_key'],)).fetchall()
        if any(r['blocked_at'] for r in matches):
            raise ValueError('identity is suppressed; source import cannot undo an opt-out')
        if len(matches) > 1:
            raise ValueError('ambiguous existing contacts; reconcile identities first')
        if matches:
            # Re-observation cannot downgrade an existing relationship or expand consent.
            person = dict(matches[0])
            if person['company_key'] != s.company_key(company):
                raise ValueError('existing contact employer differs; explicitly update the reviewed contact first')
        else:
            person = s.add_contact(conn, {'name': name or lead['name'], 'company': company,
                'role_function': role_function, 'team': team, 'relationship': relationship,
                'relationship_note': relationship_note, 'kind': kind,
                'profile_url': lead['profile_url'], 'source': 'reviewed-lead:' + lead_id,
                'source_note': evidence, 'email_allowed': False, 'email_basis': 'manual_only'})
        conn.execute("UPDATE referral_leads SET state='promoted',contact_id=?,review_note=? WHERE id=?",
                     (person['id'], evidence, lead_id))
    return person


def reject_lead(conn, lead_id, *, reason):
    if not _text(reason):
        raise ValueError('rejection needs a reason')
    with s.transaction(conn):
        if conn.execute("UPDATE referral_leads SET state='rejected',review_note=? WHERE id=? AND state='review'",
                        (reason, lead_id)).rowcount != 1:
            raise ValueError('only an unpromoted review lead can be rejected; suppress contacts separately')

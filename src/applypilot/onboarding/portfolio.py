"""Read-only capability portfolio. Original evidence, not a software-stack checklist."""
from __future__ import annotations
from .storage import read_json


def portfolio(root, profile):
    from .evidence_edit import evidence_id
    from applypilot.profile import resume_evidence_entries
    projects=profile.get('public_evidence',[])
    projects=projects if isinstance(projects,list) else []
    rows=[]
    for item in projects:
        if not isinstance(item,dict): continue
        claims=item.get('claims') or []
        eligible = resume_evidence_entries({'public_evidence': [item]})
        rows.append({'id':evidence_id(item),'title':str(item.get('title') or item.get('ref') or 'Evidence'),
            'provider':str(item.get('provider','material')),'url':str(item.get('url','')),
            'scope':str(item.get('scope','')),'resume_eligible':item.get('resume_eligible') is True,
            'claims':claims,'allowed_claims':item.get('allowed_claims') or [],
            'eligible_claims': eligible[0]['allowed_claims'] if eligible else [],
            'version':item.get('version'),'fetched_at':item.get('fetched_at')})
    ledger=read_json(root/'impact_ledger.json',{},limit=128*1024**2)
    records=ledger.get('records',[]) if isinstance(ledger,dict) else []
    from applypilot.backup.diary import view
    diary = view(root, page_size=None)
    entries = [{'id': row['id'], 'kind': 'repository', 'title': row['title'],
                'text': '\n'.join(str(claim.get('allowed_resume_claim') or claim.get('capability') or '')
                    for claim in row['claims'] if isinstance(claim, dict)) or '\n'.join(row['allowed_claims']),
                'scope': row['scope'], 'created_at': row.get('fetched_at') or '', 'url': row['url'],
                'resume_eligible': row['resume_eligible']} for row in rows]
    entries.extend(diary['entries'])
    entries.sort(key=lambda row: str(row.get('created_at') or ''), reverse=True)
    from .workspace import canonical_profile
    experience = canonical_profile(profile).get('experience') or {}
    accomplishments = []
    for row in rows:
        if row['resume_eligible']:
            accomplishments.extend({'text': text, 'source': row['title'], 'id': row['id'], 'kind': 'repository'}
                                    for text in row['eligible_claims'] if isinstance(text, str))
    for record in records:
        if not isinstance(record, dict) or record.get('resume_eligible') is not True:
            continue
        intervention = record.get('intervention') or {}
        text = intervention.get('summary') or intervention.get('description') if isinstance(intervention, dict) else ''
        if text:
            accomplishments.append({'text': text, 'source': record.get('problem') or 'Achievement',
                                    'id': record.get('id'), 'kind': 'record'})
    return {'evidence':rows,'evidence_count':len(rows),'impact_count':len(records),
            'claims_count':sum(len(r['claims']) or len(r['allowed_claims']) for r in rows),
            'entries': entries, 'entries_total': len(rows) + diary['total'],
            'summary': {'text': experience.get('professional_summary') or '',
                        'capabilities': experience.get('capabilities') or [], 'domains': experience.get('domains') or [],
                        'accomplishments': accomplishments[:12], 'accomplishments_total': len(accomplishments)},
            'note':'Your contributions, evidence, and measured results appear together. Every statement keeps its original source and review status.'}


def strategy(existing, data):
    """Patch orthogonal strategy controls without rebuilding queries/markets/variants."""
    from copy import deepcopy
    if not isinstance(data,dict) or set(data)-{'preference_order','exploration','probes','location_labels','sources_enabled'}:
        raise ValueError('Unknown strategy update')
    result=deepcopy(existing)
    if 'preference_order' in data:
        prefs=result.get('preferences',[]);order=data['preference_order']
        ids=[r['id'] for r in prefs]
        if not isinstance(order,list) or len(order)!=len(ids) or set(order)!=set(ids) or len(set(order))!=len(order):
            raise ValueError('Reorder the existing preference IDs without removing or duplicating rules')
        by_id={r['id']:r for r in prefs};result['preferences']=[by_id[k] for k in order]
    if 'exploration' in data:
        item=data['exploration']
        if not isinstance(item,dict) or set(item)-{'discovery_share','application_share'}: raise ValueError('Unknown exploration setting')
        if any(type(v) not in {int,float} or not 0<=v<=1 for v in item.values()): raise ValueError('Exploration shares must be between zero and one')
        result['exploration']={**result.get('exploration',{}),**item}
    if 'probes' in data:
        probes=data['probes']
        if not isinstance(probes,list) or len(probes)>100 or any(not isinstance(v,str) or not 1<=len(v.strip())<=160 for v in probes): raise ValueError('Use at most 100 short exploration probes')
        result['probes']=list(dict.fromkeys(v.strip() for v in probes))
    if 'location_labels' in data:
        labels=data['location_labels']
        from applypilot.discovery.intent import compile_search_intent
        # The existing compiler is the authority for label meanings and allowed markets.
        if not isinstance(labels,list) or any(not isinstance(v,str) for v in labels): raise ValueError('Select existing market labels')
        result['location_labels']=labels
    if 'sources_enabled' in data:
        names=data['sources_enabled']
        if not isinstance(names,list) or any(not isinstance(v,str) for v in names): raise ValueError('Choose discovery sources')
        result['sources']={**result.get('sources',{}),'enabled':names}
    from applypilot.discovery.intent import compile_search_intent
    compile_search_intent(result)
    return result

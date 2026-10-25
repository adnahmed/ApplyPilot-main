"""Evidence-backed professional graph; shared history is affinity, not friendship."""
from __future__ import annotations

from datetime import date
import re
from applypilot.referrals import store as s
from applypilot.referrals.identity import profile_key


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS professional_edges(
        id TEXT PRIMARY KEY, subject TEXT NOT NULL, predicate TEXT NOT NULL,
        object TEXT NOT NULL, label TEXT NOT NULL, starts TEXT, ends TEXT,
        source_url TEXT NOT NULL, quote TEXT NOT NULL, observed_at TEXT NOT NULL,
        trust TEXT NOT NULL, UNIQUE(subject,predicate,object,source_url,quote))""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_professional_subject ON professional_edges(subject,predicate)')


def edge(conn, subject, predicate, object_, *, label, source_url, quote,
         starts=None, ends=None, trust='observed'):
    if predicate not in {'works_at','worked_at','studied_at','connected_to','knows_work','professional','acquaintance'}:
        raise ValueError('Unsupported professional relationship')
    if not all(isinstance(v,str) and v.strip() for v in (subject,object_,label,source_url,quote)):
        raise ValueError('Graph evidence needs explicit subjects, source and quote')
    identity=s.digest(s.dump([subject,predicate,object_,source_url,quote]))
    with s.transaction(conn):
        conn.execute("""INSERT INTO professional_edges VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at""",
            (identity,subject,predicate,object_,label,starts,ends,source_url,quote[:4000],s.iso(),trust))
    return identity


def seed_profile(conn, profile):
    with s.transaction(conn):
        conn.execute("DELETE FROM professional_edges WHERE subject='owner' AND trust='owner' AND source_url='profile.json'")
        _seed_profile(conn,profile)


def _seed_profile(conn, profile):
    # Owner-supplied, not generated resume prose. Accept common profile shapes.
    items=profile.get('experience') or profile.get('work_experience') or []
    if isinstance(items,dict): items=items.get('items',[])
    for item in items if isinstance(items,list) else []:
        if not isinstance(item,dict): continue
        company=item.get('company') or item.get('employer')
        if not isinstance(company,str) or not company.strip(): continue
        end=item.get('end_date') or item.get('end')
        current=end in (None,'','Present','present','Current','current')
        edge(conn,'owner','works_at' if current else 'worked_at','company:'+s.company_key(company),
             label=company,source_url='profile.json',quote=s.dump(item),
             starts=str(item.get('start_date') or item.get('start') or '') or None,
             ends=str(end) if end else None,trust='owner')
    items=profile.get('education') or []
    if isinstance(items,dict): items=[items]
    for item in items if isinstance(items,list) else []:
        if not isinstance(item,dict): continue
        name=item.get('institution') or item.get('school') or item.get('university')
        if isinstance(name,str) and name.strip():
            edge(conn,'owner','studied_at','school:'+s.company_key(name),label=name,
                 source_url='profile.json',quote=s.dump(item),trust='owner')
    experience=profile.get('experience',{})
    if isinstance(experience,dict) and experience.get('current_company'):
        company=experience['current_company']
        edge(conn,'owner','works_at','company:'+s.company_key(company),label=company,
             source_url='profile.json',quote='Current company: '+company,trust='owner')
    facts=profile.get('resume_facts',{})
    if isinstance(facts,dict):
        for company in facts.get('preserved_companies',[]):
            if isinstance(company,str) and company.strip():
                edge(conn,'owner','worked_at','company:'+s.company_key(company),label=company,
                     source_url='profile.json',quote='Owner resume employer: '+company,trust='owner')
        school=facts.get('preserved_school')
        if isinstance(school,str) and school.strip():
            edge(conn,'owner','studied_at','school:'+s.company_key(school),label=school,
                 source_url='profile.json',quote='Owner resume school: '+school,trust='owner')
    # Optional one-time relationship seeds. Do not assume exported first-degree
    # connections actually know the owner's work.
    for item in (profile.get('professional_network') or [])[:1000]:
        if not isinstance(item,dict): continue
        identity=profile_key(item.get('profile_url'))
        relation=item.get('relationship')
        note=item.get('evidence')
        if identity and relation in {'knows_work','professional','acquaintance','connected_to'} and isinstance(note,str) and note.strip():
            edge(conn,'owner',relation,identity,label=item.get('name') or identity,
                 source_url='profile.json',quote=note,trust='owner')


def observe(conn, identity, facts, *, source_url, text):
    """Only exact captured quotes become edges. Dates remain optional/unknown."""
    for item in facts.get('history',[])[:30]:
        if not isinstance(item,dict) or item.get('quote','') not in text or not item.get('quote'): continue
        kind=item.get('kind');name=item.get('name')
        if kind not in {'works_at','worked_at','studied_at'} or not isinstance(name,str) or not name.strip(): continue
        if name.casefold() not in item['quote'].casefold(): continue
        prefix='school:' if kind=='studied_at' else 'company:'
        edge(conn,identity,kind,prefix+s.company_key(name),label=name,source_url=source_url,
             quote=item['quote'],starts=item.get('starts'),ends=item.get('ends'))


def affinity(conn, identity):
    if not identity: return {'score':0,'signals':[],'relationship':None,'introducers':[]}
    direct=conn.execute("""SELECT * FROM professional_edges WHERE subject='owner' AND object=?
        AND predicate IN ('knows_work','professional','acquaintance') AND trust='owner'
        ORDER BY CASE predicate WHEN 'knows_work' THEN 0 WHEN 'professional' THEN 1 ELSE 2 END LIMIT 1""",(identity,)).fetchone()
    signals=[]
    for row in conn.execute("""SELECT DISTINCT p.object,p.label,p.predicate FROM professional_edges p
        JOIN professional_edges o ON o.object=p.object AND o.subject='owner'
        WHERE p.subject=? AND p.predicate IN ('works_at','worked_at','studied_at')
        AND o.predicate IN ('works_at','worked_at','studied_at')""",(identity,)):
        signals.append({'kind':'shared_school' if row['predicate']=='studied_at' else 'shared_employer',
                        'label':row['label'],'meaning':'shared affiliation; personal familiarity and overlapping dates are not established'})
    connected=conn.execute("SELECT 1 FROM professional_edges WHERE subject='owner' AND object=? AND predicate='connected_to'",(identity,)).fetchone()
    if connected: signals.append({'kind':'first_degree','meaning':'platform connection, not proof of acquaintance'})
    paths=[]
    for row in conn.execute("""SELECT DISTINCT a.object,a.quote FROM professional_edges a JOIN professional_edges b
        ON a.object=b.subject WHERE a.subject='owner' AND a.predicate IN ('knows_work','professional','acquaintance')
        AND a.trust='owner' AND b.predicate='connected_to' AND b.object=? LIMIT 5""",(identity,)):
        paths.append({'via':row['object'],'owner_relationship_evidence':row['quote'],
                      'meaning':'observed introduction path; no referral or introduction promised'})
    return {'score':min(.12,.04*len(signals)+.06*bool(paths)), 'signals':signals,
            'relationship':{'kind':direct['predicate'],'note':direct['quote']} if direct else None,
            'introducers':paths}

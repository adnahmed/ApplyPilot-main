"""Deterministic JobPosting projection with field-level provenance, no model calls.

Ambiguous multi-job pages are not projected. Facts never rewrite application
routes or assert a referral mechanism. Reads are shared by HTTP and Crawl4AI.
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from applypilot.referrals import store as s


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS job_structured_facts(
        job_url TEXT PRIMARY KEY REFERENCES jobs(url), source_url TEXT NOT NULL,
        document_hash TEXT NOT NULL, facts_json TEXT NOT NULL, observed_at TEXT NOT NULL)""")


def _key(url):
    p=urlsplit(str(url or ''))
    return urlunsplit((p.scheme,p.netloc.lower(),p.path.rstrip('/'),
        urlencode([(k,v) for k,v in parse_qsl(p.query) if not k.startswith('utm_') and k not in {'ref','source','src'}]),''))


def nodes(values, *, depth=0):
    if depth>8:
        return
    if isinstance(values,list):
        for value in values[:100]:
            yield from nodes(value,depth=depth+1)
    elif isinstance(values,dict):
        yield values
        for key in ('@graph','mainEntity','itemListElement','item'):
            if key in values:
                yield from nodes(values[key],depth=depth+1)


def projection(values, *, source_url, expected_title=None, expected_company=None):
    jobs=[]
    for n in nodes(values):
        types=n.get('@type',[])
        if isinstance(types,str): types=[types]
        if isinstance(types,list) and any(str(t).split('/')[-1]=='JobPosting' for t in types):
            jobs.append(n)
    exact=[n for n in jobs if _key(n.get('url'))==_key(source_url)]
    selected=exact if exact else jobs
    if len(selected)!=1:
        return {}
    n=selected[0]
    org=n.get('hiringOrganization') or {}
    if not isinstance(org,dict): org={}
    if expected_company and org.get('name') and s.company_key(org['name'])!=s.company_key(expected_company):
        return {}
    if expected_title and n.get('title') and s.company_key(n['title'])!=s.company_key(expected_title):
        return {}
    facts={}
    def put(key,value,pointer):
        if isinstance(value,(str,int,float)) and not isinstance(value,bool) and str(value).strip() and len(str(value))<=1000:
            facts[key]={'value':str(value).strip(),'source_url':source_url,'pointer':pointer,'evidence':json.dumps(value,ensure_ascii=False)}
    identifier=n.get('identifier')
    if isinstance(identifier,dict): put('requisition_id',identifier.get('value'),'identifier.value')
    elif isinstance(identifier,(str,int)): put('requisition_id',identifier,'identifier')
    put('title',n.get('title'),'title');put('company',org.get('name'),'hiringOrganization.name')
    category=n.get('occupationalCategory')
    if isinstance(category,dict): category=category.get('name')
    put('function',category,'occupationalCategory')
    department=n.get('department') or org.get('department')
    if isinstance(department,dict): department=department.get('name')
    put('team',department,'department/hiringOrganization.department')
    for key in ('datePosted','validThrough','employmentType','industry'):
        put(key,n.get(key),key)
    return facts


def store_document(conn, job_url, document):
    row=conn.execute('SELECT title,company FROM jobs WHERE url=?',(job_url,)).fetchone()
    if not row:
        raise ValueError('Unknown job')
    source=document.url if hasattr(document,'url') else document['url']
    values=document.structured_data if hasattr(document,'structured_data') else document.get('structured_data',[])
    facts=projection(list(values),source_url=source,expected_title=row['title'],expected_company=row['company'])
    if not facts:
        return {}
    with s.transaction(conn):
        conn.execute("""INSERT INTO job_structured_facts VALUES(?,?,?,?,?) ON CONFLICT(job_url) DO UPDATE SET
            source_url=excluded.source_url,document_hash=excluded.document_hash,
            facts_json=excluded.facts_json,observed_at=excluded.observed_at""",
            (job_url,source,s.digest(s.dump(list(values))),s.dump(facts),s.iso()))
    return facts


def context(conn, job_url):
    row=conn.execute('SELECT * FROM job_structured_facts WHERE job_url=?',(job_url,)).fetchone()
    if not row: return {}
    facts=s.obj(row['facts_json']);kwargs={}
    for field,param,evidence in [('requisition_id','requisition_id','requisition_evidence'),
                                 ('function','job_function','function_evidence'),('team','job_team','team_evidence')]:
        if field in facts:
            kwargs[param]=facts[field]['value'][:200]
            kwargs[evidence]='Observed '+row['source_url']+' at '+facts[field]['pointer']+': '+facts[field]['evidence']
    if 'validThrough' in facts:
        try: kwargs['deadline']=s.iso(facts['validThrough']['value'])
        except (ValueError,TypeError): pass
    return kwargs

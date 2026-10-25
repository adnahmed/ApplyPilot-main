"""Durable evidence and review state for the native job-swarm research workflow."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from applypilot.discovery.repository import transaction
from applypilot.targets import store as targets

from .store import identity

OUTCOMES = ('reviewing', 'contacted', 'followed_up', 'replied', 'rejected', 'closed')
HANDS_OFF = ('contacted', 'followed_up', 'replied', 'rejected', 'closed')


def website_key(url):
    parsed = urlsplit(targets._url(url))
    if not parsed.hostname:
        raise ValueError('Company research requires an official website')
    return 'https://' + parsed.netloc.lower().removeprefix('www.')


def ensure_schema(conn):
    for sql in (
        """CREATE TABLE IF NOT EXISTS company_predictions (
            website TEXT PRIMARY KEY, company TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
            updated_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '',
            report_json TEXT NOT NULL DEFAULT '{}', outcome TEXT NOT NULL DEFAULT '',
            notes TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS company_research_documents (
            id TEXT PRIMARY KEY, website TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
            text TEXT NOT NULL, event_date TEXT, observed_at TEXT NOT NULL, authored INTEGER NOT NULL,
            first_seen_at TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}')""",
        """CREATE TABLE IF NOT EXISTS company_hiring_snapshots (
            website TEXT NOT NULL, scope TEXT NOT NULL, day TEXT NOT NULL,
            observed_at TEXT NOT NULL, detail_json TEXT NOT NULL,
            PRIMARY KEY(website,scope,day))""",
        """CREATE TABLE IF NOT EXISTS company_hiring_postings (
            website TEXT NOT NULL, scope TEXT NOT NULL, url TEXT NOT NULL,
            title TEXT NOT NULL, text TEXT NOT NULL, relevant INTEGER,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, active INTEGER NOT NULL,
            PRIMARY KEY(website,scope,url))""",
        """CREATE TABLE IF NOT EXISTS company_prediction_outcomes (
            id TEXT PRIMARY KEY, website TEXT NOT NULL, observed_at TEXT NOT NULL,
            outcome TEXT NOT NULL, notes TEXT NOT NULL, report_json TEXT NOT NULL)""",
        'CREATE INDEX IF NOT EXISTS idx_company_documents_site ON company_research_documents(website,event_date)',
    ):
        conn.execute(sql)


def eligible_companies(conn):
    """Passive evidence is stored, but only research tiers or owner intent spend."""
    latest = {}
    for row in conn.execute("""SELECT company,website,score FROM (
        SELECT company,website,score,observed_at,id,
            ROW_NUMBER() OVER (PARTITION BY website ORDER BY observed_at DESC,id DESC) AS latest_rank
        FROM company_signals) ranked WHERE latest_rank=1 ORDER BY observed_at DESC,id DESC"""):
        latest.setdefault(website_key(row['website']), dict(row))
    eligible = {key: row for key, row in latest.items() if row['score'] >= 50}
    paused = set()
    for row in conn.execute("SELECT company,website,state,origin FROM company_targets WHERE website<>''"):
        key = website_key(row['website'])
        if row['state'] != 'active':
            paused.add(key)
        elif row['origin'] == 'operator':
            eligible[key] = dict(row)
    return {key: row for key, row in eligible.items() if key not in paused}


def seed(conn):
    rows = eligible_companies(conn).values()
    with transaction(conn, resource='company-predictions'):
        for row in rows:
            conn.execute("""INSERT INTO company_predictions(website,company,updated_at)
                VALUES(?,?,?) ON CONFLICT(website) DO UPDATE SET company=excluded.company""",
                (website_key(row['website']), row['company'], targets.now()))


def _decode(row):
    value = dict(row)
    value['report'] = json.loads(value.pop('report_json'))
    return value


def get(conn, website):
    row = conn.execute('SELECT * FROM company_predictions WHERE website=?', (website_key(website),)).fetchone()
    if row is None:
        raise LookupError('Company research not found')
    return _decode(row)


def research_candidates(conn, limit=None):
    rows = conn.execute("""SELECT * FROM company_predictions WHERE status<>'paused' AND outcome NOT IN ('contacted','followed_up','replied','rejected','closed')
        ORDER BY website""")
    eligible = eligible_companies(conn)
    result = []
    for row in rows:
        if row['website'] in eligible:
            result.append(_decode(row))
            if limit is not None and len(result) == limit:
                break
    return result


def enabled(conn, website):
    from .store import settings
    cfg = settings(conn)
    if not cfg['enabled'] or not cfg['predict_enabled']:
        return False
    return website_key(website) in eligible_companies(conn)


def save_report(conn, company, report, *, status, error=''):
    """A user pause, outcome, or retry during inference wins over the stale worker."""
    with transaction(conn, resource='company-predictions'):
        if not enabled(conn, company['website']):
            return False
        current = get(conn, company['website'])
        previous = current['report']
        completed = previous if previous.get('draft') else previous.get('previous_report')
        saved = dict(report)
        if completed and not report.get('draft') and status != 'low_fit':
            saved['previous_report'] = {key: value for key, value in completed.items() if key != 'previous_report'}
        updated = conn.execute("""UPDATE company_predictions SET report_json=?,status=?,last_error=?,
            updated_at=?,revision=revision+1 WHERE website=? AND revision=? AND status<>'paused'
            AND outcome NOT IN ('contacted','followed_up','replied','rejected','closed')""",
            (targets.dump(saved), status, error[:1200],
             targets.now(),
             company['website'], company['revision']))
        if updated.rowcount != 1:
            return False
        company['revision'] += 1
    return True


def control(conn, website, action):
    if action not in ('retry', 'pause', 'resume'):
        raise ValueError('Unknown predictive research action')
    with transaction(conn, resource='company-predictions'):
        company = get(conn, website)
        if action != 'pause' and company['outcome'] in HANDS_OFF:
            raise ValueError('Set the outcome to reviewing before researching this company again')
        conn.execute("""UPDATE company_predictions SET status=?,updated_at=?,revision=revision+1
            WHERE website=?""", ('paused' if action == 'pause' else 'queued', targets.now(),
                                 company['website']))
    return get(conn, website)


def record_outcome(conn, website, outcome, revision, notes=''):
    if outcome not in OUTCOMES or not isinstance(notes, str) or len(notes) > 4000:
        raise ValueError('Choose a research outcome and notes of at most 4,000 characters')
    with transaction(conn, resource='company-predictions'):
        company = get(conn, website)
        if str(company['revision']) != str(revision):
            raise ValueError('Company research changed; reload before saving an outcome')
        stamp = targets.now()
        conn.execute("""INSERT INTO company_prediction_outcomes
            (id,website,observed_at,outcome,notes,report_json) VALUES(?,?,?,?,?,?)""",
            (identity(company['website'], stamp, outcome), company['website'], stamp, outcome, notes,
             targets.dump(company['report'])))
        conn.execute("""UPDATE company_predictions SET outcome=?,notes=?,updated_at=?,revision=revision+1 WHERE website=?""", (outcome, notes, stamp, company['website']))
    return get(conn, website)


def save_documents(conn, website, documents):
    website = website_key(website)
    with transaction(conn, resource='company-predictions'):
        for doc in documents:
            url = targets._url(doc['url'])
            text = str(doc['text']).strip()
            if not text:
                continue
            event = doc.get('event_date')
            key = identity(website, url, event, text)
            doc['id'] = key
            conn.execute("""INSERT INTO company_research_documents
                (id,website,url,title,text,event_date,observed_at,authored,first_seen_at,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at,
                metadata_json=excluded.metadata_json,authored=excluded.authored,title=excluded.title""",
                (key, website, url, str(doc.get('title') or url)[:500], text, event,
                 doc.get('observed_at') or targets.now(), int(bool(doc.get('authored') and event)), targets.now(),
                 targets.dump({key: doc[key] for key in ('primary', 'truncated', 'coverage_gaps', 'date_kind') if key in doc})))


def documents(conn, website):
    rows = conn.execute("""SELECT * FROM (
        SELECT d.*,ROW_NUMBER() OVER (PARTITION BY url ORDER BY observed_at DESC,id DESC) AS revision_rank
        FROM company_research_documents d WHERE website=?) ranked WHERE revision_rank=1
        ORDER BY COALESCE(event_date,first_seen_at) DESC,observed_at DESC LIMIT 60""",
        (website_key(website),)).fetchall()
    result = []
    for row in rows:
        # Revisions of one article are one authored event, not a semantic trajectory.
        value = dict(row)
        value.pop('revision_rank')
        value.update(json.loads(value.pop('metadata_json')))
        value['authored'] = bool(value['authored'])
        result.append(value)
    return result


def record_observations(conn, website, observations):
    """Consume complete native board payloads; return compact public observations."""
    from applypilot.discovery.jobseek import assemble_hiring_observations
    observations = assemble_hiring_observations(observations)
    cleaned = []
    with transaction(conn, resource='company-predictions'):
        for observation in observations:
            value = dict(observation)
            snapshot = value.pop('hiring_snapshot', None)
            cleaned.append(value)
            if not snapshot or snapshot.get('complete') is not True or not website:
                continue
            stamp = snapshot['observed_at']
            website = website_key(website)
            for posting in snapshot['postings']:
                relevant = posting.get('relevant')
                conn.execute("""INSERT INTO company_hiring_postings
                    (website,scope,url,title,text,relevant,first_seen_at,last_seen_at,active)
                    VALUES(?,?,?,?,?,?,?,?,1) ON CONFLICT(website,scope,url) DO UPDATE SET
                    title=excluded.title,text=excluded.text,relevant=excluded.relevant,
                    last_seen_at=excluded.last_seen_at,active=1""",
                    (website, snapshot['scope'], posting['url'], posting.get('title') or '',
                     posting.get('text') or '', int(relevant) if relevant is not None else None, stamp, stamp))
            conn.execute("""UPDATE company_hiring_postings SET active=0
                WHERE website=? AND scope=? AND last_seen_at<>? AND active=1""",
                (website, snapshot['scope'], stamp))
            value['hiring_counts'] = {key: snapshot[key] for key in ('total', 'relevant', 'scope', 'observed_at')}
            conn.execute("""INSERT INTO company_hiring_snapshots(website,scope,day,observed_at,detail_json)
                VALUES(?,?,?,?,?) ON CONFLICT(website,scope,day) DO UPDATE SET
                observed_at=excluded.observed_at,detail_json=excluded.detail_json""",
                (website, snapshot['scope'], stamp[:10], stamp,
                 targets.dump({key: val for key, val in snapshot.items() if key != 'postings'})))
    return cleaned


def hiring_corpus(conn, website):
    website = website_key(website)
    cutoff = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    snapshots = [json.loads(row['detail_json']) for row in conn.execute("""SELECT detail_json
        FROM company_hiring_snapshots WHERE website=? AND observed_at>=? ORDER BY observed_at""", (website, cutoff))]
    records = {}
    for row in conn.execute("""SELECT * FROM company_hiring_postings WHERE website=?
        AND (last_seen_at>=? OR active=1)""", (website, cutoff)):
        value = dict(row)
        value.update(active=bool(row['active']), relevant=bool(row['relevant']) if row['relevant'] is not None else None,
                     company_website=website, is_target=True)
        records[(row['scope'], row['url'])] = value
    # Actual matching vacancies form the cross-company taxonomy; hypotheses never enter jobs.
    peers = conn.execute("""SELECT j.url,j.title,j.company,j.full_description,j.description,
        p.first_seen_at,p.observed_at,j.discovery_role_status FROM jobs j JOIN (
            SELECT job_url,MIN(first_seen_at) AS first_seen_at,MAX(last_seen_at) AS observed_at
            FROM discovery_postings WHERE source_status='open' GROUP BY job_url) p ON p.job_url=j.url
        WHERE j.discovery_source_status='open' AND j.discovery_role_status='relevant'
        AND p.observed_at>=? ORDER BY p.observed_at DESC LIMIT 400""", (cutoff,)).fetchall()
    own_urls = {row['url'] for row in records.values()}
    target = conn.execute('SELECT company FROM company_predictions WHERE website=?', (website,)).fetchone()
    for row in peers:
        if row['url'] in own_urls:
            continue
        is_target = bool(target and row['company'] and row['company'].casefold() == target['company'].casefold())
        records[('', row['url'])] = {'url': row['url'], 'title': row['title'] or '',
            'text': (row['full_description'] or row['description'] or '')[:6000],
            'first_seen_at': row['first_seen_at'], 'last_seen_at': row['observed_at'],
            'active': True, 'relevant': True, 'is_target': is_target,
            'website': website if is_target else '', 'company_website': website}
    return list(records.values()), [{k: v for k, v in row.items() if k != 'postings'} for row in snapshots]


def overview(conn):
    if not conn.execute("""SELECT 1 FROM information_schema.tables WHERE table_schema=current_schema()
        AND table_name='company_predictions'""").fetchone():
        return {'companies': [], 'outcomes': {}, 'implementation': 'job-swarm-native-v1'}
    companies = [_decode(row) for row in conn.execute("""SELECT * FROM company_predictions
        ORDER BY CASE status WHEN 'drafted' THEN 0 WHEN 'error' THEN 1 ELSE 2 END,updated_at DESC LIMIT 100""")]
    eligible = eligible_companies(conn)
    for company in companies:
        company['research_eligible'] = company['website'] in eligible
        for report in (company['report'], company['report'].get('previous_report', {})):
            report.get('analytics', {}).pop('state_vector', None)
    outcomes = {row['outcome']: row['n'] for row in conn.execute(
        "SELECT outcome,COUNT(*) AS n FROM company_predictions WHERE outcome<>'' GROUP BY outcome")}
    return {'companies': companies, 'outcomes': outcomes, 'implementation': 'job-swarm-native-v1'}

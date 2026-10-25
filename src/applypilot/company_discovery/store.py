"""Company-source registry and evidence; target/job state stays in its existing tables."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from applypilot.discovery.repository import transaction
from applypilot.targets import store as targets

KINDS = ('portfolio_directory', 'accelerator', 'funding_database', 'funding_news',
         'vc_job_board', 'government_program', 'company_registry', 'product_launch', 'community', 'other')
SIGNALS = ('company_discovered', 'funding_round', 'fundraising', 'accelerator_cohort', 'vc_portfolio_addition',
           'grant_or_program', 'product_launch', 'expansion', 'founder_hiring', 'careers_page_detected')
DEFAULTS = {'enabled': True, 'discover_sources': True, 'companies_per_source': 8,
            'predict_enabled': True, 'predict_drafts_per_cycle': 2}


def identity(*parts):
    return hashlib.sha256(targets.dump(parts).encode()).hexdigest()


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS company_discovery_settings (
        id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL)""")
    conn.execute('INSERT INTO company_discovery_settings(id,config_json) VALUES(1,?) ON CONFLICT DO NOTHING',
                 (targets.dump(DEFAULTS),))
    conn.execute("""CREATE TABLE IF NOT EXISTS company_sources (
        id TEXT PRIMARY KEY, url TEXT UNIQUE NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL,
        market TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'candidate',
        evidence_json TEXT NOT NULL DEFAULT '{}',
        last_checked_at TEXT, last_error TEXT NOT NULL DEFAULT '', runs INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS company_signals (
        id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES company_sources(id),
        company TEXT NOT NULL, website TEXT NOT NULL, signal_type TEXT NOT NULL,
        source_url TEXT NOT NULL, observed_at TEXT NOT NULL, event_date TEXT,
        score INTEGER NOT NULL, detail_json TEXT NOT NULL,
        target_id TEXT REFERENCES company_targets(id))""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_company_signals_site ON company_signals(website)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_company_signals_source ON company_signals(source_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_company_signals_target ON company_signals(target_id,observed_at DESC)')
    from .predictive_store import ensure_schema as ensure_predictions
    ensure_predictions(conn)
    import yaml

    from applypilot import config
    seeds = yaml.safe_load((config.CONFIG_DIR / 'company_discovery.yaml').read_text(encoding='utf-8'))
    for row in seeds['sources']:
        row = {**row, 'url': row['url'].format(year=datetime.now(UTC).year)}
        conn.execute("""INSERT INTO company_sources(id,url,name,kind,market,state)
            VALUES(?,?,?,?,?,'candidate') ON CONFLICT DO NOTHING""",
            (identity(row['url']), row['url'], row['name'], row['kind'], row.get('market', '')))


def settings(conn):
    row = conn.execute('SELECT * FROM company_discovery_settings WHERE id=1').fetchone()
    saved = json.loads(row['config_json'])
    return {**DEFAULTS, **{key: saved[key] for key in DEFAULTS if key in saved}, 'revision': row['revision']}


def save_settings(conn, data):
    if set(data) != {*DEFAULTS, 'revision'}:
        raise ValueError('Supply the displayed company discovery settings')
    for key in ('enabled', 'discover_sources', 'predict_enabled'):
        if type(data[key]) is not bool:
            raise ValueError(key + ' must be a boolean')
    for key, maximum in (('companies_per_source', 25), ('predict_drafts_per_cycle', 3)):
        if type(data[key]) is not int or not 1 <= data[key] <= maximum:
            raise ValueError(f'{key} must be between 1 and {maximum}')
    with transaction(conn, resource='company-discovery'):
        updated = conn.execute('UPDATE company_discovery_settings SET config_json=?,revision=revision+1 WHERE id=1 AND revision=?',
            (targets.dump({key: data[key] for key in DEFAULTS}), data['revision']))
        if updated.rowcount != 1:
            raise ValueError('Company discovery settings changed; reload before saving')
    return settings(conn)


def add_source(conn, data, *, evidence=None):
    url = targets._url(data.get('url'))
    if not url or data.get('kind') not in KINDS:
        raise ValueError('A public source URL and source type are required')
    name = str(data.get('name') or url).strip()[:200]
    source_id = identity(url)
    with transaction(conn, resource='company-discovery'):
        conn.execute("""INSERT INTO company_sources(id,url,name,kind,market,evidence_json)
            VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
            (source_id, url, name, data['kind'], str(data.get('market') or '')[:200],
             targets.dump(evidence or {})))
    row = conn.execute('SELECT id,state FROM company_sources WHERE url=?', (url,)).fetchone()
    return dict(row)


def control_source(conn, source_id, action):
    if action not in ('pause', 'resume', 'check'):
        raise ValueError('Unknown company source action')
    with transaction(conn, resource='company-discovery'):
        updated = conn.execute('UPDATE company_sources SET state=? WHERE id=?',
            ('paused' if action == 'pause' else 'candidate', source_id))
        if updated.rowcount != 1:
            raise LookupError('Company source not found')
    return {'id': source_id, 'state': 'paused' if action == 'pause' else 'candidate'}


def signal_key(value):
    return identity(value['website'], value['source_url'], value['signal_type'], value.get('event_date'))


def saved_signal(conn, value):
    row = conn.execute('SELECT detail_json FROM company_signals WHERE id=?', (signal_key(value),)).fetchone()
    if row is None:
        # A second directory mention must not restart passive primary research.
        row = conn.execute('SELECT detail_json FROM company_signals WHERE website=? '
                           'ORDER BY observed_at DESC,id DESC LIMIT 1', (value['website'],)).fetchone()
    return json.loads(row['detail_json']) if row else {}


def save_signal(conn, source_id, value, score):
    key = signal_key(value)
    with transaction(conn, resource='company-discovery'):
        target = conn.execute('SELECT id FROM company_targets WHERE identity_key=?',
                              (targets.identity(value),)).fetchone()
        from .predictive_store import record_observations
        if value.get('verification', {}).get('careers_observations'):
            value['verification']['careers_observations'] = record_observations(
                conn, value['website'], value['verification']['careers_observations'])
        conn.execute("""INSERT INTO company_signals
            (id,source_id,company,website,signal_type,source_url,observed_at,event_date,score,detail_json,target_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            company=excluded.company,observed_at=excluded.observed_at,score=excluded.score,detail_json=excluded.detail_json,
            target_id=COALESCE(excluded.target_id,company_signals.target_id)""",
            (key, source_id, value['company'], value['website'], value['signal_type'], value['source_url'],
             value.get('observed_at') or targets.now(), value.get('event_date'), score, targets.dump(value),
             target['id'] if target else None))
    return key


def watch(conn, signal_id, *, automatic=False):
    row = conn.execute('SELECT * FROM company_signals WHERE id=?', (signal_id,)).fetchone()
    if row is None:
        raise LookupError('Company signal not found')
    value = json.loads(row['detail_json'])
    result = targets.save(conn, {'company': row['company'], 'website': row['website'],
        'careers_url': value.get('careers_url', ''), 'action': 'watch', 'priority': 'normal', 'cadence': 'weekly'},
        origin='company_discovery' if automatic else 'operator')
    boards = []
    for signal in conn.execute('SELECT detail_json FROM company_signals WHERE website=? ORDER BY observed_at,id', (row['website'],)):
        boards.extend(json.loads(signal['detail_json']).get('jobseek_boards', []))
    if boards:
        targets.attach_boards(conn, result['target']['id'], boards)
        result['target'] = targets.get(conn, result['target']['id'])
    with transaction(conn, resource='company-discovery'):
        conn.execute('UPDATE company_signals SET target_id=? WHERE website=?',
                     (result['target']['id'], row['website']))
    return result


def finish_source(conn, source_id, *, error='', evidence=None, resource_usage=None):
    """Publish source quality/history. Traversal position stays in Workflow history."""
    with transaction(conn, resource='company-discovery'):
        row = conn.execute('SELECT evidence_json FROM company_sources WHERE id=?', (source_id,)).fetchone()
        previous = json.loads(row['evidence_json'])
        merged = {key: value for key, value in previous.items()
                  if key not in {'next_page', 'catalog_offset', 'feed_cursor', 'allocation'}}
        merged.update(evidence or {})
        conn.execute("""UPDATE company_sources SET last_checked_at=?,last_error=?,runs=runs+1,
            evidence_json=?,kind=COALESCE(?,kind),state=CASE WHEN state='paused' THEN state
            WHEN ? THEN 'active' ELSE state END WHERE id=?""",
            (targets.now(), str(error)[:1200], targets.dump(merged),
             evidence.get('kind') if evidence else None, bool(evidence and evidence.get('accepted')), source_id))


def overview(conn):
    from .source_policy import source_metrics, target_research_allowed, tier
    sources = source_metrics(conn)
    signals = [dict(row) for row in conn.execute("""SELECT c.*,s.name AS source_name,t.state AS target_state,t.origin AS target_origin
        FROM company_signals c JOIN company_sources s ON s.id=c.source_id
        LEFT JOIN company_targets t ON t.id=c.target_id ORDER BY c.score DESC,c.observed_at DESC LIMIT 100""")]
    watch_eligible = {}
    for row in signals:
        row['detail'] = json.loads(row.pop('detail_json'))
        row['tier'] = tier(row['score'])
        if row['target_id']:
            if row['target_id'] not in watch_eligible:
                watch_eligible[row['target_id']] = target_research_allowed(conn,
                    {'id': row['target_id'], 'origin': row['target_origin']})
            row['watch_eligible'] = watch_eligible[row['target_id']]
            row['decision'] = (row['target_state'] if row['target_state'] != 'active' else
                'watching' if row['watch_eligible'] else 'research' if row['score'] >= 50 else row['tier'])
        else:
            row['decision'] = 'research' if row['score'] >= 50 else row['tier']
    from .predictive_store import overview as prediction_overview
    return {'settings': settings(conn), 'sources': sources, 'signals': signals,
            'predictive': prediction_overview(conn)}

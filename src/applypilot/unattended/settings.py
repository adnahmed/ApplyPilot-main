"""Outreach preferences for the owner's saved accounts."""
from __future__ import annotations

import json
import os
from pathlib import Path
from applypilot import config

DEFAULTS = {
    "version": 2, "enabled": True, "channels": ["email"],
    "accounts": {}, "daily_send_limit": -1, "daily_research_limit": -1,
    "research_jobs_per_cycle": 3, "people_per_job": 4, "cycle_seconds": 300,
    "referral_first_hours": 0, "research_grace_minutes": 0,
    "send_grace_seconds": 180,
    "account_spacing_seconds": {"email": 60, "linkedin": 600, "x": 120},
    "linkedin_daily_messages": 20,
    "allow_relevant_cold_outreach": True, "automatic_replies": True,
    "network_files": [], "network_page_size": 30, "network_refresh_days": 7,
    "pipeline_timeout_seconds": 7200, "cycle_timeout_seconds": 900,
    "backup_days": 7, "log_retention_days": 14,
    "alerts_webhook_env": "APPLYPILOT_ALERT_WEBHOOK", "digest_hours": 24,
}


# Preserve saved limits when reading an older configuration.
LEGACY_DEFAULTS = {k: v for k, v in DEFAULTS.items() if k != "send_grace_seconds"}
LEGACY_DEFAULTS.update(version=1, enabled=False, referral_first_hours=12, research_grace_minutes=10,
                       daily_send_limit=10, daily_research_limit=20)


def path():
    return config.APP_DIR / "unattended.json"


def validate(data):
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError("Unknown unattended setting")
    cfg = DEFAULTS | data
    if type(cfg['version']) is not int or cfg['version'] not in {1, 2}:
        raise ValueError("Unsupported unattended policy version")
    if cfg['version'] == 1:
        if 'send_grace_seconds' in data:
            raise ValueError("Legacy policies cannot add new unsigned settings")
        for key in ('daily_send_limit', 'daily_research_limit'):
            if key not in data:
                cfg[key] = LEGACY_DEFAULTS[key]
        # This upgrade changes timing, not accounts, recipients or send limits.
        cfg.update(version=2, referral_first_hours=0, research_grace_minutes=0,
                   send_grace_seconds=180)
    for k in ('enabled', 'allow_relevant_cold_outreach', 'automatic_replies'):
        if type(cfg[k]) is not bool:
            raise ValueError(f"{k} must be a boolean")
    if not isinstance(cfg['channels'], list) or len(set(cfg['channels'])) != len(cfg['channels']) or not set(cfg['channels']) <= {'email','linkedin','x'}:
        raise ValueError("Channels must be unique: email, linkedin, x")
    from .budgets import validate_limit
    for key in ('daily_send_limit', 'daily_research_limit'):
        validate_limit(cfg[key], key)
    if type(cfg['research_jobs_per_cycle']) is not int or cfg['research_jobs_per_cycle'] < 0:
        raise ValueError('research_jobs_per_cycle must be a nonnegative integer')
    for key, low, high in (
        ('people_per_job',1,10),
        ('cycle_seconds',60,86400), ('referral_first_hours',0,24),
        ('research_grace_minutes',0,30), ('send_grace_seconds',0,180), ('network_page_size',1,50),
        ('network_refresh_days',1,90), ('pipeline_timeout_seconds',300,86400),
        ('cycle_timeout_seconds',60,3600), ('backup_days',1,30),
        ('log_retention_days',1,90), ('digest_hours',1,168),
    ):
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key} must be an integer in [{low}, {high}]")
    spacing = cfg['account_spacing_seconds']
    if (not isinstance(spacing, dict) or set(spacing) != {'email','linkedin','x'}
            or any(type(value) is not int or not 1 <= value <= 86400 for value in spacing.values())):
        raise ValueError('Specify positive account spacing for email, linkedin and x')
    if type(cfg['linkedin_daily_messages']) is not int or not 1 <= cfg['linkedin_daily_messages'] <= 100:
        raise ValueError('LinkedIn daily message limit must be between 1 and 100')
    if not isinstance(cfg['accounts'], dict) or set(cfg['accounts']) - {'linkedin','x'}:
        raise ValueError("accounts must map linkedin/x to your exact profile URL")
    from applypilot.referrals.identity import profile_key
    for channel, url in cfg['accounts'].items():
        key = profile_key(url)
        prefix = 'x:' if channel == 'x' else 'linkedin:'
        if not key or not key.startswith(prefix):
            raise ValueError(f"Invalid owner profile for {channel}")
    if cfg['enabled'] and any(c not in cfg['accounts'] for c in cfg['channels'] if c != 'email'):
        raise ValueError("Bind each enabled social channel to your own profile during setup")
    if not isinstance(cfg['network_files'], list) or len(cfg['network_files']) > 20 or any(not isinstance(p,str) for p in cfg['network_files']):
        raise ValueError("network_files must be up to 20 local export paths")
    if cfg['alerts_webhook_env'] != 'APPLYPILOT_ALERT_WEBHOOK':
        raise ValueError("Alert URLs belong in APPLYPILOT_ALERT_WEBHOOK, never in harvested data")
    return cfg


def environment_enabled():
    value = os.getenv('APPLYPILOT_OUTREACH_ENABLED', 'true').strip().lower()
    if value not in {'true', 'false', '1', '0', 'yes', 'no', 'on', 'off'}:
        raise ValueError('APPLYPILOT_OUTREACH_ENABLED must be true or false')
    return value in {'true', '1', 'yes', 'on'}


def load(*, apply_environment=True):
    p = path()
    if p.exists():
        if p.stat().st_size > 64000:
            raise ValueError("Unattended policy exceeds 64 KB")
        raw = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(raw, dict):
            raw = {'daily_send_limit': 10, 'daily_research_limit': 20} | raw
    else:
        raw = {'channels': ['email'], 'accounts': {}}
        personal = config.load_profile().get('personal', {}) if config.PROFILE_PATH.exists() else {}
        for channel, field in (('linkedin', 'linkedin_url'), ('x', 'x_url')):
            if personal.get(field):
                raw['channels'].append(channel)
                raw['accounts'][channel] = personal[field]
    cfg = validate(raw)
    if apply_environment:
        cfg['enabled'] = cfg['enabled'] and environment_enabled()
    return cfg


def save(data):
    cfg=validate(data)
    p=path();p.parent.mkdir(parents=True,exist_ok=True)
    temporary=p.with_suffix('.tmp')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='utf-8') as f:
        json.dump(cfg,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
    os.replace(temporary,p)
    return cfg


def update_limits(conn, *, daily_limit=None, daily_research_limit=None, contacts_per_job=None, send_grace_seconds=None, expected_revision=None):
    """Shared CLI/web owner control; preserve identities and existing guard rails.

    A website supplies the exact revision it displayed, so a stale tab cannot
    silently overwrite newer settings.
    """
    from .policy import active
    from applypilot.referrals import store as s
    from .budgets import validate_limit
    if daily_limit is not None:
        validate_limit(daily_limit, 'Daily outbound limit')
    if daily_research_limit is not None:
        validate_limit(daily_research_limit, 'Daily research limit')
    if contacts_per_job is not None:
        validate_limit(contacts_per_job, 'Contacts per posting')
    if send_grace_seconds is not None and (type(send_grace_seconds) is not int or not 0 <= send_grace_seconds <= 180):
        raise ValueError('Cancel window must be an integer between 0 and 180 seconds')
    with s.transaction(conn):
        cfg = load()
        saved_enabled = load(apply_environment=False)['enabled']
        grant = active(conn, cfg)
        if expected_revision is not None and expected_revision != controls_revision(conn, cfg):
            raise ValueError('Settings changed in another window. Reload before saving.')
        if daily_limit is not None:
            cfg['daily_send_limit'] = daily_limit
        if daily_research_limit is not None:
            cfg['daily_research_limit'] = daily_research_limit
        if send_grace_seconds is not None:
            cfg['send_grace_seconds'] = send_grace_seconds
        cfg = validate(cfg)
        changes = {'daily_send_limit': cfg['daily_send_limit']}
        if daily_research_limit is not None:
            changes.update(daily_search_limit=daily_research_limit, daily_plan_limit=daily_research_limit)
        if contacts_per_job is not None:
            changes['contacts_per_job'] = contacts_per_job
        s.configure(conn, **changes)
        if grant:
            rules = grant['rules'] | {'daily_limit': cfg['daily_send_limit'], 'policy_hash': s.digest(s.dump(cfg))}
            conn.execute('UPDATE referral_auto_grants SET rules_json=? WHERE id=?', (s.dump(rules), grant['id']))
        save(cfg | {'enabled': saved_enabled})
    return {'daily_outreach_limit': cfg['daily_send_limit'], 'daily_research_limit': cfg['daily_research_limit'], 'send_grace_seconds': cfg['send_grace_seconds'],
            'channels': cfg['channels'], 'saved': True}


def controls_revision(conn, cfg):
    """Include the editable contact budget in the stale-tab guard, not the grant."""
    from applypilot.referrals import store as s
    contacts = s.policy(conn)['contacts_per_job'] if conn is not None else s.DEFAULTS['contacts_per_job']
    return s.digest(s.dump({'unattended': cfg, 'contacts_per_job': contacts}))

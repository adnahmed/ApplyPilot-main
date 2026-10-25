"""Read-only warm-up and domain facts from external infrastructure."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3


def observe_warmup(sender):
    path = os.getenv('APPLYPILOT_DARKZWARMER_DB')
    if not path:
        return {'state': 'not_configured', 'provider': 'darkzwarmer'}
    database = Path(path).expanduser().resolve(strict=True)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as conn:
        row = conn.execute('SELECT current_day,emails_sent_today FROM warmup_state WHERE inbox_email=?',
                           (sender,)).fetchone()
    if row is None:
        return {'state': 'mailbox_not_registered', 'provider': 'darkzwarmer'}
    return {'state': 'observed', 'provider': 'darkzwarmer',
            'day': int(row[0]), 'sent_today': int(row[1])}


def domain_status(sender):
    import httpx
    domain = sender.rsplit('@', 1)[1]
    if not re.fullmatch(r'[A-Za-z0-9.-]{1,253}', domain):
        raise ValueError('Invalid sender domain')
    queries = [('mx', domain, 'MX'), ('spf', domain, 'TXT'), ('dmarc', '_dmarc.' + domain, 'TXT')]
    selector = os.getenv('APPLYPILOT_EMAIL_DKIM_SELECTOR', '')
    if selector:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,63}', selector):
            raise ValueError('Invalid DKIM selector')
        queries.append(('dkim', selector + '._domainkey.' + domain, 'TXT'))
    result = {'state': 'observed', 'domain': domain, 'records': {}}
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        for label, name, type_ in queries:
            response = client.get('https://cloudflare-dns.com/dns-query',
                params={'name': name, 'type': type_}, headers={'Accept': 'application/dns-json'})
            response.raise_for_status()
            body = response.json()
            if body.get('Status') not in {0, 3}:
                raise RuntimeError('DNS observation failed')
            records = [str(item['data']) for item in body.get('Answer', []) if 'data' in item]
            if label == 'spf':
                records = [record for record in records if 'v=spf1' in record.lower()]
            result['records'][label] = records
    return result

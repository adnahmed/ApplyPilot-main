"""Licensed feed contracts supplied by the account owner, never by remote pages."""
from __future__ import annotations

import json
import os
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from applypilot import config
from applypilot.integrations.types import SourceError


def configured():
    path = config.APP_DIR / 'company-feed-contracts.json'
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > 100000:
        raise SourceError('Company feed contracts must be a regular JSON file under 100 KB', 'configuration')
    try:
        value = json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as exc:
        raise SourceError('Company feed contracts could not be read', 'configuration') from exc
    if not isinstance(value, list) or len(value) > 20:
        raise SourceError('Company feed contracts must be a list of at most 20 provider contracts', 'configuration')
    for row in value:
        allowed = {'name', 'url', 'method', 'credential_env', 'auth_header', 'auth_prefix', 'body',
                   'records_path', 'next_path', 'pagination', 'limit_param', 'cursor_param', 'parameters_in'}
        if not isinstance(row, dict) or set(row) - allowed:
            raise SourceError('Unknown licensed feed contract field', 'configuration')
        url = urlsplit(row.get('url', ''))
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or not (url.hostname == 'magnitt.com' or url.hostname.endswith('.magnitt.com'))):
            raise SourceError('Licensed MAGNiTT contracts must use the account-provided HTTPS MAGNiTT endpoint', 'configuration')
        if any(re.search('key|token|password|secret', key, re.I) for key, _ in parse_qsl(url.query)):
            raise SourceError('Keep licensed API credentials in request headers, never URLs', 'configuration')
        if row.get('credential_env') != 'MAGNITT_API_KEY':
            raise SourceError('MAGNiTT contracts must reference MAGNITT_API_KEY', 'configuration')
        header = row.get('auth_header', '')
        if (not isinstance(header, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', header)
                or header.lower() in {'host', 'cookie', 'content-length', 'connection', 'transfer-encoding', 'origin', 'referer'}):
            raise SourceError('Supply the authentication header from your licensed API documentation', 'configuration')
        if (row.get('method', 'GET') not in {'GET', 'POST'} or
                row.get('pagination', 'cursor') not in {'cursor', 'offset'} or
                row.get('parameters_in', 'query') not in {'query', 'body'}):
            raise SourceError('Unsupported licensed feed request or pagination convention', 'configuration')
        if row.get('parameters_in') == 'body' and row.get('method', 'GET') != 'POST':
            raise SourceError('Body pagination requires a POST feed contract', 'configuration')
        for key in ('records_path', 'next_path', 'limit_param', 'cursor_param'):
            if key in row and (not isinstance(row[key], str) or not re.fullmatch(r'[A-Za-z0-9_.-]{0,100}', row[key])):
                raise SourceError('Invalid licensed feed JSON path or parameter', 'configuration')
        if not isinstance(row.get('auth_prefix', ''), str) or any(c in row.get('auth_prefix', '') for c in '\r\n'):
            raise SourceError('Invalid licensed feed authorization prefix', 'configuration')
        if not isinstance(row.get('body', {}), dict):
            raise SourceError('Licensed feed request body must be an object', 'configuration')
    return value


def _path(value, path):
    for key in path.split('.') if path else ():
        if not isinstance(value, dict) or key not in value:
            raise SourceError('Licensed feed response does not match its configured JSON paths', 'schema')
        value = value[key]
    return value


def load(source, *, before_request, limit):
    host = urlsplit(source['url']).hostname or ''
    if host != 'magnitt.com' and not host.endswith('.magnitt.com'):
        return None
    contract = next((row for row in configured() if row['url'] == source['url']), None)
    if contract is None:
        return None
    from .feeds import _document, _request
    secret = os.getenv('MAGNITT_API_KEY', '')
    if not secret:
        raise SourceError('Licensed MAGNiTT feed requires MAGNITT_API_KEY', 'auth')
    previous = json.loads(source.get('evidence_json') or '{}').get('feed_cursor')
    if previous is not None and (not isinstance(previous, (str, int)) or len(str(previous)) > 1000):
        raise SourceError('Invalid licensed feed checkpoint', 'schema')
    parameters = {}
    if contract.get('limit_param'):
        parameters[contract['limit_param']] = limit
    if contract.get('cursor_param') and previous is not None:
        parameters[contract['cursor_param']] = previous
    body = dict(contract.get('body', {})) if contract.get('method', 'GET') == 'POST' else None
    url = contract['url']
    if contract.get('parameters_in', 'query') == 'body':
        body.update(parameters)
    else:
        parsed = urlsplit(url)
        url = urlunsplit(parsed._replace(query=urlencode({**dict(parse_qsl(parsed.query)), **parameters})))
    raw = _request(url, before_request,
        headers={contract['auth_header']: contract.get('auth_prefix', '') + secret}, body=body)
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise SourceError('Licensed feed did not return JSON', 'schema') from exc
    rows = _path(value, contract.get('records_path', ''))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or len(rows) > limit:
        raise SourceError('Licensed feed must honor its bounded record limit', 'schema')
    if contract.get('next_path'):
        following = _path(value, contract['next_path'])
    elif contract.get('pagination') == 'offset':
        if previous is not None and type(previous) is not int:
            raise SourceError('Licensed feed offset must be an integer', 'schema')
        following = (previous or 0) + len(rows) if len(rows) == limit else None
    else:
        following = None
        if len(rows) == limit:
            raise SourceError('A full licensed feed page requires a next-cursor path', 'configuration')
    if following is not None and (not isinstance(following, (str, int)) or following == previous or len(str(following)) > 1000):
        raise SourceError('Licensed feed returned an invalid or repeated continuation', 'schema')
    return _document(source, rows, 'magnitt_licensed', notices=(
        'Licensed account feed; the configured provider contract determines coverage. Unknown dates stay unknown.',)), following

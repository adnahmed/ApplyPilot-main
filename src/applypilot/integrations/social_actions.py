"""Finite XActions operations with exact approval and durable effect evidence."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import re
import shutil
import hashlib

from applypilot.orchestration.contracts import digest

TOOLS = {'publish_response': 'x_reply', 'follow_account': 'x_follow', 'send_message': 'x_send_dm',
         'send_connection': None}


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS professional_actions(
        operation_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL,
        input_json TEXT NOT NULL, approval_hash TEXT, state TEXT NOT NULL DEFAULT 'draft',
        provider_id TEXT, evidence_json TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")


def prepare(conn, kind, account_id, arguments):
    if kind not in TOOLS:
        raise ValueError('Unsupported professional action')
    expected = ({'url', 'text'} if kind == 'publish_response' else {'username'} if kind == 'follow_account'
                else {'username', 'message'} if kind == 'send_message' else {'profile_url', 'message'})
    if not isinstance(arguments, dict) or set(arguments) != expected:
        raise ValueError('Professional action arguments do not match the exact operation')
    if any(not isinstance(value, str) or not value.strip() or len(value) > 4000 for value in arguments.values()):
        raise ValueError('Professional action requires bounded explicit arguments')
    if 'username' in arguments and not re.fullmatch(r'[A-Za-z0-9_]{1,15}', arguments['username']):
        raise ValueError('Use the exact X username')
    if kind == 'publish_response' and (not re.fullmatch(r'https://(?:x|twitter)\.com/[A-Za-z0-9_]+/status/[0-9]+', arguments['url']) or len(arguments['text']) > 280):
        raise ValueError('Use an exact X post URL and a response of at most 280 characters')
    if kind == 'send_connection':
        from applypilot.referrals.identity import profile_key
        if not str(profile_key(arguments['profile_url']) or '').startswith('linkedin:'):
            raise ValueError('Use the exact LinkedIn person profile')
    value = {'kind': kind, 'account_id': account_id, 'arguments': arguments}
    operation_id = digest(value)
    conn.execute("""INSERT INTO professional_actions(operation_id,account_id,kind,input_json)
        VALUES(?,?,?,?) ON CONFLICT(operation_id) DO NOTHING""",
        (operation_id, account_id, kind, json.dumps(value, sort_keys=True)))
    return {'operation_id': operation_id, 'approval_hash': digest(value), 'state': 'draft'}


def approve(conn, operation_id, expected_hash):
    from applypilot.referrals.store import transaction
    with transaction(conn):
        row = conn.execute('SELECT * FROM professional_actions WHERE operation_id=? FOR UPDATE', (operation_id,)).fetchone()
        if not row or digest(json.loads(row['input_json'])) != expected_hash or row['state'] != 'draft':
            raise ValueError('Professional action approval no longer matches')
        conn.execute("UPDATE professional_actions SET approval_hash=?,state='approved',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                     (expected_hash, operation_id))
    return {'operation_id': operation_id, 'state': 'approved'}


def _configuration(value):
    from applypilot.config import APP_DIR
    path = Path(os.getenv('APPLYPILOT_ACTIONS_MCP_CONFIG', APP_DIR / 'professional_actions_mcp.json'))
    if path.stat().st_size > 64000:
        raise ValueError('Professional action configuration exceeds 64 KB')
    cfg = json.loads(path.read_text(encoding='utf-8'))
    if cfg.get('account_id') != value['account_id'] or value['kind'] not in cfg.get('enabled_actions', []):
        raise ValueError('Professional action is outside the configured account permission')
    command = cfg['command']
    resolved = shutil.which(command) or str(Path(command).expanduser())
    if not Path(resolved).is_file() or Path(resolved).stem.lower() in {'npx','uvx','pip','bash','sh','cmd','powershell','pwsh'}:
        raise ValueError('Configure a previously installed, trusted MCP server executable')
    args = cfg.get('args', [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError('MCP arguments must be strings')
    if any(re.search(r'(?i)(api[_-]?key|auth[_-]?token|password|cookie|secret)', arg) for arg in args):
        raise ValueError('Credentials must not be placed in process arguments')
    credential = os.getenv('LINKEDIN_BROWSER_PROFILE_DIR' if value['kind'] == 'send_connection' else 'XACTIONS_SESSION_COOKIE', '')
    if not credential or cfg.get('session_sha256') != hashlib.sha256(credential.encode()).hexdigest():
        raise ValueError('Confirm the configured account and exact provider session before enabling writes')
    return cfg, resolved, args


async def execute(conn, operation_id):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from jsonschema import Draft202012Validator
    from applypilot.referrals.store import transaction
    from .mcp import _decode
    from .worker import minimal_env
    from applypilot.execution import check_cancelled
    row = conn.execute('SELECT * FROM professional_actions WHERE operation_id=?', (operation_id,)).fetchone()
    if row is None:
        raise ValueError('Unknown professional operation')
    if row['state'] in {'attempted','uncertain','sent'}:
        return {'state': row['state'], 'provider_id': row['provider_id']}
    value = json.loads(row['input_json'])
    if row['state'] != 'approved' or row['approval_hash'] != digest(value):
        return {'state': 'needs_authorization'}
    cfg, command, args = _configuration(value)
    names = ('LINKEDIN_BROWSER_PROFILE_DIR',) if value['kind'] == 'send_connection' else ('XACTIONS_SESSION_COOKIE',)
    env = minimal_env(names)
    env['XACTIONS_MODE'] = 'local'
    tool_name = cfg.get('connection_tool') if value['kind'] == 'send_connection' else TOOLS[value['kind']]
    if not isinstance(tool_name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', tool_name):
        return {'state': 'capability_unavailable'}
    async with asyncio.timeout(120):
        async with AsyncExitStack() as stack:
            streams = await stack.enter_async_context(stdio_client(StdioServerParameters(command=command, args=args, env=env)))
            session = await stack.enter_async_context(ClientSession(*streams))
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            if tool_name not in tools:
                return {'state': 'capability_unavailable'}
            Draft202012Validator(tools[tool_name].inputSchema).validate(value['arguments'])
            check_cancelled()
            with transaction(conn):
                current = conn.execute('SELECT state,approval_hash FROM professional_actions WHERE operation_id=? FOR UPDATE', (operation_id,)).fetchone()
                if current['state'] != 'approved' or current['approval_hash'] != digest(value):
                    return {'state': current['state']}
                conn.execute("UPDATE professional_actions SET state='attempted',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?", (operation_id,))
            try:
                result = _decode(await session.call_tool(tool_name, value['arguments']))
                provider_id = next((str(result[key]) for key in ('id','url','tweetUrl','message_id') if isinstance(result, dict) and result.get(key)), None)
                state = 'sent' if provider_id or (value['kind'] == 'follow_account' and isinstance(result, dict) and result.get('success') is True) else 'uncertain'
                with transaction(conn):
                    conn.execute('UPDATE professional_actions SET state=?,provider_id=?,evidence_json=?,updated_at=CURRENT_TIMESTAMP WHERE operation_id=?',
                        (state, provider_id, json.dumps(result), operation_id))
                return {'state': state, 'provider_id': provider_id}
            except BaseException:
                with transaction(conn):
                    conn.execute("UPDATE professional_actions SET state='uncertain',updated_at=CURRENT_TIMESTAMP WHERE operation_id=?", (operation_id,))
                raise

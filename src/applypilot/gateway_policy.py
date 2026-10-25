"""Explicit gateway-only defaults for the application and its Claude subprocesses.

Docker isolation enforces the LiteLLM server's actual network boundary. This host
policy prevents missing configuration from silently selecting legacy provider
credentials; it is not an OS firewall for arbitrary third-party host plugins.
"""
from __future__ import annotations
import os
from urllib.parse import urlsplit,urlunsplit


def required() -> bool:
    value=os.getenv('APPLYPILOT_REQUIRE_LLM_PROXY','true').strip().lower()
    if value not in {'true','false','1','0','yes','no','on','off'}:
        raise ValueError('APPLYPILOT_REQUIRE_LLM_PROXY must be a boolean')
    return value in {'true','1','yes','on'}


def checked_url(value: str | None, name: str) -> str | None:
    value=(value or '').strip().rstrip('/')
    if not value:
        if required():raise RuntimeError(f'{name} is required: direct AI fallback is disabled')
        return None
    parsed=urlsplit(value)
    if parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f'{name} must be an HTTP(S) gateway URL without embedded credentials, query or fragment')
    return value


def application_gateway() -> str | None:
    return checked_url(os.getenv('LLM_URL'),'LLM_URL')


def configure_agent_environment(env: dict[str,str], *, model: str | None = None) -> None:
    """Configure the authorized subprocess without consulting native CLI login.

    When gateway-only mode is required, a conventional LiteLLM /v1 application
    URL can supply the Messages API origin. Nonstandard paths require an explicit
    agent URL instead of guessing an endpoint. Existing custom headers survive.
    """
    base=env.get('APPLYPILOT_AGENT_BASE_URL')
    if not base and required():
        parsed=urlsplit(application_gateway())
        if parsed.path not in {'','/v1'}:
            raise RuntimeError('Set APPLYPILOT_AGENT_BASE_URL explicitly for a nonstandard gateway path')
        base=urlunsplit((parsed.scheme,parsed.netloc,'','',''))
    base=checked_url(base,'APPLYPILOT_AGENT_BASE_URL')
    if base:
        env['ANTHROPIC_BASE_URL']=base
        if model:
            # Pin Claude's own fallback/background aliases inside this child;
            # native product model names need not exist on the local gateway.
            for name in ('ANTHROPIC_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL',
                         'ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL',
                         'CLAUDE_CODE_SUBAGENT_MODEL'):
                env[name] = model
        headers=[line for line in env.get('ANTHROPIC_CUSTOM_HEADERS','').splitlines()
                 if not line.lower().startswith('x-applypilot-workload:')]
        env['ANTHROPIC_CUSTOM_HEADERS']='\n'.join([*headers,'X-ApplyPilot-Workload: apply'])
        key=env.get('APPLYPILOT_AGENT_API_KEY') or (env.get('LLM_API_KEY') if required() else None)
        if not key and required():
            raise RuntimeError('An agent gateway key is required: APPLYPILOT_AGENT_API_KEY or LLM_API_KEY')
        if key:
            env['ANTHROPIC_AUTH_TOKEN']=key
            env.pop('ANTHROPIC_API_KEY',None)
    else:
        # Explicitly authorized legacy mode retains the previous CLI behavior.
        env.pop('ANTHROPIC_API_KEY',None)

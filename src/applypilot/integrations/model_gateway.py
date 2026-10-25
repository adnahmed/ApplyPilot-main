"""One JSON completion through the configured gateway; no local retry/ownership.

The gateway owns provider selection/fallback. Temporal owns whether and when a
failed semantic Activity is retried. This adapter never sleeps, reserves model
calls, mutates provider cooldowns, or logs prompts/credentials.
"""
from __future__ import annotations

import json
import os

import httpx
from jsonschema import Draft202012Validator, ValidationError


def complete_json(prompt: str, *, schema: dict, model: str = 'agent-fast',
                  max_tokens: int = 8192, transport=None):
    """Validate a single response without synthesizing missing model output."""
    from applypilot.gateway_policy import application_gateway
    from applypilot.llm import _run_async

    try:
        base = application_gateway()
    except (ValueError, RuntimeError):
        raise RuntimeError('Configure a valid shared model gateway') from None
    if not base:
        raise RuntimeError('Configure the shared model gateway')
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError('A nonempty model prompt is required')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('A gateway model alias is required')
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError('The per-response output size must be positive')
    Draft202012Validator.check_schema(schema)
    headers = {'Content-Type': 'application/json'}
    key = os.getenv('LLM_API_KEY', '')
    if key:
        headers['Authorization'] = 'Bearer ' + key
    request = {'model': model, 'temperature': 0, 'max_tokens': max_tokens,
               'messages': [{'role': 'user', 'content': prompt}],
               'response_format': {'type': 'json_object'}}
    return _run_async(_complete_json(base, headers, request, schema, transport))


async def _complete_json(base, headers, request, schema, transport):
    failure = None
    try:
        async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                                    timeout=httpx.Timeout(None, connect=30, write=30, pool=30)) as client:
            response = await client.post(base.rstrip('/') + '/chat/completions', headers=headers, json=request)
        if not 200 <= response.status_code < 300:
            from .search_providers import retry_after
            error = RuntimeError(f'Model gateway returned HTTP {response.status_code}')
            error.retry_after = retry_after(response.headers.get('retry-after'))
            raise error
        data = response.json()
        choice = data['choices'][0]
        if choice.get('finish_reason') not in {'stop', None}:
            raise ValueError('Model returned incomplete JSON output')
        text = choice['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Model returned no JSON output')
        value = json.loads(text)
        Draft202012Validator(schema).validate(value)
        return value
    except httpx.HTTPError:
        failure = RuntimeError('Model gateway connection failed')
    except (AttributeError, KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError):
        failure = ValueError('Model response did not satisfy its JSON contract')
    # No chained httpx exception containing the private request is serialized.
    raise failure

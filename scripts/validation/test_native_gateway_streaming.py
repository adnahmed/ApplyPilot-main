"""Pinned LiteLLM streaming callbacks with isolated providers and accounting."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'docs' / 'guides'))
os.environ['LITELLM_LOCAL_MODEL_COST_MAP'] = 'True'
os.environ['EGRESS_ENABLED'] = 'false'


def block_network(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.sendto'}:
        raise RuntimeError('Network disabled in streaming validation')


if __name__ == '__main__':
    sys.addaudithook(block_network)

import litellm
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.utils import ProxyLogging
from plugins import provider_quota_guard as quota
from plugins.output_contract_validator import output_contract_validator
from warp_egress.callback import WarpEgressCallback
from warp_egress.context import EgressContext, clear_egress, current_egress, set_egress

logging.getLogger('LiteLLM').setLevel(logging.CRITICAL)
logging.getLogger('LiteLLM Router').setLevel(logging.CRITICAL)


class NativeStreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name in ('callbacks', 'input_callback', 'success_callback', 'failure_callback',
                     '_async_success_callback', '_async_failure_callback'):
            self.enterContext(patch.object(litellm, name, []))
        self.settle = self.enterContext(patch.object(quota, '_durable_settle_receipt', new_callable=AsyncMock))
        self.enterContext(patch.object(quota, '_ensure_spool_replay'))
        self.enterContext(patch.object(quota, '_attempt_keys', return_value=[]))
        self.done = asyncio.Event()
        self.receipt = {'id': 'isolated-stream', 'pending': [], 'estimates': {'tokens': 10000}}
        owner = self

        class ObservedQuota(quota.ProviderQuotaCallback):
            async def async_pre_call_deployment_hook(self, kwargs, call_type):
                kwargs.setdefault('metadata', {})['_provider_admission'] = owner.receipt

            async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
                try:
                    await super().async_log_success_event(kwargs, response_obj, start_time, end_time)
                finally:
                    owner.done.set()

            async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
                try:
                    await super().async_log_failure_event(kwargs, response_obj, start_time, end_time)
                finally:
                    owner.done.set()

        class ObservedEgress(WarpEgressCallback):
            async def async_pre_call_deployment_hook(self, kwargs, call_type):
                context = EgressContext('fixture', 'fixture', 'worker', '', '192.0.2.1')
                set_egress(context)
                kwargs.setdefault('metadata', {})['_applypilot_egress_observation'] = {
                    'credential_id': context.credential_id, 'credential_slot': context.credential_slot,
                    'worker_id': context.worker_id, 'exit_ip': context.exit_ip}

            async def async_post_call_failure_deployment_hook(self, request_data, exception, call_type, fallback_depth=None):
                owner.egress_failures.append(exception)
                await super().async_post_call_failure_deployment_hook(request_data, exception, call_type, fallback_depth)

        self.egress = ObservedEgress()
        self.egress_failures = []
        self.egress.enabled = True
        self.egress.state = SimpleNamespace(mark_success=AsyncMock(), mark_failure=AsyncMock())
        litellm.callbacks = [output_contract_validator, self.egress, ObservedQuota()]

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        clear_egress()

    def router(self, response):
        return litellm.Router(model_list=[{'model_name': 'fixture', 'model_info': {'id': 'fixture'},
            'litellm_params': {'model': 'openai/gpt-4o-mini', 'api_key': 'offline-fixture',
                               'mock_response': response}}], num_retries=0, disable_cooldowns=True)

    async def test_native_stream_success_settles_and_cleans_egress_once(self):
        router = self.router('{"ok": true}')
        stream = await router.acompletion(model='fixture', messages=[{'role': 'user', 'content': 'fixture'}], stream=True)
        chunks = [chunk async for chunk in stream]
        self.assertTrue(chunks)
        await asyncio.wait_for(self.done.wait(), 5)
        self.settle.assert_awaited_once()
        self.assertEqual(self.settle.call_args.args[0], self.receipt)
        self.egress.state.mark_success.assert_awaited_once_with('worker')
        self.egress.state.mark_failure.assert_not_awaited()

    async def test_native_stream_failure_settles_once(self):
        router = self.router('Exception: mock_streaming_error')
        with self.assertRaises(Exception):
            stream = await router.acompletion(model='fixture', messages=[{'role': 'user', 'content': 'fixture'}], stream=True)
            async for chunk in stream:
                pass
        await asyncio.wait_for(self.done.wait(), 5)
        self.settle.assert_awaited_once()
        self.assertEqual(len(self.egress_failures), 1)
        self.egress.state.mark_success.assert_not_awaited()

    async def test_proxy_iterator_rejects_json_before_forwarding_content(self):
        router = self.router('invalid JSON')
        request = {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'fixture'}],
                   'stream': True, 'response_format': {'type': 'json_object'}}
        stream = await router.acompletion(**request)
        forwarded = []
        with self.assertRaises(litellm.InternalServerError):
            async for chunk in ProxyLogging(None).async_post_call_streaming_iterator_hook(
                    response=stream, user_api_key_dict=UserAPIKeyAuth(request_route='/v1/chat/completions'),
                    request_data=request):
                forwarded.append(chunk)
        self.assertEqual(forwarded, [])
        await asyncio.wait_for(self.done.wait(), 5)
        self.settle.assert_awaited_once()

    async def test_proxy_iterator_releases_valid_json_unchanged(self):
        router = self.router('{"ok": true}')
        request = {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'fixture'}],
                   'stream': True, 'response_format': {'type': 'json_object'}}
        stream = await router.acompletion(**request)
        chunks = [chunk async for chunk in ProxyLogging(None).async_post_call_streaming_iterator_hook(
            response=stream, user_api_key_dict=UserAPIKeyAuth(request_route='/v1/chat/completions'), request_data=request)]
        response = litellm.stream_chunk_builder(chunks)
        self.assertEqual(response.choices[0].message.content, '{"ok": true}')
        await asyncio.wait_for(self.done.wait(), 5)
        self.settle.assert_awaited_once()

    async def test_anthropic_sse_contract_uses_native_assembler(self):
        from litellm.proxy.guardrails.anthropic_sse import anthropic_sse_chunks_from_response
        for text in ('{"ok": true}', 'invalid JSON'):
            chunks = anthropic_sse_chunks_from_response(litellm.ModelResponse(
                model='fixture', choices=[{'index': 0, 'finish_reason': 'stop',
                                           'message': {'role': 'assistant', 'content': text}}],
                usage={'prompt_tokens': 3, 'completion_tokens': 4, 'total_tokens': 7}))
            closed = asyncio.Event()

            async def source():
                try:
                    for chunk in chunks:
                        yield chunk
                finally:
                    closed.set()

            request = {'model': 'fixture', 'response_format': {'type': 'json_object'}}
            consumer = ProxyLogging(None).async_post_call_streaming_iterator_hook(
                response=source(), user_api_key_dict=UserAPIKeyAuth(request_route='/v1/messages'), request_data=request)
            if text.startswith('{'):
                self.assertEqual([chunk async for chunk in consumer], list(chunks))
            else:
                with self.assertRaises(litellm.InternalServerError):
                    await anext(consumer)
            self.assertTrue(closed.is_set())

    async def test_responses_api_completed_event_contract_is_checked(self):
        for text in ('{"ok": true}', 'invalid JSON'):
            completed = {'status': 'completed', 'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': text}]}]}
            event = SimpleNamespace(type='response.completed', response=completed)

            async def source():
                yield event

            request = {'model': 'fixture', 'text': {'format': {'type': 'json_object'}}}
            consumer = ProxyLogging(None).async_post_call_streaming_iterator_hook(
                response=source(), user_api_key_dict=UserAPIKeyAuth(request_route='/v1/responses'), request_data=request)
            if text.startswith('{'):
                self.assertEqual([chunk async for chunk in consumer], [event])
            else:
                with self.assertRaises(litellm.InternalServerError):
                    await anext(consumer)

    async def test_proxy_iterator_rejects_malformed_tool_arguments(self):
        response = litellm.ModelResponse(model='openai/gpt-4o-mini', choices=[{
            'index': 0, 'finish_reason': 'tool_calls', 'message': {'role': 'assistant', 'content': None,
            'tool_calls': [{'id': 'call-fixture', 'type': 'function',
                            'function': {'name': 'lookup', 'arguments': '{"id": "wrong"}'}}]}}])
        router = self.router('placeholder')
        request = {'model': 'fixture', 'messages': [{'role': 'user', 'content': 'fixture'}], 'stream': True,
                   'tool_choice': 'required', 'tools': [{'type': 'function', 'function': {
                       'name': 'lookup', 'parameters': {'type': 'object', 'properties': {'id': {'type': 'integer'}},
                                                       'required': ['id']}}}]}
        original_completion = litellm.acompletion

        async def provider(**kwargs):
            kwargs['mock_response'] = response
            return await original_completion(**kwargs)

        with patch.object(litellm, 'acompletion', new=provider):
            stream = await router.acompletion(**request)
        forwarded = []
        with self.assertRaises(litellm.InternalServerError):
            async for chunk in ProxyLogging(None).async_post_call_streaming_iterator_hook(
                    response=stream, user_api_key_dict=UserAPIKeyAuth(request_route='/v1/chat/completions'),
                    request_data=request):
                forwarded.append(chunk)
        self.assertEqual(forwarded, [])
        await asyncio.wait_for(self.done.wait(), 5)
        self.settle.assert_awaited_once()

    async def test_local_quota_rejection_does_not_settle_usage_or_penalize_egress(self):
        error = litellm.RateLimitError(message='local admission busy', model='fixture', llm_provider='quota')
        error._provider_quota_guard_local = True
        kwargs = {'model': 'fixture', 'exception': error, 'metadata': {
            '_applypilot_egress_observation': {'credential_id': 'fixture', 'credential_slot': 'fixture',
                                               'worker_id': 'worker', 'exit_ip': '192.0.2.1'}}}
        with patch.object(quota._backend, 'set_quota_domain_cooldown_for_keys', new_callable=AsyncMock) as cooldown:
            await quota.provider_quota_callback.async_log_failure_event(kwargs, None, None, None)
            await self.egress.async_log_failure_event(kwargs, None, None, None)
        self.settle.assert_not_awaited()
        cooldown.assert_not_awaited()
        self.egress.state.mark_success.assert_not_awaited()
        self.egress.state.mark_failure.assert_not_awaited()
        self.assertIsNone(current_egress.get())


if __name__ == '__main__':
    unittest.main(verbosity=2)

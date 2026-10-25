import asyncio
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI

from applypilot import llm
from applypilot.execution import activity_scope
from applypilot.integrations.model_gateway import complete_json
from applypilot.reasoning import budget


SCHEMA = {'type': 'object', 'properties': {'answer': {'type': 'integer'}},
          'required': ['answer'], 'additionalProperties': False}
PROMPT = 'private prompt'


def completion(text='{"answer": 42}', *, finish_reason='stop'):
    return httpx.Response(200, json={
        'id': 'controlled-completion', 'object': 'chat.completion', 'created': 1,
        'model': 'agent-fast',
        'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text},
                     'finish_reason': finish_reason}],
    })


class GatewayClientBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('applypilot.gateway_policy.application_gateway',
                                return_value='http://gateway.invalid/v1'))
        self.enterContext(patch.dict('os.environ', {'LLM_API_KEY': 'private-key',
                                                   'LLM_USE_RESPONSES': ''}))

    @contextmanager
    def adapter(self, name, transport):
        if name == 'json':
            yield lambda: complete_json(PROMPT, schema=SCHEMA, transport=transport)
            return

        def sdk(**kwargs):
            return AsyncOpenAI(http_client=httpx.AsyncClient(transport=transport,
                               timeout=kwargs['timeout']), **kwargs)

        with patch.object(llm, 'AsyncOpenAI', side_effect=sdk):
            client = llm.LLMClient('http://gateway.invalid/v1', 'agent-fast', 'private-key')
            try:
                yield lambda: json.loads(client.ask(PROMPT))
            finally:
                client.close()

    def test_capacity_wait_crosses_former_deadline_without_retries(self):
        for adapter in ('json', 'chat'):
            with self.subTest(adapter=adapter):
                clock = [0]
                requests = []

                async def respond(request):
                    requests.append(request)
                    clock[0] = 601
                    await asyncio.sleep(0.3)
                    return completion()

                with patch.object(budget, 'time', SimpleNamespace(monotonic=lambda: clock[0])):
                    with self.adapter(adapter, httpx.MockTransport(respond)) as call:
                        self.assertEqual(call(), {'answer': 42})
                self.assertEqual(len(requests), 1)
                timeout = requests[0].extensions['timeout']
                self.assertIsNone(timeout['read'])
                for component in ('connect', 'write', 'pool'):
                    self.assertGreater(timeout[component], 0)

    def test_activity_cancellation_drains_actual_http_request(self):
        for adapter in ('json', 'chat'):
            with self.subTest(adapter=adapter):
                started, drained, stop = threading.Event(), threading.Event(), threading.Event()
                errors = []

                async def wait_for_capacity(request):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        drained.set()

                with self.adapter(adapter, httpx.MockTransport(wait_for_capacity)) as call:
                    def run():
                        try:
                            with activity_scope(key='controlled', stage='models', stop_event=stop):
                                call()
                        except BaseException as error:
                            errors.append(error)

                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    try:
                        self.assertTrue(started.wait(15), f'HTTP request never began: {errors!r}')
                    finally:
                        stop.set()
                        worker.join(3)
                    self.assertFalse(worker.is_alive(), 'Cancelled gateway worker was stranded')
                    self.assertTrue(drained.is_set(), 'The HTTP coroutine was not drained')
                    self.assertEqual(len(errors), 1)
                    self.assertIsInstance(errors[0], budget.InferenceCancelled)

    def test_json_contract_failures_do_not_synthesize_results(self):
        for response in (completion('not JSON'), completion('{"answer": "wrong"}'),
                         completion('{}'), completion(''), completion(finish_reason='length'),
                         httpx.Response(200, json={'choices': ['malformed']})):
            with self.subTest(body=response.text):
                with self.assertRaises(ValueError):
                    complete_json(PROMPT, schema=SCHEMA,
                                  transport=httpx.MockTransport(lambda request: response))

    def test_gateway_statuses_are_factual_failures_without_error_taxonomy(self):
        for status in (302, 401, 403, 429, 500, 503):
            with self.subTest(status=status):
                calls = []

                def reject(request):
                    calls.append(request)
                    return httpx.Response(status, text=PROMPT, headers={'retry-after': '60'})

                with self.assertRaisesRegex(RuntimeError, f'HTTP {status}') as failure:
                    complete_json(PROMPT, schema=SCHEMA, transport=httpx.MockTransport(reject))
                self.assertFalse(hasattr(failure.exception, 'kind'))
                self.assertEqual(failure.exception.retry_after, 60)
                self.assertNotIn(PROMPT, str(failure.exception))
                self.assertEqual(len(calls), 1)

    def test_observed_retry_delay_is_preserved_without_local_wait_or_cap(self):
        for header, delay in (('172800', 172800), ('0', 0), ('nan', None), ('invalid', None)):
            with self.subTest(header=header):
                response = httpx.Response(503, headers={'retry-after': header})
                with self.assertRaises(RuntimeError) as failure:
                    complete_json(PROMPT, schema=SCHEMA,
                                  transport=httpx.MockTransport(lambda request: response))
                self.assertEqual(failure.exception.retry_after, delay)
                self.assertIsNone(failure.exception.__context__)
                self.assertIsNone(failure.exception.__cause__)

    def test_network_error_does_not_serialize_private_request(self):
        def disconnect(request):
            raise httpx.ReadError(PROMPT + ' private-key', request=request)

        with self.assertRaisesRegex(RuntimeError, 'connection failed') as failure:
            complete_json(PROMPT, schema=SCHEMA, transport=httpx.MockTransport(disconnect))
        self.assertIsNone(failure.exception.__context__)
        self.assertIsNone(failure.exception.__cause__)
        self.assertNotIn(PROMPT, str(failure.exception))


if __name__ == '__main__':
    unittest.main()

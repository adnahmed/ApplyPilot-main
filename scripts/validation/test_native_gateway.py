"""Run with the pinned LiteLLM image and --network none; no provider traffic."""
from __future__ import annotations

import asyncio
import copy
import logging
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
GUIDES = ROOT / "docs" / "guides"
sys.path.insert(0, str(GUIDES))
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["LITELLM_CONFIG_PATH"] = str(GUIDES / "litellm.yaml")


def block_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.gethostbyname", "socket.sendto"}:
        raise RuntimeError("Network access is disabled during native gateway validation")


if __name__ == "__main__":
    sys.addaudithook(block_network)

import litellm
import yaml
from plugins.request_profile_router import (
    RequestProfile, _load_config, compatible_fallbacks, family_satisfies, request_profile_callback,
)

logging.getLogger("LiteLLM Router").setLevel(logging.CRITICAL)
logging.getLogger("LiteLLM").setLevel(logging.CRITICAL)


class NativeGatewayTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.load((GUIDES / "litellm.yaml").read_text(), Loader=yaml.CSafeLoader)
        cls.fallbacks = {source: targets for row in cls.config["router_settings"]["fallbacks"]
                         for source, targets in row.items()}
        cls.aliases = {row["model_name"]: row for row in cls.config["model_list"]
                       if row["litellm_params"]["model"] == "auto_router/complexity_router"}

    def setUp(self):
        self.original_callbacks = litellm.callbacks[:]
        litellm.callbacks = []

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        litellm.callbacks = self.original_callbacks

    def make_router(self, alias, retries=0):
        entry = copy.deepcopy(self.aliases[alias])
        entry["litellm_params"]["complexity_router_config"]["plugins"] = []
        families = list(dict.fromkeys(family for values in
            entry["litellm_params"]["complexity_router_config"]["tiers"].values() for family in values))
        models = [entry] + [{"model_name": family,
                            "litellm_params": {"model": "openai/gpt-4o-mini", "api_key": "offline-fixture"},
                            "model_info": {"id": family}}
                           for family in families]
        router = litellm.Router(
            model_list=models, num_retries=retries, retry_after=0,
            retry_policy={"InternalServerErrorRetries": retries},
            max_fallbacks=self.config["router_settings"]["max_fallbacks"],
            fallbacks=[{family: self.fallbacks[family]} for family in families],
            default_fallbacks=[], routing_strategy="simple-shuffle",
            enable_weighted_failover=True, disable_cooldowns=True,
        )
        return router, families

    async def test_every_surface_has_complete_scoped_fallbacks(self):
        for alias in self.aliases.values():
            request = await request_profile_callback.async_pre_call_hook(
                user_api_key_dict=None, cache=None,
                data={"model": alias["model_name"], "messages": [{"role": "user", "content": "fixture"}]},
                call_type="acompletion")
            scoped_fallbacks = {source: targets for row in request["fallbacks"] for source, targets in row.items()}
            tiers = alias["litellm_params"]["complexity_router_config"]["tiers"]
            for values in tiers.values():
                pool = set(values)
                for family in values:
                    targets = self.fallbacks[family]
                    self.assertEqual(len(targets), len(set(targets)))
                    self.assertNotIn(family, targets)
                    self.assertLessEqual(set(scoped_fallbacks[family]), set().union(*map(set, tiers.values())))
                # The alias can initially select any family in its own tier.
                # Families in a narrower degradation tier retain that scope.
                origins = [family for family in values if set(self.fallbacks[family]) | {family} == pool]
                self.assertTrue(origins, alias["model_name"])
                self.assertGreaterEqual(self.config["router_settings"]["max_fallbacks"], len(pool) - 1)

    def test_structured_fallback_filter_keeps_all_and_only_eligible_targets(self):
        profile = RequestProfile(requires_json_schema=True, requires_tools=True)
        quota_config = _load_config()
        filtered = compatible_fallbacks(self.config["router_settings"]["fallbacks"], quota_config, profile)
        for row in filtered:
            for family, targets in row.items():
                expected = [target for target in self.fallbacks[family]
                            if family_satisfies(quota_config, target, profile)[0]]
                self.assertEqual(targets, expected)

    async def test_native_router_reaches_last_family_in_full_pool(self):
        router, families = self.make_router("agent-local-json")
        attempted = []

        async def provider(**kwargs):
            family = kwargs["model_info"]["id"]
            attempted.append(family)
            if len(set(attempted)) < len(families):
                raise litellm.InternalServerError(message="isolated provider failure", model=family, llm_provider="openai")
            return litellm.ModelResponse(model=family, choices=[{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "healthy final family"}}])

        with patch.object(litellm, "acompletion", new=provider):
            result = await router.acompletion(model="agent-local-json", messages=[{"role": "user", "content": "fixture"}])
        self.assertEqual(result.choices[0].message.content, "healthy final family")
        self.assertEqual(set(attempted), set(families))

    async def test_native_retry_policy_recovers_before_fallback(self):
        router, families = self.make_router("agent-local-json", retries=2)
        attempted = []

        async def provider(**kwargs):
            family = kwargs["model_info"]["id"]
            attempted.append(family)
            if len(attempted) < 3:
                raise litellm.InternalServerError(message="isolated transient failure", model=family, llm_provider="openai")
            return litellm.ModelResponse(model=family, choices=[{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "recovered"}}])

        with patch.object(litellm, "acompletion", new=provider):
            result = await router.acompletion(model=families[0], messages=[{"role": "user", "content": "fixture"}])
        self.assertEqual(result.choices[0].message.content, "recovered")
        self.assertEqual(len(attempted), 3)
        self.assertEqual(len(set(attempted)), 1)

    async def test_native_failure_callback_settles_rejected_response_usage(self):
        from litellm.integrations.custom_logger import CustomLogger
        from plugins import provider_quota_guard as quota
        from plugins.output_contract_validator import output_contract_validator

        receipt = {"id": "isolated-usage-fixture", "pending": [], "estimates": {"tokens": 10000}}
        settled = asyncio.Event()
        observations = []

        class Admission(CustomLogger):
            async def async_pre_call_deployment_hook(self, kwargs, call_type):
                kwargs.setdefault("metadata", {})["_provider_admission"] = receipt

        class FailureObserver(CustomLogger):
            async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
                try:
                    await quota.provider_quota_callback.async_log_failure_event(kwargs, response_obj, start_time, end_time)
                finally:
                    observations.append(kwargs)
                    settled.set()

        response = litellm.ModelResponse(model="openai/gpt-4o-mini", choices=[{
            "index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "invalid JSON"}}],
            usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
        litellm.callbacks = [Admission(), output_contract_validator, FailureObserver()]
        with patch.object(quota, "_durable_settle_receipt", new_callable=AsyncMock) as settle:
            with self.assertRaises(litellm.InternalServerError):
                await litellm.acompletion(model="openai/gpt-4o-mini", api_key="offline-fixture",
                    messages=[{"role": "user", "content": "fixture"}],
                    response_format={"type": "json_object"}, mock_response=response, num_retries=0)
            await asyncio.wait_for(settled.wait(), timeout=5)
            settle.assert_awaited_once()
            saved_receipt, keys, amounts = settle.call_args.args
        self.assertEqual(saved_receipt, receipt)
        self.assertEqual(amounts, {"tokens": 10.0, "input_tokens": 7.0, "output_tokens": 3.0})
        self.assertEqual(len(observations), 1)

    async def test_native_retries_settle_each_attempt_receipt(self):
        from plugins import provider_quota_guard as quota
        from plugins.output_contract_validator import output_contract_validator

        failure_settled = asyncio.Event()
        receipts = []

        class ObservedQuota(quota.ProviderQuotaCallback):
            async def async_pre_call_deployment_hook(self, kwargs, call_type):
                receipt = {"id": f"isolated-attempt-{len(receipts)}", "pending": [], "estimates": {"tokens": 10000}}
                receipts.append(receipt)
                kwargs.setdefault("metadata", {})["_provider_admission"] = receipt

            async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
                try:
                    await super().async_log_failure_event(kwargs, response_obj, start_time, end_time)
                finally:
                    failure_settled.set()

        original_completion = litellm.acompletion
        router, families = self.make_router("agent-local-json", retries=1)
        litellm.callbacks = [output_contract_validator, ObservedQuota()]
        calls = 0

        async def provider(**kwargs):
            nonlocal calls
            calls += 1
            kwargs["mock_response"] = litellm.ModelResponse(model=kwargs["model"], choices=[{
                "index": 0, "finish_reason": "stop", "message": {"role": "assistant",
                "content": "invalid JSON" if calls == 1 else '{"ok": true}'}}],
                usage={"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
            return await original_completion(**kwargs)

        with patch.object(quota, "_durable_settle_receipt", new_callable=AsyncMock) as settle:
            with patch.object(litellm, "acompletion", new=provider):
                result = await asyncio.wait_for(router.acompletion(model=families[0],
                    messages=[{"role": "user", "content": "fixture"}], response_format={"type": "json_object"}), timeout=20)
            await asyncio.wait_for(failure_settled.wait(), timeout=5)
            self.assertEqual(result.choices[0].message.content, '{"ok": true}')
            self.assertEqual(len(receipts), 2)
            self.assertEqual({call.args[0]["id"] for call in settle.await_args_list},
                             {receipt["id"] for receipt in receipts})
            for call in settle.await_args_list:
                self.assertEqual(call.args[2], {"tokens": 10.0, "input_tokens": 7.0, "output_tokens": 3.0})


if __name__ == "__main__":
    unittest.main(verbosity=2)

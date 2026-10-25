"""Durable infrastructure deferrals, separate from document quality attempts."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
import re
import json
import math
import random
import hashlib
import os
from functools import lru_cache
from pathlib import Path

REASONS = frozenset({"provider_auth", "provider_quota", "provider_unavailable"})












def admission_details(error: BaseException | str | None) -> dict | None:
    """Recover the gateway envelope from typed errors or persisted error text."""
    seen=set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        direct=getattr(error,'_applypilot_admission',None)
        if isinstance(direct,dict):
            return direct
        body=getattr(error,'body',None)
        texts=[str(error)]
        while isinstance(body,dict):
            if isinstance(body.get('message'),str):
                texts.insert(0,body['message'])
            body=body.get('error')
        for text in texts:
            marker='[admission_deferred] '
            if marker not in text:
                continue
            try:
                value,_=json.JSONDecoder().raw_decode(text.split(marker,1)[1])
                if isinstance(value,dict) and isinstance(value.get('reason_code'),str):
                    return {key:value.get(key) for key in ('reason_code','reason','origin','retryable','retry_trigger',
                                                         'scope_id','next_eligible_at','evidence_id','logical_task_id')}
            except (ValueError,TypeError):
                pass
        error=getattr(error,'__cause__',None) or getattr(error,'__context__',None)
    return None


def infrastructure_reason(error: BaseException) -> str | None:
    """Classify typed SDK/transport errors and ApplyPilot's exhausted-model error.

    Do not classify arbitrary validation messages containing 'server' or a number
    as outages. Invalid/empty model output still consumes the quality-attempt cap.
    """
    admission=admission_details(error)
    if admission:
        return 'provider_auth' if admission['reason_code']=='credential_review' else 'provider_quota'
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        status = getattr(error, "status_code", None)
        if status is None:
            status = getattr(getattr(error, "response", None), "status_code", None)
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = None
        text = str(error)
        # Local pre-dispatch rejections never generated document content.
        # Recognize the exact legacy gateway prefix, not arbitrary HTTP 400s.
        if (getattr(error, "_runtime_guard_local", False) or
                (status in {400, 429, 503} and "Runtime admission blocked:" in text)):
            return "provider_unavailable"
        # The gateway intentionally maps invalid structured output to HTTP 500
        # for model fallback. That is still a quality failure, not an outage.
        if getattr(error, "llm_provider", None) == "output_contract_validator" or "OutputContractError:" in text:
            return None
        if status == 400 and "User location is not supported for the API use." in text:
            return "provider_auth"
        name = type(error).__name__.lower()
        if status in {401, 403} or name in {"authenticationerror", "permissiondeniederror"}:
            return "provider_auth"
        if status == 429 or name == "ratelimiterror":
            return "provider_quota"
        # Provider-side credit exhaustion. Not a malformed request, so replaying
        # it cannot succeed and must not spend the quality-attempt cap.
        if status == 402:
            return "provider_quota"
        if (getattr(error, "error_kind", None) == "timeout"
                or status in {408, 500, 502, 503, 504, 529}
                or isinstance(error, (TimeoutError, ConnectionError)) or name in {
                    "apitimeouterror", "apiconnectionerror", "connecterror", "connecttimeout",
                    "readtimeout", "writetimeout", "pooltimeout", "remoteprotocolerror"}):
            return "provider_unavailable"
        # 451 is a provider policy refusal (region block, terms enforcement), not a
        # property of this request. The same call keeps returning 451, so park it
        # as infrastructure instead of draining the quality-attempt cap. Every
        # other 4xx stays a quality failure on purpose: 400/404/405/413/415/422
        # are request-shape bugs that a retry cannot fix, and 407/408 are already
        # classified as transport failures below.
        if status == 451:
            return "provider_unavailable"
        if isinstance(error, RuntimeError) and text.startswith("All models exhausted after trying:"):
            match = re.search(r"Failure reason: (rate_limited|auth_failure|provider_error)\.", text)
            if match:
                return {"rate_limited": "provider_quota", "auth_failure": "provider_auth",
                        "provider_error": "provider_unavailable"}[match.group(1)]
        error = error.__cause__
    return None


def retry_after_seconds(error: BaseException | str | None) -> float | None:
    """Read an explicit provider/gateway due time; never invent a quality retry."""
    from email.utils import parsedate_to_datetime
    admission=admission_details(error)
    if admission and admission.get('next_eligible_at'):
        try:
            due=datetime.fromisoformat(admission['next_eligible_at'].replace('Z','+00:00'))
            if due.tzinfo is None:
                due=due.replace(tzinfo=timezone.utc)
            return max(0.0,(due-datetime.now(timezone.utc)).total_seconds())
        except (ValueError,TypeError,OverflowError):
            pass
    seen = set()
    for _ in range(8):
        if error is None or id(error) in seen:
            break
        seen.add(id(error))
        headers = getattr(error, "headers", None) or getattr(getattr(error, "response", None), "headers", {}) or {}
        raw = getattr(error, "retry_after", None)
        if raw is None:
            raw = headers.get("retry-after", headers.get("Retry-After"))
        if raw is not None:
            try:
                value=float(raw)
                if math.isfinite(value):
                    return max(0.0,value)
            except (ValueError, TypeError):
                try:
                    return max(0.0, (parsedate_to_datetime(str(raw)) - datetime.now(timezone.utc)).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    pass
        match = re.search(r"Runtime admission blocked:.*?hold remains for (\d+(?:\.\d+)?)s", str(error))
        if match:
            return min(31 * 86400, float(match.group(1)))
        error = getattr(error, "__cause__", None) or getattr(error, "__context__", None)
    return None

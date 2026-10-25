"""Temporal transport configuration; no credentials in policy/history/logs."""
from __future__ import annotations

import os
from temporalio.client import Client, TLSConfig
from .settings import certificate, load


async def connect(root=None, *, plugins=(), runtime=None):
    policy = load(root)
    target = os.getenv("APPLYPILOT_TEMPORAL_ADDRESS", "127.0.0.1:7233")
    tls_enabled = os.getenv("APPLYPILOT_TEMPORAL_TLS", "false").lower() in {"true", "1", "yes"}
    cert = certificate("APPLYPILOT_TEMPORAL_TLS_CERT")
    key = certificate("APPLYPILOT_TEMPORAL_TLS_KEY")
    ca = certificate("APPLYPILOT_TEMPORAL_TLS_CA")
    if bool(cert) != bool(key):
        raise ValueError("mTLS requires both client certificate and private key")
    api_key = os.getenv("APPLYPILOT_TEMPORAL_API_KEY") or None
    if (cert or key or ca or api_key) and not tls_enabled:
        raise ValueError("Enable TLS before sending Temporal credentials")
    tls = TLSConfig(client_cert=cert, client_private_key=key, server_root_ca_cert=ca,
        domain=os.getenv("APPLYPILOT_TEMPORAL_TLS_SERVER_NAME") or None) if tls_enabled else False
    args = dict(namespace=policy.namespace, tls=tls, api_key=api_key,
                plugins=list(plugins), identity=os.getenv("APPLYPILOT_TEMPORAL_IDENTITY") or None)
    if runtime is not None:
        args["runtime"] = runtime
    return await Client.connect(target, **args)

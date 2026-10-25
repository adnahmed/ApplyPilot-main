#!/usr/bin/env python3
"""Run downstream-first tests without real accounts or model calls.

Missing model SDK imports are fail-fast doubles, never synthetic API successes.
Python network access is limited to loopback/Unix sockets. Browser fixtures abort
external page requests. This is not SDK/gateway/ATS integration certification.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile
import types

ROOT = Path(__file__).resolve().parents[1]


def _loopback(value):
    if value in (None, "", "localhost", b"localhost"):
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _network_guard(event, args):
    if event == "socket.getaddrinfo" and not _loopback(args[0]):
        raise RuntimeError("Offline flow tests prohibit external DNS/network access")
    if event in {"socket.connect", "socket.connect_ex"}:
        sock, address = args
        if sock.family in {socket.AF_INET, socket.AF_INET6} and not _loopback(address[0]):
            raise RuntimeError("Offline flow tests prohibit external network access")


def main():
    os.chdir(ROOT)
    sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
    sys.addaudithook(_network_guard)
    for name, client in (("anthropic", "AsyncAnthropic"), ("openai", "AsyncOpenAI")):
        if importlib.util.find_spec(name) is None:
            module = types.ModuleType(name)
            module.__spec__ = importlib.machinery.ModuleSpec(name, loader=None)
            class NoProvider:
                def __init__(self, *args, **kwargs):
                    raise AssertionError("Provider construction forbidden: SDK unavailable in offline test run")
            setattr(module, client, NoProvider)
            sys.modules[name] = module
            print(f"IMPORT DOUBLE: {name}; client construction fails. No SDK certification.", flush=True)
    import pytest
    arguments = sys.argv[1:] or ["tests/test_downstream_flow.py", "tests/test_readme_dashboard_browser.py", "-q"]
    with tempfile.TemporaryDirectory(prefix="applypilot-flow-tests-") as directory:
        os.environ["APPLYPILOT_DIR"] = directory
        return pytest.main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Run focused offline regressions; refuse any attempted network connection.

When anthropic/openai are absent, only their imports are substituted. Instantiating a
substitute is an error, never a fake successful provider result. The callback
fixtures deliberately double SDK/Redis imports; the Lua tests model Redis
commands. Real pinned SDK/Redis validation is a separate in-image gate.
"""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types

ROOT=Path(__file__).resolve().parents[1]
os.chdir(ROOT)
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def no_network(event,args):
    if event in {'socket.connect','socket.connect_ex','socket.getaddrinfo'}:
        raise RuntimeError('Offline remediation checks prohibit network access')


def main():
    sys.addaudithook(no_network)
    for module_name, class_name in (("anthropic", "AsyncAnthropic"), ("openai", "AsyncOpenAI")):
        if importlib.util.find_spec(module_name) is None:
            sdk=types.ModuleType(module_name)
            class NoProvider:
                def __init__(self,*args,**kwargs):
                    raise RuntimeError("Provider SDK absent; no real or simulated provider call permitted")
            setattr(sdk, class_name, NoProvider)
            sys.modules[module_name]=sdk
            print(f"IMPORT DOUBLE: {module_name} unavailable; constructor raises. No SDK compatibility claim.",flush=True)
    import pytest
    default=['tests/test_remediation_20260912.py','tests/test_remediation_quota_lua.py',
        'tests/test_database.py','tests/test_queue_starvation_regressions.py','tests/test_company_cap.py',
        'tests/test_application_throughput_patch.py','tests/test_local_admission_guard.py','tests/test_feedback_loop.py','tests/test_causal_audit_blockers.py',
        'tests/test_callback_file_imports.py','tests/test_callback_keyword_contracts.py',
        'tests/test_provider_quota_bulk.py','tests/test_workers_live.py','tests/test_worker_activity_journal.py']
    with tempfile.TemporaryDirectory(prefix='applypilot-offline-') as directory:
        os.environ['APPLYPILOT_DIR']=directory
        return pytest.main(sys.argv[1:] if len(sys.argv)>1 else ["-q", *default])

if __name__=='__main__':
    raise SystemExit(main())

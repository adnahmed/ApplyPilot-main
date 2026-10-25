#!/usr/bin/env python3
"""Read-only release gate; never prints resolved configuration, credentials or URLs.

Default checks the target application interpreter/configuration and resolved
Compose configuration. --running also inspects the actual proxy networks.
--source-only validates the source declaration, not deployed settings.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

class GateFailed(RuntimeError):pass

def need(ok,message):
    if not ok:raise GateFailed(message)

def docker(args):
    result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=45,check=False)
    need(result.returncode==0,'Docker/Compose check failed; inspect locally without publishing its resolved secrets')
    return result.stdout

def check_compose(data,resolved):
    services=data['services'];proxy=services['litellm'];networks=data['networks']
    attached=proxy.get('networks',{'default':{}})
    need(attached and all(networks.get(n,{}).get('internal') is True for n in attached),
         'LiteLLM must attach only to internal networks')
    need(not proxy.get('network_mode') and not proxy.get('privileged'), 'Unsupported proxy network/privilege override')
    need('egress-singbox-controller' in proxy['depends_on'],'Missing sing-box startup dependency')
    raw=services.get('egress-raw-proxy-controller',{})
    if raw:
        need('alternative-egress' in raw.get('profiles',[]),'Unused raw proxy controller must be opt-in')
    if resolved:
        env=proxy['environment']
        truth=lambda key: str(env.get(key,'')).lower() in {'true','1','yes','on'}
        need(truth('EGRESS_ENABLED'),'EGRESS_ENABLED must be true')
        need(not truth('WARP_EGRESS_FAIL_OPEN') and not truth('PROVIDER_QUOTA_FAIL_OPEN'),'Fail-open overrides must be disabled')
        need(truth('WARP_EGRESS_STRICT_LITELLM_VERSION'),'Strict pinned transport validation must be enabled')
        need(env.get('EGRESS_ALLOWED_BACKENDS')=='singbox','Unexpected provider backend override')
        for name in ('litellm','egress-singbox-controller','egress-raw-proxy-controller'):
            if name in services:
                need(services[name]['environment'].get('EGRESS_PARALLELISM_MODE')=='bounded','An old unbounded egress override is still effective')
        cap=int(env.get('PROVIDER_GLOBAL_INFLIGHT',24));reserve=int(env.get('PROVIDER_APPLICATION_RESERVE',6))
        need(0<reserve<cap,'Provider application reserve must be positive and smaller than total capacity')
        fraction=float(env.get('PROVIDER_BACKGROUND_QUOTA_FRACTION',.8))
        need(.1<=fraction<1,'Background quota fraction must leave application/finishing headroom')
        finish=float(env.get('PROVIDER_FINISH_QUOTA_FRACTION',.9))
        need(fraction<=finish<1,'Finishing quota fraction must be above background and leave application headroom')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-only',action='store_true')
    parser.add_argument('--running',action='store_true')
    args=parser.parse_args()
    try:
        import yaml
        compose=ROOT/'docs/guides/docker-compose.litellm.yml'
        check_compose(yaml.safe_load(compose.read_text()),False)
        from applypilot.apply import readiness
        need(bool(readiness.COLUMNS),'Missing readiness projection')
        print('PASS: source declares internal provider network, startup dependency and additive readiness schema')
        if args.source_only:
            print('SOURCE ONLY: no claim about host dependencies, effective .env, Docker deployment or provider readiness')
            return 0
        need((3,11)<=sys.version_info[:2]<(3,13),'Use the application project interpreter: Python 3.11 or 3.12')
        for name in ('anthropic','openai','watchfiles','patchright'):
            need(importlib.util.find_spec(name) is not None, f'Missing application dependency: {name}')
        from applypilot import config
        config.load_env()
        from applypilot.gateway_policy import required,application_gateway,configure_agent_environment
        need(required(),'Gateway-only application mode must remain enabled')
        application_gateway();configure_agent_environment(dict(os.environ))
        print('PASS: supported interpreter, application dependencies and explicit agent/application gateway configuration')
        command=['compose','-f',str(compose)]
        resolved=json.loads(docker([*command,'config','--format','json']))
        check_compose(resolved,True)
        print('PASS: effective Compose settings are fail-closed, sing-box only and bounded')
        if args.running:
            cid=docker([*command,'ps','-q','litellm']).strip()
            need(bool(cid) and '\n' not in cid,'Expected exactly one running LiteLLM container')
            info=json.loads(docker(['inspect',cid]))[0]
            need(info['State']['Running'],'LiteLLM is not running')
            attached=info['NetworkSettings']['Networks']
            need(bool(attached),'No proxy network attachment')
            for network in attached.values():
                value=json.loads(docker(['network','inspect',network['NetworkID']]))[0]
                need(value.get('Internal') is True,'Running LiteLLM has an external network attachment')
            print('PASS: actual LiteLLM container attaches only to internal Docker networks')
        print('NEXT GATE: run the in-image remediation checker before re-enabling application workers')
        return 0
    except Exception as exc:
        print('FAIL: '+(str(exc) if isinstance(exc,GateFailed) else f'{type(exc).__name__}; inspect the local configuration/dependencies'),file=sys.stderr)
        return 1

if __name__=='__main__':raise SystemExit(main())

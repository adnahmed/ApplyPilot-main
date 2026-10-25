"""In-image smoke tests, no credentials and no remote requests.

RUN --network=none at build time. Also usable with docker compose exec source-tools
python /opt/applypilot/smoke.py. This checks installs/protocols, not account access.
"""
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def sdk_checks():
    versions = json.loads(Path('/opt/applypilot/sdk-versions.json').read_text())
    for provider in versions:
        result = subprocess.run(['/opt/tools/'+provider+'/bin/python', '/opt/applypilot/worker_main.py'],
            input=json.dumps({'provider':provider,'operation':'doctor','arguments':{}}),
            text=True,capture_output=True,timeout=50,check=True)
        lines=[line[len("APPLYPILOT_RESULT:"):] for line in result.stdout.splitlines() if line.startswith("APPLYPILOT_RESULT:")]
        if not lines: raise RuntimeError("Missing worker protocol result: "+provider)
        data=json.loads(lines[-1])
        if data.get('ok') is not True or not data.get('data',{}).get('imported'):
            raise RuntimeError('SDK worker contract failed: '+provider)
        print('PASS: '+provider+' worker protocol')
    for provider in ('playwright','linkedin_scraper'):
        code = """import asyncio
from playwright.async_api import async_playwright
async def main():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
        page=await browser.new_page()
        await page.set_content('<title>ApplyPilot runtime</title><p>Local test only</p>')
        assert await page.title()=='ApplyPilot runtime'
        await browser.close()
asyncio.run(main())
"""
        subprocess.run(['/opt/tools/'+provider+'/bin/python','-c',code],check=True,timeout=50)
        print('PASS: '+provider+' Chromium local rendering')
    subprocess.run(['/usr/local/bin/li','--help'],check=True,capture_output=True,timeout=10)
    print('PASS: linkedin-rs executable')


def gmail_protocol():
    with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryFile() as stderr:
        env={k:v for k,v in os.environ.items() if not any(s in k.upper() for s in ('TOKEN','SECRET','CREDENTIAL','CLIENT_ID','GMAIL_','MCP_CONFIG'))}
        env.update(MCP_CONFIG_DIR=folder, GMAIL_OAUTH_PATH=folder+'/absent-client.json',
                   GMAIL_CREDENTIALS_PATH=folder+'/absent-tokens.json', TELEMETRY_ENABLED='false')
        proc=subprocess.Popen(['node','/opt/gmail/entry.js'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,text=True,env=env)
        lines=queue.Queue()
        def read():
            for line in proc.stdout:
                if len(line)>2_000_000: lines.put({'error':'oversized protocol output'});break
                try: lines.put(json.loads(line))
                except ValueError: lines.put({'error':'non-JSON stdout from Gmail MCP'})
            lines.put({'error':'Gmail MCP exited'})
        reader=threading.Thread(target=read,daemon=True);reader.start()
        def send(value):
            proc.stdin.write(json.dumps({'jsonrpc':'2.0',**value})+'\n');proc.stdin.flush()
        def receive(identifier):
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                try: value=lines.get(timeout=max(.1,deadline-time.monotonic()))
                except queue.Empty: break
                if value.get('error'): raise RuntimeError('Gmail MCP initialize/list tools failed (no credentials used)')
                if value.get('id')==identifier: return value['result']
            raise RuntimeError('Gmail MCP protocol smoke test timed out')
        try:
            send({'id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'applypilot-smoke','version':'1'}}})
            receive(1)
            send({'method':'notifications/initialized'})
            send({'id':2,'method':'tools/list','params':{}})
            tools={t['name'] for t in receive(2)['tools']}
            if not {'get_profile','list_messages','get_message','send_message'}<=tools:
                raise RuntimeError('Pinned Gmail MCP tool names do not match application expectations')
            print('PASS: Gmail MCP initialization and required tool declarations (no tool called)')
        finally:
            proc.terminate()
            try: proc.wait(timeout=5)
            except subprocess.TimeoutExpired: proc.kill();proc.wait()
            proc.stdin.close();proc.stdout.close();reader.join(timeout=1)


if __name__=='__main__':
    sdk_checks()
    gmail_protocol()
    print('PASS: local source-tools installation contracts; no live accounts tested')

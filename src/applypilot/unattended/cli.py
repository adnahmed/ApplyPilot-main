"""Account settings and unattended operational controls."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import sys
from typing import Optional

import typer
from applypilot import config
from applypilot.referrals import store as s
from applypilot.tracking.feedback_cli import connection, output
from . import settings, state

app=typer.Typer(help='Set up automatic research, private outreach, replies and supervised operation once.',no_args_is_help=True)


def local_checks():
    config.load_env();results=[]
    def item(name,ok,detail): results.append({'component':name,'ok':bool(ok),'detail':detail})
    item('python',sys.version_info[:2] in {(3,11),(3,12)},'Use Python 3.11 or 3.12; running '+sys.version.split()[0])
    for binary in ('claude','npx','pdflatex'):
        found=shutil.which(binary);item(binary,bool(found),found or 'Install '+binary+' and put it on PATH')
    for name,path in [('profile',config.PROFILE_PATH),('searches',config.SEARCH_CONFIG_PATH),('resume',config.RESUME_PATH)]:
        item(name,path.is_file(),str(path))
    try:
        profile=config.load_profile();personal=profile.get('personal',{})
        s.email_address(personal.get('email'))
        if not personal.get('full_name','').strip(): raise ValueError('personal.full_name is required')
        config.load_compiled_intent(config.load_search_config(),profile=profile)
        item('profile_and_search_schema',True,'Loaded owner facts and compiled search intent')
    except Exception as exc:
        item('profile_and_search_schema',False,type(exc).__name__+': '+str(exc)[:250])
    if not os.environ.get('LLM_URL'):
        item('llm_gateway',False,'Start the documented LiteLLM gateway and set LLM_URL / LLM_API_KEY')
    else: item('llm_gateway',True,'LLM_URL is configured; live checks probe structured and general routes')
    return results


async def live_checks(cfg,profile,*,progress=lambda **kw:None,include_gateway=True):
    results=[]
    if set(cfg['channels']) & {'linkedin','x'}:
        try:
            from .browser import BrowserOS
            progress(message='Connecting to the configured BrowserOS MCP endpoint…')
            async with BrowserOS() as browser:
                from applypilot.referrals.identity import profile_key
                for channel in cfg['channels']:
                    if channel=='email': continue
                    progress(message='Verifying the signed-in '+channel+' owner through the configured browser adapter.')
                    actual=await browser.identity(channel)
                    if profile_key(actual)!=profile_key(cfg['accounts'][channel]):
                        raise ValueError('Signed-in '+channel+' profile differs from configured owner')
                    results.append({'component':channel,'ok':True,'detail':'Signed-in owner identity verified; no messages sent'})
        except Exception as exc:
            from .browser import verification_error
            code,detail=verification_error(exc)
            results.append({'component':'browseros','ok':False,'detail':detail,'error_code':code})
    if 'email' in cfg['channels']:
        try:
            from applypilot.tracking.gmail_client import open_feedback_mailbox, probe_mailbox
            progress(message='Verifying the connected Gmail owner and required tool names. No message is sent.')
            async with open_feedback_mailbox() as mailbox:
                if mailbox.address!=s.email_address(profile['personal']['email']):
                    raise ValueError('Gmail identity differs from profile.personal.email')
                if not {'get_profile','list_messages','get_message','send_message'}<=mailbox.tools:
                    raise ValueError('Use the documented Shinzo Gmail MCP with read and send_message(raw,threadId) tools')
                await probe_mailbox(mailbox, expected_address=profile['personal']['email'], require_send=True)
                results.append({'component':'gmail','ok':True,'detail':'Owner, read API and required tool names verified; send permission/delivery untested. No email sent.'})
        except Exception as exc:
            from .browser import verification_error
            code,detail=verification_error(exc)
            results.append({'component':'gmail','ok':False,'detail':detail,'error_code':code})
    if not include_gateway:
        return results
    # Inspect current gateway aliases without sending private facts or requiring
    # model inference just to install a policy. The pipeline still needs working lanes.
    try:
        import httpx
        base=os.environ['LLM_URL'].rstrip('/')
        url=base+'/models' if base.endswith('/v1') else base+'/v1/models'
        headers={'Authorization':'Bearer '+os.environ.get('LLM_API_KEY','')}
        async with httpx.AsyncClient(timeout=15,follow_redirects=False) as client:
            response=await client.get(url,headers=headers);response.raise_for_status()
            models={m['id'] for m in response.json().get('data',[]) if isinstance(m,dict) and 'id' in m}
        required={'agent-fast','agent-general','agent-reasoning','agent-code','agent-vision','agent-structured-fast','agent-structured','agent-structured-reasoning','agent-long-context','browser'}
        missing=sorted(required-models)
        if missing: raise ValueError('Missing documented workload aliases: '+', '.join(missing))
        results.append({'component':'litellm','ok':True,'detail':'Required workload aliases advertised; no generation performed'})
    except Exception as exc:
        results.append({'component':'litellm','ok':False,'detail':type(exc).__name__+': '+str(exc)[:250]})
    return results


def authorize(conn,cfg,profile):
    """Compatibility entrypoint for saving outreach settings."""
    from .policy import configure
    cfg=settings.validate(cfg)
    if not cfg['enabled'] or not cfg['channels'] or not settings.environment_enabled():
        raise ValueError('Outreach is off; check APPLYPILOT_OUTREACH_ENABLED and selected channels')
    with s.transaction(conn):
        settings.save(cfg)
        configure(conn, profile, cfg)
    return {'status':'enabled','channels':cfg['channels'],'daily_outreach_limit':cfg['daily_send_limit'],
            'daily_research_limit':cfg['daily_research_limit'],'send_grace_seconds':cfg['send_grace_seconds'],
            'application_hold_hours':cfg['referral_first_hours'],'mode':'unattended','sent':0,
            'next_command':'applypilot autopilot','policy_path':str(settings.path())}


@app.command('setup')
def setup(enable:bool=typer.Option(False,'--enable',help='Authorize future automatic research, relevant outreach and replies on the selected owner accounts.'),
          linkedin:Optional[str]=typer.Option(None,'--linkedin',help='Your LinkedIn profile; defaults to profile.personal.linkedin_url.'),
          x:Optional[str]=typer.Option(None,'--x',help='Your X profile; otherwise uses profile.personal.x_url when supplied.'),
          email:Optional[bool]=typer.Option(None,'--email/--no-email',help='Keep saved choice when omitted; fresh setup enables email.'),
          social:Optional[bool]=typer.Option(None,'--social/--no-social',help='Keep saved channels when omitted; fresh setup uses supplied profile identities.'),
          daily_limit:Optional[int]=typer.Option(None,'--daily-limit',min=-1,help='-1: no daily cap; 0: pause. Omitted preserves the saved quota; new installations are uncapped.'),
          research_limit:Optional[int]=typer.Option(None,'--research-limit',min=-1),
          referral_hours:Optional[int]=typer.Option(None,'--referral-hours',min=0,max=24,help='Optional legacy post-send APPLICATION hold. Default 0, not the outbound cancel window.'),
          send_grace_seconds:Optional[int]=typer.Option(None,'--send-grace-seconds',min=0,max=180,help='Initial automatic request cancel window. Default 180 seconds; 0 disables it.'),
          network_file:Optional[list[Path]]=typer.Option(None,'--network-file')):
    """Verify the existing installation and grant permission once. Sends nothing now."""
    if not enable: raise typer.BadParameter('Use --enable to authorize unattended outreach, or unattended check for diagnostics only')
    checks=local_checks()
    if any(not r['ok'] for r in checks): output({'status':'not_enabled','checks':checks});raise typer.Exit(1)
    profile=config.load_profile();existing=settings.path().exists();cfg=settings.load()
    cfg['channels']=list(cfg['channels']);cfg['accounts']=dict(cfg['accounts'])
    if email is not None:
        cfg['channels']=[c for c in cfg['channels'] if c!='email']
        if email: cfg['channels'].insert(0,'email')
    if social is False:
        if linkedin or x: raise typer.BadParameter('Do not combine --no-social with an owner social profile')
        cfg['channels']=[c for c in cfg['channels'] if c=='email']
    else:
        for channel,supplied,field in [('linkedin',linkedin,'linkedin_url'),('x',x,'x_url')]:
            if supplied or social is True or not existing or channel in cfg['channels']:
                url=supplied or cfg['accounts'].get(channel) or profile.get('personal',{}).get(field)
                if url:
                    if channel not in cfg['channels']: cfg['channels'].append(channel)
                    cfg['accounts'][channel]=url
    cfg['enabled']=True
    for key,value in [('daily_send_limit',daily_limit),('daily_research_limit',research_limit),
                      ('referral_first_hours',referral_hours),('send_grace_seconds',send_grace_seconds)]:
        if value is not None: cfg[key]=value
    if network_file is not None:
        cfg['network_files']=[str(p.expanduser().resolve()) for p in network_file]
    cfg=settings.validate(cfg)
    if not cfg['channels']: raise typer.BadParameter('Enable at least one email/social channel')
    try:
        live=asyncio.run(asyncio.wait_for(live_checks(cfg,profile),timeout=180))
    except TimeoutError:
        live=[{'component':'live_setup','ok':False,'detail':'Account/endpoint probe timed out; no new authorization saved'}]
    checks+=live
    if any(not r['ok'] for r in checks): output({'status':'not_enabled','checks':checks});raise typer.Exit(1)
    conn=connection(True)
    try: output(authorize(conn,cfg,profile)|{'checks':checks,'alerts': 'webhook' if os.environ.get('APPLYPILOT_ALERT_WEBHOOK') else 'local status file only; configure APPLYPILOT_ALERT_WEBHOOK for push notifications'})
    finally: conn.close()


def update_from_autopilot(*, daily_limit=None, send_grace_seconds=None):
    """Change only requested knobs on an existing authorization; no account reset."""
    conn=connection(True)
    try:
        return settings.update_limits(conn, daily_limit=daily_limit,
                                      send_grace_seconds=send_grace_seconds)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    finally:
        conn.close()


@app.command('check')
def check(live:bool=typer.Option(False,'--live',help='Also probe configured accounts, BrowserOS and advertised LiteLLM aliases. Never sends.')):
    checks=local_checks()
    if live:
        try: checks+=asyncio.run(asyncio.wait_for(live_checks(settings.load(),config.load_profile()),180))
        except Exception as exc: checks.append({'component':'live','ok':False,'detail':type(exc).__name__})
    output({'checks':checks,'ok':all(r['ok'] for r in checks)})
    if not all(r['ok'] for r in checks): raise typer.Exit(1)


@app.command('cycle')
def cycle_command(phase: str = typer.Option('all', '--phase', help='all, delivery, or prepare')):
    """Request captured networking work; never create a local dispatcher."""
    from applypilot.orchestration.client import run_work
    if phase not in {'all', 'delivery', 'prepare'}:
        raise typer.BadParameter('Choose all, delivery, or prepare')
    names = ('network_prepare', 'network_delivery') if phase == 'all' else (
        'network_prepare' if phase == 'prepare' else 'network_delivery',)
    async def submit():
        return [await run_work(name) for name in names]
    output(asyncio.run(submit()))


@app.command('serve')
def serve_command():
    """Resume installed native networking Schedules and return."""
    from .dispatcher import serve
    output(asyncio.run(serve()))


@app.command('status')
def status_command():
    conn=connection(True)
    try:
        from .maintenance import digest
        output({'enabled':settings.load()['enabled'],'policy':s.policy(conn),**digest(conn),**state.status(conn)})
    finally: conn.close()


@app.command('disable')
def disable():
    """Stop future outreach and release holds; does not undo sent applications."""
    conn=connection(True)
    try:
        with s.transaction(conn):
            cfg=settings.load();cfg['enabled']=False;settings.save(cfg)
            conn.execute('UPDATE referral_auto_grants SET revoked_at=? WHERE revoked_at IS NULL',(s.iso(),))
            conn.execute("UPDATE referral_outbox SET state='cancelled',approved_hash=NULL WHERE state IN ('draft','approved') AND approved_by LIKE 'standing:%'")
            conn.execute("UPDATE referral_cases SET hold_until=NULL")
            s.configure(conn,mode='off')
        output({'enabled':False,'note':'Autopilot applications continue. A send already in flight cannot be unsent.'})
    finally: conn.close()


@app.command('service-install')
def service_install(start:bool=typer.Option(False,'--start',help='Install and start the OS-managed autopilot service.')):
    """Install restart-on-exit service (Linux user systemd or macOS LaunchAgent)."""
    from .service import install
    output(install(start=start))

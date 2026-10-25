"""Bounded connection probes and saved outreach settings."""
from __future__ import annotations
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit
import httpx
from .storage import now, read_json, read_bytes, require_stopped, revision, setup_lock, write_json
from .environment import values, child_env, stamp

REQUIRED_ALIASES={"agent-fast","agent-general","agent-reasoning","agent-code","agent-vision",
    "agent-structured-fast","agent-structured","agent-structured-reasoning","agent-long-context","browser"}


def readiness(root):
    """Presence/schema only. Do not label a saved key or binary as connected."""
    rows=[]
    def add(key,ok,message,required=True): rows.append({"component":key,"ok":bool(ok),"detail":message,"required":required,"live":False})
    add("python",sys.version_info[:2] in {(3,11),(3,12)},"Supported: Python 3.11/3.12. Running "+sys.version.split()[0])
    for binary in ("claude","npx","pdflatex"):
        add(binary,bool(shutil.which(binary) or (os.name=="nt" and shutil.which(binary+".cmd"))),"Installed on PATH" if shutil.which(binary) else "Install "+binary+" and put it on PATH",binary!="npx")
    for module in ("mcp","pypdf","docx"):
        add(module,importlib.util.find_spec(module) is not None,"Python package present" if importlib.util.find_spec(module) else "Run uv sync in the supported Python environment",module=="mcp")
    from .planning import load_live,validate,get
    try:
        profile,searches=load_live(root)
        for key in ("personal.full_name","personal.email","personal.country"):
            add(key,bool(get(profile,key)),"Saved" if get(profile,key) else "Complete this field in Profile")
        compiled=validate(profile,searches)
        add("searches",bool(searches and compiled["active_queries"]),f"{compiled['active_queries']} active queries; {len(compiled['preferences'])} ordered preferences")
    except (ValueError,TypeError) as exc:
        add("profile/search schema",False,str(exc)[:400])
    from .workspace import resume_material
    master = resume_material(root)
    add("resume",bool(read_bytes(root/"resume.txt").strip()),
        "Master résumé text saved" if (root/"resume.txt").is_file() else
        ("Existing " + master["name"] + " detected. Apply the prepared draft to save canonical resume.txt; no upload needed." if master else "Import a master résumé"))
    env=child_env(root)
    add("llm",bool(env.get("LLM_URL") or any(env.get(k) for k in ("OPENAI_API_KEY","GEMINI_API_KEY","ANTHROPIC_API_KEY"))),"Saved model configuration; use Verify for live connectivity")
    try:
        from applypilot.config import get_chrome_path
        chrome=bool(get_chrome_path())
    except (FileNotFoundError,RuntimeError): chrome=False
    from applypilot.integrations.mcporter import browser_candidates
    add("browser",bool(env.get("BROWSEROS_MCP_URL") or browser_candidates(root) or env.get("CHROME_PATH") or chrome),"Configure BrowserOS or the existing application Chrome installation")
    journal=read_json(root/"setup"/"commit.json",{})
    add("config_transaction",journal.get("state")!="applying","Interrupted save: restore the listed snapshot before running" if journal.get("state")=="applying" else "No incomplete setup save")
    from applypilot.integrations.worker import PACKAGES,python_for
    for name in PACKAGES:
        try:
            from applypilot.integrations.container_runtime import selected, runtime
            ok=bool(runtime()) if selected(name) else bool(python_for(name))
        except Exception: ok=False
        add(name,ok,"Optional runtime present; not a live login check" if ok else "Start source-tools with the existing Compose stack, or use your local SDK",False)
    return rows


def record(root,key,rows):
    with setup_lock(root):
        path=root/"setup"/"checks.json"
        results=read_json(path,{})
        results[key]={"checked_at":now(),"checks":rows,"ok":any(not r.get("skipped") for r in rows) and all(r["ok"] or r.get("skipped") for r in rows),
                      "config_revision":stamp(root)+revision(root,("profile.json","unattended.json"))}
        if key in {'sources', 'linkedin', 'x'}:
            results[key]['probe_version'] = 2
        if key == "gmail":
            from .gmail_state import credential_fingerprint
            results[key]['credential_fingerprint'] = credential_fingerprint(root, child_env(root))
        write_json(path,results)
    return {"component":key,**results[key],"sent":0,
            "message":"Connection checks passed; no messages sent." if results[key]["ok"] else
                      "Connection checks did not pass. Review the failed checks; no permission was changed."}


def verify(root,data,progress=lambda **kw:None):
    key=data.get("component")
    if key not in {"llm","gmail","browseros","github","sources","linkedin","x"}: raise ValueError("Unknown connection check")
    rows=[]
    def add(name,ok,detail): rows.append({"component":name,"ok":bool(ok),"detail":detail,"live":True})
    if key=="llm":
        from .environment import validate_key
        base=os.environ.get("LLM_URL","").rstrip("/")
        if not base: raise ValueError("Save LLM_URL first; this check verifies the configured gateway's model catalog")
        validate_key("LLM_URL",base)
        url=base+"/models" if base.endswith("/v1") else base+"/v1/models"
        with httpx.Client(timeout=15,follow_redirects=False) as client:
            with client.stream("GET",url,headers={"Authorization":"Bearer "+os.environ.get("LLM_API_KEY","")}) as response:
                if response.status_code!=200: raise ValueError("Gateway model catalog returned HTTP "+str(response.status_code)+"; check the endpoint/key")
                raw=b""
                for chunk in response.iter_bytes():
                    raw+=chunk
                    if len(raw)>1_000_000: raise ValueError("Gateway catalog exceeds 1 MB")
        import json
        payload=json.loads(raw);models={r.get("id") for r in payload.get("data",[]) if isinstance(r,dict)}
        missing=sorted(REQUIRED_ALIASES-models)
        add("llm_gateway",not missing,"Required aliases advertised; inference not performed" if not missing else "Missing aliases: "+", ".join(missing))
    elif key=="gmail":
        async def gmail_check():
            from applypilot.tracking.gmail_client import open_feedback_mailbox, probe_mailbox
            async with open_feedback_mailbox() as mailbox:
                owner=(read_json(root/"profile.json",{}).get("personal") or {}).get("email","")
                required={"get_profile","list_messages","get_message","send_message"}
                add("gmail_identity",bool(owner) and mailbox.address.casefold()==owner.casefold(),"Connected mailbox: "+mailbox.address+("; save the matching profile email" if not owner else ""))
                add("gmail_tools",required<=mailbox.tools,"Required read/send tool names checked; send permission is not inferred from names")
                if owner and mailbox.address.casefold() == owner.casefold():
                    await probe_mailbox(mailbox, expected_address=owner, require_send=True)
                    add("gmail_read", True, "Gmail message-list API succeeded; no message bodies read or email sent. Send permission/delivery remains untested.")
        asyncio.run(asyncio.wait_for(gmail_check(),65))
        if rows and all(row["ok"] for row in rows):
            from .gmail_state import record_health
            record_health(root, child_env(root), state="connected")
    elif key=="browseros":
        accounts=data.get("accounts",{})
        if not isinstance(accounts,dict) or set(accounts)-{"linkedin","x"}: raise ValueError("Choose your LinkedIn/X account URLs")
        from applypilot.referrals.identity import profile_key
        for channel,url in accounts.items():
            if not (profile_key(url) or "").startswith(channel+":"): raise ValueError("Invalid "+channel+" owner profile")
        async def browser_check():
            from applypilot.unattended.browser import BrowserOS, BrowserUnavailable
            async with BrowserOS() as browser:
                add("browseros_tools",True,"Neo tool contract and read-only execution verified; no messages sent")
                for channel,expected in accounts.items():
                    try:
                        actual=await browser.identity(channel)
                        add(channel,profile_key(actual)==profile_key(expected),"Signed-in owner: "+actual)
                    except BrowserUnavailable as exc:
                        add(channel,False,str(exc))
                        rows[-1]["error_code"]=exc.code
        try:
            asyncio.run(asyncio.wait_for(browser_check(),140))
        except Exception as exc:
            from applypilot.unattended.browser import verification_error
            code,detail=verification_error(exc)
            add("browseros",False,detail)
            rows[-1]["error_code"]=code
    elif key=="github":
        from .github import identity
        from applypilot.evidence.github import GitHubProvider
        who=identity(data.get("identity"));provider=GitHubProvider()
        sources=provider.discover(who,limit=1)
        add("github",bool(sources),"Public repository accessible; source authorship is not inferred" if sources else provider.last_error or "No eligible public repositories found")
    elif key in {"sources", "linkedin", "x"}:
        from .source_checks import verify_sources
        rows = verify_sources(root, progress, platform=key if key != "sources" else None)

    return record(root,key,rows)


def authorize(root,data,progress=lambda **kw:None):
    """Save settings requested by an older dashboard without a second login flow."""
    require_stopped(root)
    from applypilot.unattended import settings
    from applypilot.unattended.cli import authorize as existing_authorize
    from applypilot.database import init_db
    from .planning import load_live,validate
    profile,searches=load_live(root);validate(profile,searches)
    initial_revision=revision(root,("profile.json",".env","unattended.json"))
    cfg=settings.load()
    channels=data.get("channels",cfg["channels"])
    if not isinstance(channels,list) or not channels or set(channels)-{"email","linkedin","x"}: raise ValueError("Select one or more outreach channels")
    cfg.update(enabled=True,channels=channels)
    accounts=dict(cfg["accounts"])
    for channel,key in (("linkedin","linkedin_url"),("x","x_url")):
        if channel in channels:
            url=profile.get("personal",{}).get(key) or accounts.get(channel)
            if not url: raise ValueError("Save your "+channel+" profile URL in Profile first")
            accounts[channel]=url
    cfg["accounts"]=accounts
    for key in ("daily_send_limit","daily_research_limit","send_grace_seconds"):
        if key in data: cfg[key]=data[key]
    cfg=settings.validate(cfg)
    progress(message="Saving outreach settings for your existing accounts.")
    with setup_lock(root):
        require_stopped(root)
        if revision(root,("profile.json",".env","unattended.json"))!=initial_revision: raise ValueError("Account settings changed in another window. Reload before saving.")
        conn=init_db()
        try: result=existing_authorize(conn,cfg,profile)
        finally: conn.close()
    return {"authorized":True,"saved":True,"result":result,"checks":[],"sent":0,"restart_required":True}

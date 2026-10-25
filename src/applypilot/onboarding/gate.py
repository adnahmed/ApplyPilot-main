"""First-run browser setup gate for the existing autopilot command; no second runner."""
from __future__ import annotations
import os
import secrets
import sys
import time
from .storage import now, read_json, revision, write_json, setup_lock, running


def needed(root):
    return (any(not (root/name).is_file() for name in ("profile.json","searches.yaml","resume.txt"))
            or read_json(root/"setup"/"commit.json",{}).get("state")=="applying")


def wait(root,port):
    from applypilot.web.server import existing_url,start
    # Reserve the waiting command briefly under the cross-process settings lock.
    # Configuration edits themselves remain available while it is waiting.
    with setup_lock(root):
        if running(root): raise RuntimeError("Stop the current autopilot before starting a setup run")
        prior=read_json(root/"setup"/"gate.json",{})
        if prior.get("waiting") and time.time()-float(prior.get("heartbeat",0))<8:
            raise RuntimeError("Another autopilot is already waiting in website setup")
        write_json(root/"setup"/"gate.json",{"waiting":True,"pid":os.getpid(),"heartbeat":time.time()})
    url=existing_url(root)
    viewer=None
    if not url:
        viewer=start(app_dir=root,port=port,open_browser=False)
        url=viewer.url
    token=url.split("#token=",1)[-1]
    url=url.split("#",1)[0]+"#token="+token+"&view=setup"
    print("\nComplete your one-time setup in the website. No applications or outreach run until you press Start.")
    print("Private setup link: "+url)
    # Setup never launches a browser implicitly. The owner chooses where to open the private link.
    nonce=secrets.token_urlsafe(24)
    completed=False
    try:
        while True:
            write_json(root/"setup"/"gate.json",{"waiting":True,"nonce":nonce,"pid":os.getpid(),"heartbeat":time.time()})
            launch=read_json(root/"setup"/"launch.json",{})
            if launch.get("nonce")==nonce and launch.get("revision")==revision(root):
                completed=True
                break
            time.sleep(1)
    finally:
        write_json(root/"setup"/"gate.json",{"waiting":False,"heartbeat":time.time()})
        if viewer and not completed: viewer.stop()
    # Settings were collected before the ordinary environment load and worker launch.
    print("Setup complete. Starting the existing discovery, preparation and application pipeline.")

    return viewer  # The ordinary runner owns its lifetime; browser session survives the handoff.

"""Desktop OAuth using state + PKCE and loopback-only callback; compatible token files.

Only the owner-triggered connect operation opens Google. No mailbox messages are
read/sent by this flow. Existing Gmail MCP continues using the same files.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import httpx
from .storage import atomic, digest, read_bytes, read_json, setup_lock, write_json
from .environment import catalog, save, values

AUTH_URL="https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL="https://oauth2.googleapis.com/token"
SCOPES="https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.send"


def folder(root):
    from .resolution import application_values
    from .gmail_state import locations
    return locations(root, application_values(root)[0])[2]


def upload_client(root,data):
    raw=data.get("client_json")
    if not isinstance(raw,str) or len(raw)>32000: raise ValueError("Choose a Google desktop OAuth JSON under 32 KB")
    try: parsed=json.loads(raw)
    except ValueError: raise ValueError("The Google credential file is not valid JSON") from None
    if not isinstance(parsed,dict): raise ValueError("The Google credential file must contain a Desktop app JSON object")
    app=parsed.get("installed",{})
    if not isinstance(app,dict) or not app.get("client_id") or not app.get("client_secret"):
        raise ValueError("Use OAuth credentials of type Desktop app, not a web app or service account")
    if not str(app["client_id"]).endswith(".apps.googleusercontent.com"):
        raise ValueError("Expected a Google OAuth client identifier")
    target=root/"gmail"
    target.mkdir(parents=True,exist_ok=True,mode=0o700)
    if os.name!="nt": target.chmod(0o700)
    # Don't replace a working OAuth client underneath a running authorized mailbox.
    from .storage import require_stopped
    require_stopped(root)
    if (target/"credentials.json").exists():
        existing=read_json(target/"gcp-oauth.keys.json",{})
        if (existing.get("installed") or {}).get("client_id")!=app["client_id"]:
            raise ValueError("This folder already has credentials for a different OAuth client. Use a separate local Gmail folder and reconnect explicitly.")
    # Retain only relevant desktop fields; never echo the uploaded client secret.
    clean={"installed":{"client_id":app["client_id"],"client_secret":app["client_secret"],
                        "auth_uri":AUTH_URL,"token_uri":TOKEN_URL,"redirect_uris":["http://localhost"]}}
    with setup_lock(root): write_json(target/"gcp-oauth.keys.json",clean)
    result=save(root,{"revision":catalog(root)["revision"],"values":{"MCP_CONFIG_DIR":str(target), "GMAIL_OAUTH_PATH":str(target/"gcp-oauth.keys.json"), "GMAIL_CREDENTIALS_PATH":str(target/"credentials.json")}})
    return {"saved":True,"folder":str(target),"next":"Connect Gmail to open Google's consent screen","restart_required":True}


def connect(root,data,progress=lambda **kw:None):
    from .storage import require_stopped
    require_stopped(root)
    if data.get("consent") is not True: raise ValueError("Authorize connecting your own Gmail for reading and sending")
    target=folder(root)
    from .resolution import application_values
    from .gmail_state import locations
    key_path, credential_path, target = locations(root,application_values(root)[0])
    keys=read_json(key_path,{})
    app=keys.get("installed",{}) if isinstance(keys,dict) else {}
    if not isinstance(app,dict) or not app.get("client_id") or not app.get("client_secret"):
        raise ValueError("Upload Google Desktop OAuth credentials first")
    previous = read_json(credential_path, {})
    previous = previous if isinstance(previous, dict) else {}
    previous_address = previous.get("email") or previous.get("emailAddress") or ""
    owner = (read_json(root / "profile.json", {}).get("personal") or {}).get("email", "")
    requested_address = data.get("email", "")
    if requested_address and (not isinstance(requested_address, str) or len(requested_address) > 254
                              or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", requested_address)):
        raise ValueError("Enter the Gmail address you intend to reconnect")
    expected_address = previous_address or owner or requested_address
    if requested_address and expected_address and requested_address.casefold() != str(expected_address).casefold():
        raise ValueError("Reconnect the existing Gmail identity; the entered email does not match")
    if previous.get("refresh_token") and not expected_address:
        raise ValueError("Enter the address of the SAME Gmail account before reconnecting; the old token file has no saved mailbox identity")
    credential_revision = digest(read_bytes(credential_path))
    token_state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
    challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    result={}
    class Callback(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            parsed=urlsplit(self.path)
            if parsed.path!="/oauth2callback": self.send_error(404);return
            query=parse_qs(parsed.query)
            given=query.get("state",[""])[0]
            if not hmac.compare_digest(given,token_state): self.send_error(403,"Invalid OAuth state");return
            result["error"]=query.get("error",[""])[0]
            result["code"]=query.get("code",[""])[0]
            content=b"Authorization response received. Return to ApplyPilot to see the verified connection result."
            self.send_response(200);self.send_header("Content-Type","text/plain; charset=utf-8")
            self.send_header("Cache-Control","no-store");self.send_header("Content-Length",str(len(content)))
            self.end_headers();self.wfile.write(content)
    server=HTTPServer(("127.0.0.1",0),Callback)
    server.timeout=.5
    redirect=f"http://127.0.0.1:{server.server_port}/oauth2callback"
    url=AUTH_URL+"?"+urlencode({"client_id":app["client_id"],"redirect_uri":redirect,"response_type":"code",
        "scope":SCOPES,"access_type":"offline","prompt":"consent","state":token_state,
        "code_challenge":challenge,"code_challenge_method":"S256",
        **({"login_hint": expected_address} if expected_address else {})})
    progress(message="Open the Google consent link below in your chosen browser. Nothing opens automatically and this step sends no email.",auth_url=url)
    try:
        deadline=time.monotonic()+240
        while not result and time.monotonic()<deadline: server.handle_request()
    finally: server.server_close()
    if not result or result.get("error") or not result.get("code"):
        raise ValueError("Gmail authorization was cancelled, denied or timed out. No credentials were replaced.")
    with httpx.Client(timeout=20,follow_redirects=False) as client:
        response=client.post(TOKEN_URL,data={"client_id":app["client_id"],"client_secret":app["client_secret"],
            "code":result["code"],"redirect_uri":redirect,"grant_type":"authorization_code","code_verifier":verifier})
        if response.status_code!=200: raise ValueError("Google did not accept the authorization code. Check the Desktop OAuth client and retry.")
        tokens=response.json()
        if not tokens.get("refresh_token") or not tokens.get("access_token"):
            raise ValueError("Google did not return an offline refresh token. Revoke this app's old grant in Google and reconnect.")
        granted=set(str(tokens.get("scope","")).split())
        if not set(SCOPES.split()) <= granted:
            raise ValueError("Grant both Gmail read and send access to finish this connection. Existing credentials were not replaced.")
        profile=client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",headers={"Authorization":"Bearer "+tokens["access_token"]})
        if profile.status_code!=200: raise ValueError("Google token received, but Gmail identity verification failed; credentials were not saved")
        address=str(profile.json().get("emailAddress", ""))
    owner=(read_json(root/"profile.json",{}).get("personal") or {}).get("email", "")
    if owner and address.casefold()!=str(owner).casefold():
        raise ValueError("The authorized Gmail address differs from profile.personal.email. Sign into the intended account or deliberately update the profile first.")
    if expected_address and address.casefold() != str(expected_address).casefold():
        raise ValueError("Reconnect the same saved Gmail account. A different mailbox was not saved.")
    tokens["email"] = address
    tokens["expiry_date"]=int((time.time()+int(tokens.pop("expires_in",3600)))*1000)
    commit_credentials(root, target, app, tokens, address, original_paths=(key_path,credential_path), credential_revision=credential_revision)
    from .gmail_state import record_health
    from .resolution import application_values
    record_health(root, application_values(root)[0], state="connected", address=address)
    progress(message="Google identity verified; credentials saved privately.",auth_url=None)
    return {"connected":True,"address":address,"sent":0,"scopes":["gmail.readonly","gmail.send"],"restart_required":True,
            "note":"Gmail is connected and its saved credentials will be reused for outreach. No separate outreach setup is needed."}


def commit_credentials(root, target, original_client, tokens, address, *, original_paths=None, credential_revision=None):
    """Recheck ownership and idle state after the user returns from Google."""
    from applypilot.backup.common import lock as lifecycle_lock
    from .storage import require_stopped
    with lifecycle_lock(root, "lifecycle"), setup_lock(root):
        require_stopped(root)
        from .resolution import application_values
        from .gmail_state import locations
        key_path, credentials_path, current_folder = locations(root,application_values(root)[0])
        if current_folder.resolve() != target.resolve():
            raise ValueError("Gmail folder changed during consent; credentials were not replaced")
        if original_paths and tuple(p.resolve() for p in (key_path,credentials_path)) != tuple(p.resolve() for p in original_paths):
            raise ValueError("Gmail credential paths changed during consent; credentials were not replaced")
        current = read_json(key_path, {}).get("installed", {})
        if any(current.get(k) != original_client.get(k) for k in ("client_id", "client_secret")):
            raise ValueError("OAuth client changed during consent; credentials were not replaced")
        owner = (read_json(root / "profile.json", {}).get("personal") or {}).get("email", "")
        if owner and str(owner).casefold() != address.casefold():
            raise ValueError("Profile identity changed during consent; credentials were not replaced")
        if credential_revision is not None and digest(read_bytes(credentials_path)) != credential_revision:
            raise ValueError("Gmail credentials changed during consent; restart Connect instead of replacing them")
        previous = read_json(credentials_path, {})
        previous = previous if isinstance(previous, dict) else {}
        saved_address = previous.get("email") or previous.get("emailAddress")
        if saved_address and str(saved_address).casefold() != address.casefold():
            raise ValueError("Reconnect the same saved Gmail account; no credentials were replaced")
        atomic(credentials_path, (json.dumps(tokens, indent=2) + "\n").encode())
        write_json(root / "setup" / "gmail-connection.json", {
            "method": "file", "client_path": str(key_path.resolve()),
            "credentials_path": str(credentials_path.resolve()), "email": address})

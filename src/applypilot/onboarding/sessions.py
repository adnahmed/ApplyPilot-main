"""Owner-driven session setup in canonical private paths, without pasted env vars."""
from __future__ import annotations

from contextlib import closing, contextmanager
import base64
import json
import math
import re
import os
from pathlib import Path
import tempfile
import sys
import time
from urllib.parse import urlsplit

from .storage import atomic, read_json, require_stopped, setup_lock
from applypilot import postgres
from applypilot.integrations.local_config import SESSION_KEYS, session_file

PATHS = {"twikit": "sessions/twikit/cookies.json",
         "linkedin_scraper": "sessions/linkedin/session.json", "linkedin_rs": "sessions/linkedin-rs/session.json"}


class ReaderUnavailable(ValueError):
    """Fixed reader diagnostics that never include cookie or CLI output."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def status(root, env=None):
    rows=[]
    for provider,relative in PATHS.items():
        path=session_file(provider,root,env)
        present=bool(path and path.is_file())
        ready=False;detail="No saved session. Sign in or import your own cookies."
        if present:
            try:
                obj=read_json(path,{},limit=8_000_000)
                values,_=cookie_values(obj,"linkedin" if provider.startswith("linkedin") else "x")
                ready=bool(values.get("li_at")) if provider.startswith("linkedin") else bool(values.get("auth_token") and values.get("ct0"))
                detail="Session saved · not live-verified" if ready else "Storage exists, but no usable authorized session is saved"
            except (OSError,ValueError):
                detail="Saved file is unreadable or incompatible; import your own current session"
        rows.append({"provider":provider,"present":present,"session_ready":bool(ready),
                     "path":str(path or root/relative),"verified":False,"browser_login":True,"detail":detail})
    return rows


def cookie_values(obj, platform):
    """Normalize owner-supplied mapping, cookie array, header, or storage-state.

    Cookies carrying a domain are limited to the selected platform; reject
    ambiguous duplicate values instead of guessing which signed-in identity wins.
    """
    from http.cookies import SimpleCookie
    domains={"x.com","twitter.com"} if platform=="x" else {"linkedin.com"}
    values={};records=[]
    if isinstance(obj,str):
        header=SimpleCookie()
        try: header.load(obj)
        except Exception: raise ValueError("Invalid cookie header") from None
        obj={k:v.value for k,v in header.items()}
    if isinstance(obj,dict) and "cookies" in obj: obj=obj["cookies"]
    if isinstance(obj,dict):
        obj=[{"name":k,"value":v} for k,v in obj.items() if isinstance(v,str)]
    if not isinstance(obj,list) or len(obj)>500:
        raise ValueError("Choose a cookie mapping, cookie export array, Cookie header, or Playwright session JSON")
    for row in obj:
        if not isinstance(row,dict): continue
        domain=str(row.get("domain") or "").lstrip(".").lower()
        if domain and not any(domain==d or domain.endswith("."+d) for d in domains): continue
        name,value=row.get("name"),row.get("value")
        if not isinstance(name,str) or not isinstance(value,str) or not name or not value: continue
        if not re.fullmatch(r"[!#$%&\'*+.^_`|~0-9A-Za-z-]{1,256}",name) or len(value)>16384 or any(ord(c)<32 or ord(c)==127 for c in value):
            raise ValueError("Invalid cookie name or value")
        expires=row.get("expires",row.get("expirationDate",-1))
        if isinstance(expires,(int,float)) and not math.isfinite(expires):
            raise ValueError("Cookie expiration must be finite")
        if isinstance(expires,(int,float)) and expires>0 and expires<=time.time(): continue
        if name in values and values[name]!=value:
            raise ValueError("Cookie export contains conflicting sessions; export only your intended account")
        values[name]=value
        record={"name":name,"value":value,"domain":row.get("domain") or (".x.com" if platform=="x" else ".linkedin.com"),
                "path":row.get("path") or "/","expires":expires if isinstance(expires,(int,float)) else -1,
                "httpOnly":bool(row.get("httpOnly",name in {"auth_token","li_at"})),"secure":True,
                "sameSite":row.get("sameSite") if row.get("sameSite") in {"Lax","Strict","None"} else "Lax"}
        records.append(record)
    required=("auth_token","ct0") if platform=="x" else ("li_at",)
    if not all(values.get(k) for k in required):
        raise ValueError("The selected session needs "+" and ".join(required)+"; sign in normally or export your current cookies")
    return values,records


def _save_cookies(root,provider,obj):
    from . import environment
    values,records=cookie_values(obj,"linkedin" if provider.startswith("linkedin") else "x")
    if provider=="linkedin_rs": return _linkedin_native(root,values["li_at"])
    payload=values if provider=="twikit" else {"cookies":records,"origins":[]}
    return save(root,provider,(json.dumps(payload)+"\n").encode())


def _saved_cookies(root, provider):
    from .environment import child_env
    source = session_file(provider, root, child_env(root))
    if not source or not source.is_file():
        return None
    return read_json(source, {}, limit=8_000_000)


def _save_platform(root, provider, obj, progress=lambda **kw: None):
    linkedin = provider.startswith("linkedin")
    primary, secondary = ("linkedin_scraper", "linkedin_rs") if linkedin else ("twikit", None)
    result = _save_cookies(root, primary, obj)
    saved, warnings = [primary], []
    progress(message="Session saved. Connecting compatible readers automatically…", phase="session_reuse")
    try:
        if secondary is not None:
            _save_cookies(root, secondary, obj)
            saved.append(secondary)
    except Exception as exc:
        # Canonical cookies have already been saved. Optional reader/dependency
        # failure must neither destroy a successful login nor invent success.
        warnings.append({"reader": secondary,
                         "code": exc.code if isinstance(exc,ReaderUnavailable) else type(exc).__name__,
                         "message": (str(exc)+" " if isinstance(exc,ReaderUnavailable) else "Optional reader is unavailable. ")+
                                    "Your LinkedIn session is saved and ready for reuse."})
    platform = "LinkedIn" if linkedin else "X / Twitter"
    result.update(provider=primary, requested_provider=provider, readers=saved, warnings=warnings, verified=False,
                  message=platform+" session saved for compatible readers." +
                          (" One optional reader needs attention; your login was retained." if warnings else "") +
                          " Reader access has not been live-verified.")
    return result


def reuse(root, data, progress=lambda **kw: None):
    # Compatibility endpoint for old dashboard tabs; missing peers open the
    # normal connect flow instead of sending users to another technical button.
    return connect(root, data, progress)


def save(root, provider, raw):
    from . import environment
    require_stopped(root)
    if provider not in PATHS or not isinstance(raw, bytes) or not 1 <= len(raw) <= 8_000_000:
        raise ValueError("Choose a supported session file under 8 MB")
    target = root / PATHS[provider]
    try: obj = json.loads(raw)
    except (ValueError, UnicodeError): raise ValueError("Session file must contain valid JSON") from None
    if not isinstance(obj, dict) or not obj:
        raise ValueError("This reader expects its own nonempty JSON session object")
    if provider == "twikit" and not all(isinstance(obj.get(k), str) and obj[k] for k in ("auth_token", "ct0")):
        raise ValueError("Twikit needs its cookie mapping containing auth_token and ct0")
    if provider == "linkedin_scraper" and not isinstance(obj.get("cookies"), list):
        raise ValueError("linkedin_scraper needs its Playwright session JSON containing cookies")
    if provider == "linkedin_rs" and not isinstance(obj.get("li_at"), str):
        raise ValueError("linkedin-rs needs its native session.json containing li_at")
    with setup_lock(root):
        require_stopped(root)
        # Never replace an externally managed database or the owner's live browser.
        atomic(target, raw)
    result = environment.save(root, {"revision": environment.catalog(root)["revision"],
        "values": {SESSION_KEYS[provider]: str(target)}, "clear": []})
    return {"saved": True, "provider": provider, "path": str(target), "verified": False,
            "restart_required": result["restart_required"],
            "note": "Session saved privately. Verify the reader before relying on it; no messages were sent."}


def import_file(root, data):
    require_stopped(root)
    if data.get("consent") is not True:
        raise ValueError("Confirm this is your own authorized session")
    # Accept older dashboards while the canonical UI field is file_base64.
    value=data.get("file_base64",data.get("content_base64"))
    if "file_base64" in data and "content_base64" in data and data["file_base64"]!=data["content_base64"]:
        raise ValueError("Conflicting session upload fields")
    if not isinstance(value,str) or not 1<=len(value)<=10_700_000:
        raise ValueError("Choose a session file under 8 MB")
    try: raw=base64.b64decode(value,validate=True)
    except (ValueError,UnicodeError): raise ValueError("Invalid session upload") from None
    if not 1<=len(raw)<=8_000_000: raise ValueError("Choose a session file under 8 MB")
    provider=data.get("provider")
    if provider not in PATHS: raise ValueError("Unsupported session reader")
    try:
        text=raw.decode("utf-8-sig")
        try: obj=json.loads(text)
        except json.JSONDecodeError: obj=text
    except UnicodeError: raise ValueError("Choose a UTF-8 cookie export") from None
    return _save_platform(root,provider,obj)


@contextmanager
def _login_context(root, platform, backend="cloakbrowser"):
    """CloakBrowser only; serialize our own launches without deleting browser locks."""
    from .browser_runtime import prepare, failure, BrowserSetupError
    from applypilot.backup.common import lock
    if backend != "cloakbrowser" or platform not in {"linkedin", "x"}:
        raise ValueError("Social sign-in uses CloakBrowser")
    profile = root / "sessions" / "browser-profiles" / (platform + "-cloakbrowser")
    for part in (root / "sessions", profile.parent, profile):
        if part.is_symlink():
            raise ValueError("Managed browser profiles must not be symlinks")
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile.chmod(0o700)
    with lock(root, "social-" + platform, required=False) as acquired:
        if not acquired:
            raise BrowserSetupError("cloakbrowser_profile_busy", "A sign-in task already owns this social profile. No duplicate browser was opened.", phase="launch")
        module = prepare()
        try:
            context = module.launch_persistent_context(str(profile), headless=False, timeout=90000)
        except Exception as exc:
            raise failure(exc, "launch") from None
        try:
            yield context
        finally:
            # Cleanup cannot replace a successful cookie capture or the useful
            # error that triggered it with a generic TargetClosedError.
            try:
                context.close()
            except Exception:
                pass


def connect(root, data, progress=lambda **kw: None):
    """Sign in manually in an isolated host browser; no password collection."""
    require_stopped(root)
    provider = data.get("provider")
    if provider not in PATHS:
        raise ValueError("Choose a supported reader")
    # Compatibility for already-open pre-patch tabs. Their old choice is not
    # honored: all managed social login now uses the required CloakBrowser.
    if data.get("browser_backend") not in {None, "", "chrome", "cloakbrowser"}:
        raise ValueError("Social sign-in uses CloakBrowser")
    backend = "cloakbrowser"
    if data.get("consent") is not True:
        raise ValueError("Confirm saving your own login locally for this read-only connector")
    if data.get("reconnect") is not True:
        peers = (provider, "linkedin_scraper", "linkedin_rs") if provider.startswith("linkedin") else (provider, "twikit")
        for peer in dict.fromkeys(peers):
            try:
                saved = _saved_cookies(root, peer)
                if saved:
                    cookie_values(saved, "linkedin" if provider.startswith("linkedin") else "x")
                    progress(message="Reusing your saved session; no second sign-in is needed.")
                    return _save_platform(root, provider, saved, progress)
            except (OSError, ValueError, postgres.Error):
                continue
    linkedin = provider.startswith("linkedin")
    url = "https://www.linkedin.com/login" if linkedin else "https://x.com/i/flow/login"
    cookies = None; storage = None
    progress(message="Preparing CloakBrowser. Its verified browser binary downloads automatically if not cached. "
                     "No Chrome fallback is used; your saved private profile will be reused.")
    from .browser_runtime import failure, BrowserSetupError
    phase = "launch"
    try:
        with _login_context(root,"linkedin" if linkedin else "x",backend) as context:
            progress(message="Sign in manually in CloakBrowser. Complete any Google or two-factor approval yourself; "
                             "the window closes after your X/LinkedIn session is saved.")
            page=context.pages[0] if context.pages else context.new_page()
            phase = "navigate"
            try:
                page.goto(url, wait_until="commit", timeout=60000)
            except Exception as exc:
                # A slow page is not a reason to destroy the manual-login window.
                # Keep the actual page open for the owner to reload/finish loading.
                if "timeout" not in type(exc).__name__.lower():
                    raise
                progress(message="The login page is slow. CloakBrowser remains open; reload it or finish sign-in manually.", phase="browser_login")
            phase = "login"
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                pages = [candidate for candidate in context.pages if not candidate.is_closed()]
                if not pages: break
                page = pages[0]
                all_cookies=context.cookies()
                allowed={"linkedin.com"} if linkedin else {"x.com","twitter.com"}
                selected=[c for c in all_cookies if any(c.get("domain","").lstrip(".").lower()==d or
                    c.get("domain","").lstrip(".").lower().endswith("."+d) for d in allowed)]
                values={c["name"]:c["value"] for c in selected}
                # OAuth may complete in a popup; examine only the managed profile's tabs.
                for candidate in pages:
                    location=urlsplit(candidate.url)
                    host=(location.hostname or "").removeprefix("www.")
                    ready=location.scheme=="https" and host in allowed and (
                        bool(values.get("li_at")) and location.path.startswith("/feed") if linkedin else
                        bool(values.get("auth_token") and values.get("ct0")) and location.path.startswith("/home"))
                    if ready and linkedin:
                        # A feed URL can be a temporary shell before a login redirect.
                        # Keep the window open until signed-in navigation is visible.
                        ready = candidate.locator(
                            'nav.global-nav:visible a[href*="/feed"], #global-nav:visible a[href*="/feed"], '
                            'header nav:visible a[href*="/mynetwork"], [role="navigation"]:visible a[href*="/messaging"]'
                        ).count() > 0
                    if ready: cookies=values;storage={"cookies":selected,"origins":[]};break
                if cookies: break
                page.wait_for_timeout(500)
    except (ValueError, BrowserSetupError): raise
    except Exception as exc:
        raise failure(exc, phase) from None
    if not cookies:
        raise BrowserSetupError("social_login_not_completed", "No authenticated X/LinkedIn session was captured before the window closed or the ten-minute login window ended. Existing sessions were not replaced. Google and two-factor approval remain manual.", phase="login")
    return _save_platform(root,provider,storage if linkedin else cookies,progress)


def _linkedin_native(root, cookie):
    """Let the installed Rust CLI create its OWN session schema; no guessed fields."""
    import subprocess
    import uuid
    from applypilot.integrations.local_config import linkedin_binary
    from applypilot.integrations.container_runtime import runtime, base_run, directory_mount
    from applypilot.integrations.worker import minimal_env
    with tempfile.TemporaryDirectory(prefix="applypilot-li-login-") as folder:
        home = Path(folder)
        data_dir = home / ('Library/Application Support' if sys.platform == 'darwin' else '.local/share')
        session_dir = data_dir / "linkedin"
        session_dir.mkdir(parents=True)
        legacy_dir = home / ".config" / "linkedin-cli"
        legacy_dir.mkdir(parents=True)
        env = minimal_env(); env["LINKEDIN_LI_AT"] = cookie
        # The native Windows CLI resolves the real OS known folder, regardless
        # of temporary HOME/APPDATA values. Keep managed sessions in Docker.
        binary = None if os.name == 'nt' else linkedin_binary(root)
        cleanup = None
        if binary:
            env.update(HOME=folder, USERPROFILE=folder, XDG_CONFIG_HOME=str(home / ".config"),
                       XDG_DATA_HOME=str(data_dir),
                       APPDATA=str(home / ".config"), LOCALAPPDATA=str(home / ".config"))
            argv = [binary, "auth", "login"]
        else:
            rt = runtime()
            if not rt:
                message = ("Start source-tools for the optional LinkedIn reader; Windows sessions use its isolated container."
                           if os.name == 'nt' else
                           "The optional LinkedIn reader needs source-tools or a local linkedin-rs installation.")
                raise ReaderUnavailable("reader_not_installed", message)
            name = "applypilot-li-login-" + uuid.uuid4().hex[:12]
            argv = base_run(rt, name) + directory_mount(session_dir, "/root/.local/share/linkedin")
            argv += directory_mount(legacy_dir, "/root/.config/linkedin-cli")
            argv += ["-e", "LINKEDIN_LI_AT", "--entrypoint", "timeout", rt["image"],
                     "--signal=TERM", "--kill-after=5s", "110s", "/usr/local/bin/li", "auth", "login"]
            cleanup = (rt["docker"], name)
        try:
            result = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
            targets = (session_dir/"session.json", legacy_dir/"session.json")
            target = next((path for path in targets if path.is_file()), None)
            if result.returncode:
                raise ReaderUnavailable("reader_session_failed", "The optional LinkedIn reader could not save its session. Check the linkedin-rs installation.")
            if target is None:
                raise ReaderUnavailable("reader_session_missing", "The optional LinkedIn reader did not create its session in a supported location. Check its installed version.")
            return save(root, "linkedin_rs", target.read_bytes())
        finally:
            env.pop("LINKEDIN_LI_AT", None)
            if cleanup:
                try: subprocess.run([cleanup[0], "rm", "-f", cleanup[1]], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=5, check=False)
                except (OSError, subprocess.TimeoutExpired): pass

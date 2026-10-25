"""Authorized, bounded social readers with interchangeable free/local backends."""
from __future__ import annotations

from contextlib import nullcontext
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import unquote, urlsplit

from . import mcp
from .state import SourceState
from .types import SourceError, bounded_int, stable_id
from .worker import call_worker, minimal_env, run_json
from applypilot import operations as ops

PROVIDERS = frozenset({"twikit", "linkedin_scraper", "linkedin_rs", "linkedin_mcp", "xactions", "x_use"})
BROWSER_PROVIDERS = frozenset({"linkedin_scraper", "linkedin_mcp", "xactions", "x_use"})
READ_OPERATIONS = {
    "twikit": frozenset({"posts", "profile", "timeline"}),
    "linkedin_rs": frozenset({"posts", "people", "jobs", "profile", "company", "timeline", "feed", "connections", "inbox", "conversation", "audit"}),
    "linkedin_scraper": frozenset({"profile", "company", "company_posts", "jobs"}),
    "linkedin_mcp": frozenset({"posts", "people", "jobs", "profile", "feed", "self_profile", "inbox"}),
    "xactions": frozenset({"posts", "profile", "timeline", "followers", "following"}),
    "x_use": frozenset({"posts", "timeline", "post"}),
}
PROVIDER_ORDER = {"twitter": ("twikit", "xactions", "x_use"),
                  "linkedin_posts": ("linkedin_rs", "linkedin_mcp", "linkedin_scraper")}
_ENV_PATH = {"twikit": "APPLYPILOT_TWIKIT_COOKIES",
             "linkedin_scraper": "APPLYPILOT_LINKEDIN_SESSION"}


def available_readers(platform, inputs=None, *, root=None, env=None, operation="posts"):
    """Detect configured readers, without logging in, invoking tools or granting access."""
    from .local_config import session_file
    from .mcporter import binding
    inputs = inputs or {}
    available = []
    for name in PROVIDER_ORDER.get(platform, ()):
        # This tool returns aggregate text and unaligned permalink references.
        # It remains available for research, but cannot publish identified posts.
        if name == "linkedin_mcp" and operation == "posts":
            continue
        supported = "company_posts" if name == "linkedin_scraper" and operation == "posts" else operation
        if supported not in READ_OPERATIONS[name]:
            continue
        if name in mcp.READ_TOOLS:
            configured = bool(binding(name, root))
            if not configured:
                try:
                    mcp.configuration(name)
                    configured = True
                except SourceError:
                    pass
            # x-use cannot infer which of its configured identities the owner intends.
            if name == "x_use" and not inputs.get("account"):
                configured = False
        else:
            path = session_file(name, root, env)
            configured = bool(path and path.is_file())
        # This scraper reads a specific company, not a role-keyword search.
        if name == "linkedin_scraper" and operation == "posts" and not inputs.get("company_url"):
            configured = False
        if configured:
            available.append(name)
    return available


def _session(provider, inputs):
    env_name = inputs.get("session_env", _ENV_PATH[provider])
    if not isinstance(env_name, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name):
        raise SourceError("session_env must name an environment variable, not contain credentials")
    from .local_config import session_file
    path = inputs.get("session_file") or os.environ.get(env_name)
    if not path and env_name == _ENV_PATH[provider]:
        detected = session_file(provider)
        path = str(detected) if detected else None
    if not isinstance(path, str) or not path:
        raise SourceError(f"Set {env_name} to a file containing your own authorized session", "connector_auth")
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise SourceError("Authorized session file is missing", "connector_auth")
    return str(path)


def _identifier(value):
    if not isinstance(value, str) or not value or len(value) > 1000 or value.startswith("-") or any(ord(c) < 32 for c in value):
        raise SourceError("Provide a bounded identifier/query, not command-line options", "schema")
    return value


def _linkedin_url(value, operation):
    p = urlsplit(value)
    if p.scheme != "https" or p.hostname not in {"linkedin.com", "www.linkedin.com"} or p.username or p.password:
        raise SourceError("LinkedIn scraper targets must be https://www.linkedin.com URLs", "schema")
    prefix = "/in/" if operation == "profile" else "/company/"
    if not p.path.startswith(prefix):
        raise SourceError(f"This operation requires a LinkedIn {prefix} URL", "schema")
    return value


@ops.observe('linkedin_rs', 'read')
def _li(operation, value, limit, *, start=0):
    if type(start) is not int or start < 0:
        raise SourceError("start must be a nonnegative integer")
    if operation in {'profile', 'timeline', 'company'} and '://' in value:
        target = 'company' if operation == 'company' else 'profile'
        path = urlsplit(_linkedin_url(value, target)).path
        prefix = '/company/' if target == 'company' else '/in/'
        value = _identifier(unquote(path[len(prefix):].rstrip('/')))
        if any(character in value for character in '/?#'):
            raise SourceError('LinkedIn reader requires a profile or company identifier, not a nested page URL', 'schema')
    from .local_config import linkedin_binary, session_file
    # Windows known-folder APIs ignore HOME/APPDATA overrides. Use the isolated
    # container instead of allowing a native CLI to select the owner's session.
    command = None if os.name == 'nt' else linkedin_binary()
    commands = {
        "posts": ["search", "posts", value, "--count", str(limit)],
        "people": ["search", "people", value, "--count", str(limit)],
        "jobs": ["search", "jobs", value, "--count", str(limit)],
        "profile": ["profile", "view", value, "--summary"],
        "company": ["company", "view", value],
        "timeline": ["profile", "posts", value, "--count", str(limit)],
        "feed": ["feed", "list", "--count", str(limit)],
        "connections": ["connections", "list", "--count", str(limit)],
        "inbox": ["messages", "list", "--count", str(limit)],
        "conversation": ["messages", "read", value],
        "audit": ["profile", "audit"],
    }
    if operation not in commands:
        raise SourceError("Unsupported linkedin-rs read operation", "schema")
    if start:
        if operation not in {"posts", "people", "jobs", "connections"}:
            raise SourceError("Pagination offset unsupported for this read", "schema")
        commands[operation] += ["--start", str(start)]
    # --summary already emits JSON and is mutually exclusive with --json.
    argv = commands[operation] + ([] if operation == "profile" else ["--json"])
    if not command:
        from .container_runtime import linkedin_read
        session = session_file("linkedin_rs")
        if not session or not session.is_file():
            raise SourceError("Set APPLYPILOT_LINKEDIN_RS_SESSION to your existing linkedin-rs session.json", "connector_auth")
        return linkedin_read(argv, session)
    if not Path(command).is_file():
        raise SourceError("Configured linkedin-rs executable is missing", "unavailable")
    env=minimal_env(("XDG_CONFIG_HOME",))
    detected_session = session_file("linkedin_rs")
    explicit_session = str(detected_session) if detected_session else None
    if explicit_session:
        # Current linkedin-rs uses the platform data directory; older builds
        # use .config/linkedin-cli. Supply the same selected session to both.
        import tempfile, shutil
        source=Path(explicit_session).expanduser()
        if not source.is_file(): raise SourceError("Configured linkedin-rs session is missing", "connector_auth")
        with tempfile.TemporaryDirectory(prefix="applypilot-li-") as folder:
            target=Path(folder)/".config"/"linkedin-cli"/"session.json";target.parent.mkdir(parents=True)
            shutil.copyfile(source,target);target.chmod(0o600)
            data_dir=Path(folder)/('Library/Application Support' if sys.platform=='darwin' else '.local/share')
            current=data_dir/'linkedin/session.json'
            current.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,current);current.chmod(0o600)
            env["XDG_CONFIG_HOME"]=str(Path(folder)/".config")
            env["XDG_DATA_HOME"]=str(data_dir)
            env["HOME"]=folder
            env["USERPROFILE"]=folder
            env["APPDATA"]=str(Path(folder)/".config")
            env["LOCALAPPDATA"]=str(Path(folder)/".config")
            return run_json([command,*argv],timeout=90,env=env)
    return run_json([command,*argv],timeout=90,env=env)


def _mcp_operation(provider, operation, value, limit, inputs):
    operations = {
        "linkedin_mcp": {
            "posts": ("search_posts", {"keywords": value, "max_pages": 1}),
            "people": ("search_people", {"keywords": value}),
            "jobs": ("search_jobs", {"keywords": value}),
            "profile": ("get_person_profile", {"linkedin_username": value}),
            "feed": ("get_feed", {}),
            "self_profile": ("get_my_profile", {}),
            "inbox": ("get_inbox", {}),
        },
        "xactions": {
            "posts": ("x_search_tweets", {"query": value, "limit": limit}),
            "profile": ("x_get_profile", {"username": value}),
            "timeline": ("x_get_tweets", {"username": value, "limit": limit}),
            "followers": ("x_get_followers", {"username": value, "limit": limit}),
            "following": ("x_get_following", {"username": value, "limit": limit}),
        },
        "x_use": {
            "posts": ("search_tweets", {"keywords": value, "limit": limit}),
            "timeline": ("search_profile", {"profile": value, "limit": limit}),
            "post": ("get_tweet", {"tweet_url": value}),
        },
    }
    if operation not in operations[provider]:
        raise SourceError("Use sources mcp-tools and sources mcp-read for this provider's other read tools", "schema")
    name, arguments = operations[provider][operation]
    if provider == "linkedin_mcp" and operation == "people":
        network = inputs.get("network")
        if network is not None:
            if not isinstance(network, list) or not network or len(network) > 3 or any(v not in {"F", "S", "O"} for v in network):
                raise SourceError("network must be a list of F/S/O connection degrees", "schema")
            arguments["network"] = network
        company = inputs.get("current_company")
        if company is not None:
            if not isinstance(company, str) or not re.fullmatch(r"[0-9]{1,20}", company):
                raise SourceError("current_company requires a numeric LinkedIn company ID; names are not reliable filters", "schema")
            arguments["current_company"] = company
        if inputs.get("location"):
            arguments["location"] = _identifier(inputs["location"])
    if provider == "x_use":
        account = inputs.get("account")
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", account):
            raise SourceError("x_use requires an explicit configured account identifier")
        arguments["account"] = account
    return mcp.read(provider, name, arguments, authorized=True)


class SocialService:
    def __init__(self, provider, *, inputs=None, state=None, before_request=None):
        if provider not in PROVIDERS:
            raise SourceError("Unsupported or paid social provider")
        self.provider, self.inputs = provider, dict(inputs or {})
        self.state = state or SourceState()
        self.before_request = before_request

    def read(self, operation="posts", *, query=None, identifier=None, limit=20, authorized=False, fresh=False, cursor=None, paged=False):
        if authorized is not True:
            raise SourceError("Authorize access to your own social session explicitly", "access_denied")
        bounded_int(limit, "limit", 1, 500)
        if operation not in READ_OPERATIONS[self.provider]:
            raise SourceError("Reader does not support this operation", "unsupported")
        if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 65536):
            raise SourceError("Invalid reader cursor", "schema")
        if paged and (self.provider not in {"twikit"} or operation != "posts"):
            raise SourceError("This reader does not expose this cursor operation", "schema")
        if operation not in {"feed", "connections", "inbox", "audit", "self_profile"} and not (query or identifier):
            raise SourceError("This read operation requires --query or --identifier", "schema")
        value = _identifier(query or identifier or "me")
        provider = self.provider
        session = _session(provider, self.inputs) if provider in _ENV_PATH else None
        fingerprint = [provider, self.inputs.get("account")]
        if session:
            stat = Path(session).stat()
            fingerprint += [session, stat.st_mtime_ns, stat.st_size]
        # Never reuse account-backed MCP/CLI payloads across implicit sessions.
        cache_key = "social:v3:" + stable_id([fingerprint, operation, value, limit, cursor, paged, {k: self.inputs.get(k) for k in ("start", "network", "current_company", "location")}])
        cacheable = session is not None
        cached = None if fresh or not cacheable else self.state.get(cache_key)
        if cached is not None:
            return cached
        try:
            from applypilot.execution import browser_operation
            with (browser_operation() if provider in BROWSER_PROVIDERS else nullcontext()):
                if self.before_request:
                    self.before_request()
                if provider == "linkedin_rs":
                    data = _li(operation, value, limit, start=self.inputs.get("start", 0))
                elif provider in mcp.READ_TOOLS:
                    data = _mcp_operation(provider, operation, value, min(limit, 50), self.inputs)
                else:
                    args = {"query": value, "identifier": value, "limit": limit, "cursor": cursor, "paged": paged}
                    args["session_file"] = session
                    if provider == "linkedin_scraper":
                        if operation not in {"profile", "company", "company_posts", "jobs"}:
                            raise SourceError("linkedin_scraper supports profile/company/company_posts/jobs", "schema")
                        if operation != "jobs":
                            args["url"] = _linkedin_url(value, operation)
                        args["location"] = self.inputs.get("location", "")
                    elif operation not in {"posts", "profile", "timeline"}:
                        raise SourceError("This SDK supports posts/profile/timeline", "schema")
                    data = call_worker(provider, operation, args)
            if cacheable:
                self.state.put(cache_key, data, ttl=600)
            return data
        except SourceError as exc:
            raise


    def read_page(self, platform, *, query, cursor=None, limit=50):
        """Return one finite page; never invent pagination for generic MCP tools."""
        bounded_int(limit, "page size", 1, 50)
        if self.provider == "linkedin_mcp":
            raise SourceError("LinkedIn MCP returns aggregate search text for research, not identified discovery posts", "configuration")
        if self.provider in {"twikit"}:
            data = self.read("posts", query=query, cursor=cursor, paged=True,
                             limit=min(limit, 20), authorized=True)
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise SourceError("Reader omitted its page envelope", "schema")
            next_cursor = data.get("next_cursor")
            if next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 65536):
                raise SourceError("Reader returned an invalid cursor", "schema")
            # Preserve the complete fetched page; no slicing at a provider cursor boundary.
            diagnostics = dict(data.get("diagnostics") or {})
            items = normalized_posts(platform, data["items"], limit=max(1, len(data["items"])),
                                     diagnostics=diagnostics)
            errors = list(data.get("provider_errors") or [])
            return _post_page(self.provider, items, next_cursor, True, diagnostics,
                              fetched=diagnostics.get("raw_tweet_entries", len(data["items"])), errors=errors)
        if self.provider == "linkedin_rs":
            start = int(cursor) if cursor is not None else self.inputs.get("start", 0)
            if type(start) is not int or start < 0:
                raise SourceError("Invalid LinkedIn page offset", "schema")
            self.inputs["start"] = start
            data = self.read("posts", query=query, limit=limit, authorized=True)
            from .linkedin_posts import search_page
            rows, next_cursor, supported, diagnostics = search_page(data, start=start)
            posts = normalized_posts(platform, rows, limit=max(1, len(rows)), diagnostics=diagnostics)
            errors = [] if supported else [{"provider": self.provider, "kind": "schema",
                "error": "LinkedIn returned missing or invalid pagination metadata; fetched posts were retained"}]
            return _post_page(self.provider, posts, next_cursor, supported, diagnostics,
                              fetched=len(rows), errors=errors)
        if cursor is not None:
            raise SourceError("This reader has no resumable cursor", "configuration")
        if self.provider == "linkedin_scraper":
            data = self.read("company_posts", identifier=self.inputs.get("company_url"), limit=limit, authorized=True)
        else:
            data = self.read("posts", query=query, limit=limit, authorized=True)
        diagnostics = {}
        posts = normalized_posts(platform, data, limit=limit, diagnostics=diagnostics)
        return _post_page(self.provider, posts, None, False, diagnostics,
                          fetched=diagnostics.get("raw_post_count", len(posts)))


def _post_page(provider, posts, cursor, supported, diagnostics, *, fetched, errors=None):
    errors = list(errors or [])
    malformed = diagnostics.get("malformed_posts", 0)
    if malformed:
        errors.append({"provider": provider, "kind": "schema",
                       "error": f"{malformed} social posts lack usable identity/text; valid posts were retained"})
    payload = {"posts": posts, "next_cursor": cursor, "continuation_supported": supported,
               "acquisition_success": True, "fetched_posts": fetched,
               "diagnostics": diagnostics, "provider_errors": errors}
    if not posts and (malformed or diagnostics.get("unparsed_tweet_entries", 0)):
        payload.update(stop_reason="schema", acquisition_success=False)
    return payload


def normalized_posts(platform, data, *, limit=50, diagnostics=None):
    """Return genuine per-post records only, never invent author/ID association.

    Raw aggregate pages from some MCP tools are useful for research, but are not
    individual posts. Preserve via `sources social`; discovery defers them.
    """
    from applypilot.discovery.signal_cache import normalize_post
    if type(limit) is not int or limit < 1:
        raise SourceError("Normalization limit must be positive", "schema")
    if isinstance(data, dict):
        if data.get("ok") is False or data.get("success") is False or data.get("error"):
            raise SourceError("Social connector reported an upstream failure", "network")
        for key in ("posts", "tweets", "results", "elements", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
            if isinstance(data.get(key), dict):
                return normalized_posts(platform, data[key], limit=limit)
        else:
            if any(k in data for k in ("tweet_id", "id", "urn", "entityUrn", "post_url")):
                data = [data]
            else:
                raise SourceError("Reader returned an aggregate page without per-post identities; save it for research, or supply reviewed post exports", "schema")
    if not isinstance(data, list):
        raise SourceError("Social results are not a post list", "schema")
    posts, errors = [], 0
    if diagnostics is not None:
        diagnostics.setdefault("raw_post_count", len(data))
    for item in data[:limit]:
        if not isinstance(item, dict):
            errors += 1
            continue
        row = dict(item)
        row["text"] = row.get("full_text") or row.get("text_content") or row.get("rawContent") or row.get("text") or row.get("content") or row.get("commentary")
        if isinstance(row["text"], dict):
            row["text"] = row["text"].get("text")
        row["url"] = row.get("url") or row.get("tweet_url") or row.get("post_url") or row.get("permalink") or row.get("postUrl")
        row["id"] = row.get("id") or row.get("tweet_id") or row.get("urn") or row.get("entityUrn")
        if row["id"] is not None:
            row["id"] = str(row["id"])
        if platform == "twitter" and row["id"] and row["id"].isdigit() and not row["url"]:
            row["url"] = "https://x.com/i/web/status/" + row["id"]
        try:
            post = normalize_post(platform, row)
            if not post["url"] or not post["text"]:
                raise ValueError
            author = row.get("author") or row.get("user")
            if isinstance(author, dict):
                handle = author.get("username") or author.get("screen_name")
                name = author.get("name") or author.get("displayname") or author.get("display_name")
                if platform == "twitter" and isinstance(handle, str) and re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) and isinstance(name, str):
                    post["author"] = {"name": name[:250], "username": handle,
                                      "profile_url": "https://x.com/" + handle}
                elif platform == "linkedin_posts" and isinstance(name, str):
                    post["author"] = {"name": name[:250]}
                    profile = author.get("profile_url")
                    if isinstance(profile, str):
                        parts = urlsplit(profile)
                        if (parts.scheme == "https" and parts.hostname in {"linkedin.com", "www.linkedin.com"}
                                and not parts.username and not parts.password and parts.path.startswith("/in/")):
                            post["author"]["profile_url"] = profile
            posts.append(post)
        except (ValueError, TypeError):
            errors += 1
    if diagnostics is not None:
        diagnostics.update(malformed_posts=errors, normalized_posts=len(posts))
    elif errors:
        raise SourceError(f"{errors} social rows lack usable identity/text; review raw export instead of silently dropping them", "schema")
    return list({p["id"]: p for p in posts}.values())

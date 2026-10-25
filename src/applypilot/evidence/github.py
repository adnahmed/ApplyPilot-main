"""Revision-pinned GitHub evidence with explicit, owner-scoped private access."""

from __future__ import annotations

import base64
import json
import gzip
from pathlib import Path
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from applypilot.evidence.base import CandidateSource, EvidenceProvider, RawSignals

log = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ApplyPilot-Evidence/1.0",
}


class GitHubProvider(EvidenceProvider):
    name = "github"

    def __init__(self, cache_root: Path | None = None, *, cancel_event=None, budget=None):
        from applypilot.config import APP_DIR
        self.cache_root = Path(cache_root or APP_DIR) / "evidence" / "repomix"
        # Headers are instance-local: removing/changing a token must not leave a
        # previous owner's token in the next provider instance.
        self.headers = dict(_HEADERS)
        from applypilot.integrations.github_auth import evidence_token
        token = evidence_token()
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.rate_limited = False
        self.last_error = ""
        self.budget = None
        self.cancel_event = cancel_event

    def _check_cancelled(self):
        if self.cancel_event is not None and self.cancel_event.is_set():
            from applypilot.execution import ExecutionStopped
            raise ExecutionStopped("Evidence collection stopped")

    def _run_pack(self, command, *, cwd, env):
        """Drain the owned packer tree before releasing an interrupted task."""
        import psutil
        self._check_cancelled()
        with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                stdout=output, stderr=errors, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            owner = None
            try:
                try:
                    owner = psutil.Process(process.pid)
                except psutil.NoSuchProcess:
                    process.wait(timeout=5)
                deadline = time.monotonic() + 240
                while process.poll() is None:
                    self._check_cancelled()
                    if time.monotonic() >= deadline:
                        raise subprocess.TimeoutExpired(command, 240)
                    if self.cancel_event is None:
                        time.sleep(.2)
                    else:
                        self.cancel_event.wait(.2)
                self._check_cancelled()
            except BaseException:
                try:
                    owned = owner.children(recursive=True) + [owner] if owner is not None else []
                except psutil.NoSuchProcess:
                    owned = []
                for child in reversed(owned):
                    try:
                        child.terminate()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                _, remaining = psutil.wait_procs(owned, timeout=5)
                for child in remaining:
                    try:
                        child.kill()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                _, remaining = psutil.wait_procs(remaining, timeout=5)
                process.wait(timeout=5)
                if remaining:
                    raise RuntimeError("Repomix cleanup could not stop all owned processes")
                raise
            output.seek(0)
            errors.seek(0)
            return subprocess.CompletedProcess(command, process.returncode,
                output.read().decode('utf-8', errors='replace'), errors.read().decode('utf-8', errors='replace'))

    def discover(self, identity: str, limit: int | None = None, *, include_private: bool = False) -> list[CandidateSource]:
        """Discover repos for a user or single repo."""
        self.rate_limited = False
        self.last_error = ""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:/[A-Za-z0-9_.-]{1,100})?", identity):
            raise ValueError("Use a GitHub username or owner/repository identifier")
        if limit is not None and (type(limit) is not int or not 1 <= limit):
            raise ValueError("GitHub limit must be at least 1.")
        if type(include_private) is not bool:
            raise ValueError("Private repository access must be explicitly selected.")
        if include_private and "Authorization" not in self.headers:
            raise ValueError("Connect the intended GitHub account in Setup before including private repositories.")
        if "/" in identity:
            return self._discover_repo(identity, include_private=include_private)
        return self._discover_user(identity, limit, include_private=include_private)

    def _discover_user(self, username: str, limit: int | None, *, include_private: bool = False) -> list[CandidateSource]:
        # Rank after pagination.  Asking GitHub for only ``limit`` repositories
        # made a recently touched low-signal repo hide stronger older work.  A
        # ``None`` limit walks every page, which is the CLI default.
        endpoint = f"/users/{username}/repos?sort=updated"
        if include_private:
            account = self._fetch(f"{_GITHUB_API}/user")
            if not isinstance(account, dict) or str(account.get("login") or "").casefold() != username.casefold():
                raise ValueError("Private profile discovery requires the connected account's own username. "
                                 "For an authorized organization repository, supply its exact owner/repository.")
            # /users/{login}/repos only lists public repositories, even with a
            # token. Do not enumerate unrelated collaborator/organization work.
            endpoint = "/user/repos?visibility=all&affiliation=owner&sort=updated"
        repos: list[dict] = []
        per_page = 100
        page = 0
        while limit is None or len(repos) < limit:
            page += 1
            if limit is not None and page > (limit // per_page) + 2:
                break
            url = f"{_GITHUB_API}{endpoint}&per_page={per_page}&page={page}"
            data = self._fetch(url)
            if not isinstance(data, list):
                break
            repos.extend(repo for repo in data if isinstance(repo, dict))
            if len(data) < per_page:
                break

        sources = []
        for repo in repos:
            if repo.get("archived"):
                continue
            owner = str((repo.get("owner") or {}).get("login") or "")
            if include_private and owner.casefold() != username.casefold():
                continue
            if repo.get("private") and not include_private:
                continue
            sources.append(CandidateSource(
                provider="github",
                ref=f"{username}/{repo['name']}",
                title=repo.get("name", ""),
                url=repo.get("html_url", ""),
                metadata={
                    "private": repo.get("private") is True,
                    "private_access_authorized": include_private,
                    "visibility": "private" if repo.get("private") else "public",
                    "stars": repo.get("stargazers_count") or 0,
                    "last_push_date": repo.get("pushed_at") or "",
                    "description": repo.get("description") or "",
                    "language": repo.get("language") or "",
                    "topics": repo.get("topics") or [],
                    "default_branch": repo.get("default_branch") or "",
                    "fork": bool(repo.get("fork")),
                    "owner": (repo.get("owner") or {}).get("login") or username,
                }
            ))
        # Sort by recency then stars
        sources.sort(key=lambda s: s.sort_key, reverse=True)
        return sources if limit is None else sources[:limit]

    def _discover_repo(self, ref: str, *, include_private: bool = False) -> list[CandidateSource]:
        url = f"{_GITHUB_API}/repos/{ref}"
        data = self._fetch(url)
        if not isinstance(data, dict):
            return []
        if data.get("archived"):
            return []
        if data.get("private") and not include_private:
            self.last_error = "Private repository detected. Select Include private GitHub repositories before importing its contents."
            return []
        return [CandidateSource(
            provider="github",
            ref=ref,
            title=data.get("name", ""),
            url=data.get("html_url", ""),
            metadata={
                "private": data.get("private") is True,
                "private_access_authorized": include_private,
                "visibility": "private" if data.get("private") else "public",
                "stars": data.get("stargazers_count") or 0,
                "last_push_date": data.get("pushed_at") or "",
                "description": data.get("description") or "",
                "language": data.get("language") or "",
                "topics": data.get("topics") or [],
                "default_branch": data.get("default_branch") or "",
                "fork": bool(data.get("fork")),
                "owner": (data.get("owner") or {}).get("login") or ref.split("/", 1)[0],
                "parent": (data.get("parent") or {}).get("full_name") or "",
            }
        )]

    def fetch(self, source: CandidateSource) -> RawSignals:
        """Pack every nonempty repository at one immutable revision before extraction."""
        # Already have basic metadata from discover
        metadata = dict(source.metadata or {})
        repo_data = self._fetch(f"{_GITHUB_API}/repos/{source.ref}")
        if not isinstance(repo_data, dict):
            raise ValueError(self.last_error or "GitHub repository metadata could not be read safely.")
        if repo_data.get("private") and metadata.get("private_access_authorized") is not True:
            raise ValueError("Repository is now private. Re-collect it with explicit private access consent.")
        metadata.update({
            "private": repo_data.get("private") is True,
            "visibility": "private" if repo_data.get("private") else "public",
            "default_branch": repo_data.get("default_branch") or metadata.get("default_branch"),
            "fork": bool(repo_data.get("fork")),
            "owner": (repo_data.get("owner") or {}).get("login") or metadata.get("owner"),
            "parent": (repo_data.get("parent") or {}).get("full_name") or metadata.get("parent", ""),
        })
        languages = [metadata.get("language")] if metadata.get("language") else []
        topics = metadata.get("topics", [])
        observations: list[dict] = []
        branch = metadata.get("default_branch") or "HEAD"
        commit = self._fetch(f"{_GITHUB_API}/repos/{source.ref}/commits/{urllib.parse.quote(str(branch), safe='')}", allow_empty=True)
        revision = str((commit or {}).get("sha") or "") if isinstance(commit, dict) else ""
        if not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision):
            empty = isinstance(commit, dict) and commit.get("empty_repository") is True
            return RawSignals(source.title, metadata.get("description") or "", languages, topics,
                source.url, metadata.get("last_push_date", ""), extra={**metadata,
                "source_revision": "", "observations": [], "pack_status": "empty" if empty else "incomplete",
                "pack_error": "" if empty else "Unable to resolve the repository commit; retry discovery.",
                "pack_attempts": 0})
        pack = self._repomix_pack(source.ref, revision)
        observations = pack.pop("observations", [])
        readme = next((row["excerpt"] for row in observations
                       if row["path"].rsplit("/", 1)[-1].casefold().startswith("readme")), "")
        if not readme and pack["pack_status"] == "packed":
            log.info("%s has no packed README; extracting claims from its source files", source.ref)
        if pack["pack_status"] != "packed":
            return RawSignals(source.title, metadata.get("description") or "", languages, topics,
                source.url, metadata.get("last_push_date", ""), extra={**metadata, **pack,
                "source_revision": revision, "observations": []})
        langs_data = self._fetch(f"{_GITHUB_API}/repos/{source.ref}/languages")
        if isinstance(langs_data, dict):
            languages = list(langs_data)

        # A candidate-owned fork can be retained when GitHub exposes commits
        # attributed to that candidate.  This does not imply ownership of the
        # upstream project.
        if metadata.get("fork") and metadata.get("parent") and revision:
            owner = str(metadata.get("owner") or source.ref.split("/", 1)[0])
            parent = str(metadata["parent"])
            parent_data = self._fetch(f"{_GITHUB_API}/repos/{parent}")
            base_branch = (parent_data or {}).get("default_branch") if isinstance(parent_data, dict) else None
            if base_branch:
                compare = self._fetch(
                    f"{_GITHUB_API}/repos/{parent}/compare/"
                    f"{urllib.parse.quote(str(base_branch), safe='')}..."
                    f"{urllib.parse.quote(owner, safe='')}:{urllib.parse.quote(str(branch), safe='')}"
                )
                commits = compare.get("commits") if isinstance(compare, dict) else []
                authored = [c for c in commits or [] if isinstance(c, dict)
                            and str((c.get("author") or {}).get("login") or "").casefold() == owner.casefold()]
                if authored:
                    observations.append({
                        "source_revision": revision,
                        "path": "",
                        "excerpt": "; ".join(str((c.get("commit") or {}).get("message") or "").splitlines()[0]
                                             for c in authored[:5])[:1600],
                        "source_kind": "candidate_contribution",
                    })

        # GitHub's own immutable-revision records can support execution or
        # deployment statements. Configuration files alone never do.
        if revision:
            runs = self._fetch(
                f"{_GITHUB_API}/repos/{source.ref}/actions/runs?"
                + urllib.parse.urlencode({"head_sha": revision, "status": "success", "per_page": 3})
            )
            for run in ((runs or {}).get("workflow_runs") or [])[:3] if isinstance(runs, dict) else []:
                if not isinstance(run, dict) or run.get("conclusion") != "success":
                    continue
                observations.append({
                    "source_revision": revision,
                    "path": run.get("path") or "",
                    "excerpt": json.dumps({
                        "name": run.get("name"), "event": run.get("event"),
                        "conclusion": run.get("conclusion"), "run_started_at": run.get("run_started_at"),
                        "environment": "GitHub Actions (runner details unavailable)",
                        "html_url": run.get("html_url"),
                    }, ensure_ascii=False)[:1600],
                    "source_kind": "successful_run",
                })
            deployments = self._fetch(
                f"{_GITHUB_API}/repos/{source.ref}/deployments?"
                + urllib.parse.urlencode({"sha": revision, "per_page": 3})
            )
            for deployment in deployments[:3] if isinstance(deployments, list) else []:
                if not isinstance(deployment, dict) or not deployment.get("statuses_url"):
                    continue
                statuses = self._fetch(str(deployment["statuses_url"]) + "?per_page=5")
                success = next((row for row in statuses or []
                                if isinstance(row, dict) and row.get("state") == "success"), None)
                if success:
                    observations.append({
                        "source_revision": revision,
                        "path": "",
                        "excerpt": json.dumps({
                            "environment": deployment.get("environment"),
                            "description": deployment.get("description"),
                            "state": success.get("state"),
                            "environment_url": success.get("environment_url"),
                            "updated_at": success.get("updated_at"),
                        }, ensure_ascii=False)[:1600],
                        "source_kind": "deployment_record",
                    })

        description = metadata.get("description", "") or ""
        full_text = f"{description}\n\n{readme}".strip()
        full_text = full_text[:8000]

        return RawSignals(
            title=source.title,
            description=full_text,
            languages=languages,
            topics=topics,
            url=source.url,
            last_activity=metadata.get("last_push_date", ""),
            extra={
                **pack,
                "stars": metadata.get("stars", 0),
                "rate_limited": self.rate_limited,
                "private": metadata.get("private") is True,
                "visibility": metadata.get("visibility", "public"),
                "source_revision": revision,
                "observations": observations,
                "fork": bool(metadata.get("fork")),
            },
        )

    @staticmethod
    def _source_kind(path: str) -> str:
        low = path.casefold()
        name = low.rsplit("/", 1)[-1]
        if name.startswith("readme") or name.endswith((".md", ".rst", ".txt")):
            return "documentation"
        if re.search(r"(^|[._-])(test|tests|spec)([._-]|$)", name) or "/tests/" in f"/{low}/":
            return "test_code"
        if name in {"dockerfile", "compose.yml", "compose.yaml", "jest.config.js", "jest.config.ts",
                    "pyproject.toml", "package.json", "terraform.tf"} or name.endswith((".yml", ".yaml", ".json", ".toml", ".tf")):
            return "configuration"
        return "implementation"

    @classmethod
    def _path_score(cls, path: str) -> tuple[int, int, str]:
        low = path.casefold()
        name = low.rsplit("/", 1)[-1]
        excluded = any(part in low for part in ("node_modules/", "vendor/", "dist/", "build/", ".min."))
        lock = name.endswith((".lock", "lock.json"))
        supported = name == "dockerfile" or name.endswith((
            ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".kt", ".cs",
            ".rb", ".rs", ".php", ".scala", ".sql", ".yml", ".yaml", ".json", ".toml", ".tf",
        ))
        kind_rank = {"implementation": 0, "test_code": 1, "configuration": 2}.get(cls._source_kind(path), 3)
        return (1 if excluded or lock or not supported else 0, kind_rank, low)

    def _repomix_auth_env(self) -> dict:
        """Git auth env so a repomix ``--remote`` clone can reach a private repo.

        Repomix shells out to plain ``git clone`` and has no token plumbing of
        its own (an unreachable remote is silently treated as a local path, so
        an unauthenticated private clone packs nothing).  Auth is injected via
        git's environment-based config: an ``http.extraheader`` for github.com
        carrying Basic credentials built from the connected token.
        ``GIT_TERMINAL_PROMPT=0`` makes a bad or expired token fail fast instead
        of hanging on a credential prompt.  With no token the response is empty,
        so public clones stay anonymous.
        """
        header = self.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return {}
        token = header[len("Bearer "):].strip()
        if not token:
            return {}
        basic = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
        return {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }

    def _repomix_pack(self, ref: str, revision: str) -> dict:
        """Return a complete source pack or an explicit, retryable intake outcome."""
        from applypilot.onboarding.storage import atomic, digest
        self._check_cancelled()
        packing_config = {
            "input": {"maxFileSize": 50_000_000, "processors": []},
            "output": {"style": "json", "compress": False, "files": True,
                       "removeComments": False, "removeEmptyLines": False, "showLineNumbers": False,
                       "copyToClipboard": False, "git": {"includeDiffs": False, "includeLogs": False}},
            "include": [], "ignore": {"useGitignore": True, "useDotIgnore": True,
                                      "useDefaultPatterns": True, "customPatterns": []},
            "security": {"enableSecurityCheck": True},
        }
        options = {"package": "repomix", "config": packing_config,
                   "remote_trust_config": False, "version": 1}
        key = digest({"repository": ref.casefold(), "revision": revision, "options": options})
        cache = self.cache_root / (key + ".json.gz")
        base = {"pack_key": key, "packing_config": options, "pack_attempts": 0,
                "pack_status": "incomplete", "pack_error": "", "pack_cache_hit": False}
        try:
            stored = json.loads(gzip.decompress(cache.read_bytes()))
            if stored.get("key") == key and isinstance(stored.get("observations"), list):
                return {**base, "pack_status": "packed", "pack_cache_hit": True,
                        "observations": stored["observations"], "packed_files": len(stored["observations"])}
        except (OSError, EOFError, ValueError, TypeError):
            pass
        npx = shutil.which("npx") or shutil.which("npx.exe")
        if not npx:
            return {**base, "pack_error": "Repomix requires Node.js/npx; install it and retry discovery."}
        cmd = [npx, "--yes", "repomix", "--remote", ref, "--remote-branch", revision,
               "--style", "json", "--stdout", "--quiet"]
        base['pack_attempts'] = 1
        with tempfile.TemporaryDirectory(prefix='applypilot-repomix-') as work:
            config_path = Path(work) / 'repomix.config.json'
            config_path.write_text(json.dumps(packing_config), encoding='utf-8')
            proc = self._run_pack([*cmd, '--config', str(config_path)], cwd=work,
                env={**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'REPOMIX_REMOTE_TRUST_CONFIG': 'false',
                     **self._repomix_auth_env()})
        if proc.returncode:
            error = (proc.stderr or '').casefold()
            if any(marker in error for marker in ('invalid option', 'unknown option')):
                raise RuntimeError('Repomix rejected its options; update the installed Repomix package')
            if any(marker in error for marker in (
                    'authentication failed', 'repository not found', 'permission denied', 'could not read username')):
                raise PermissionError('Repomix could not access the repository; check GitHub permissions')
            raise RuntimeError(f'Repomix exited with status {proc.returncode}')
        packed = json.loads(proc.stdout or '')
        files = packed.get('files') if isinstance(packed, dict) else None
        if isinstance(files, dict):
            pairs = list(files.items())
        elif isinstance(files, list):
            pairs = [(item.get('path'), item.get('content')) for item in files if isinstance(item, dict)]
        else:
            raise ValueError('Repomix JSON has no files collection')
        rows = [{'source_revision': revision, 'path': path, 'excerpt': content,
                 'source_kind': self._source_kind(path)}
                for path, content in pairs if isinstance(path, str) and path
                and isinstance(content, str) and content.strip()]
        atomic(cache, gzip.compress(json.dumps({'key': key, 'observations': rows},
            ensure_ascii=False).encode('utf-8')))
        return {**base, 'pack_status': 'packed', 'pack_error': '',
                'observations': rows, 'packed_files': len(rows)}

    def _fetch(self, url: str, *, allow_empty: bool = False) -> Any:
        """One GitHub request; provider quota facts pass to the Activity caller."""
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname != 'api.github.com' or parsed.username or parsed.password:
            raise ValueError('GitHub evidence only requests api.github.com')

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        self._check_cancelled()
        req = urllib.request.Request(url, headers=self.headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as response:
                content = response.read(4_000_001)
                if len(content) > 4_000_000:
                    raise ValueError('GitHub response exceeded 4 MB')
                return json.loads(content.decode('utf-8'))
        except urllib.error.HTTPError as exc:
            primary = exc.code in {403, 429} and exc.headers.get('X-RateLimit-Remaining') == '0'
            secondary = exc.code == 429 or exc.code == 403 and bool(exc.headers.get('Retry-After'))
            if primary or secondary:
                from applypilot.integrations.search_providers import retry_after
                from applypilot.integrations.types import SourceError
                delay = retry_after(exc.headers.get('Retry-After', ''))
                reset = exc.headers.get('X-RateLimit-Reset')
                if primary and reset is not None:
                    try:
                        seconds = float(reset) - time.time()
                    except (TypeError, ValueError):
                        seconds = None
                    reset_delay = retry_after(str(seconds)) if seconds is not None else None
                    if reset_delay is not None and reset_delay > 0:
                        delay = max(delay or 0, reset_delay)
                self.rate_limited = True
                self.last_error = f'GitHub API rate limited (HTTP {exc.code})'
                failure = SourceError(self.last_error, 'rate_limit', delay)
                failure.http_status = exc.code
                failure.rate_limit_remaining = exc.headers.get('X-RateLimit-Remaining')
                failure.rate_limit_reset = reset
                raise failure from exc
            self.last_error = 'GitHub HTTP ' + str(exc.code)
            if exc.code in {401, 403}:
                self.last_error += ': check the saved account, repository permissions and organization SSO authorization.'
            if exc.code == 409 and allow_empty:
                return {'empty_repository': True}
            if exc.code == 404 and '/readme' in parsed.path:
                return None
            raise

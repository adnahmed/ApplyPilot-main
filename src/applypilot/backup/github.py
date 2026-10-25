"""Private Git checkpoints; restic owns all Release object storage."""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path

from .common import BRANCH, canonical, repository

MANIFEST = "applypilot-manifest.json"


class Remote:
    def __init__(self, root: Path, name: str, *, cfg=None, create_missing=False, _credential_retry=True):
        try:
            from github import Auth, Github
        except ImportError:
            raise ValueError("Install backup dependencies with uv sync (PyGithub is required)") from None
        self.name = repository(name)
        from applypilot.integrations.github_auth import (
            GitHubCredentialError, backup_auth_status, backup_token,
            missing_backup_credential, rejected_backup_credential,
        )
        selected = backup_auth_status(root, cfg)
        self.auth_source = selected["source"]
        token = backup_token(root, cfg)
        if not token:
            raise missing_backup_credential(self.auth_source)
        self.token = token
        self.client = Github(auth=Auth.Token(token), timeout=30, retry=0,
                             per_page=100, seconds_between_requests=.25, seconds_between_writes=1)
        try:
            self.login = self.client.get_user().login
            if not isinstance(self.login, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", self.login):
                raise ValueError("GitHub did not return a valid authenticated account")
            expected = selected["account"]
            if expected and self.login.casefold() != expected.casefold():
                raise GitHubCredentialError(
                    "The selected backup credential belongs to a different GitHub account than the "
                    "saved backup account. Select the intended account explicitly; no upload was attempted.",
                    source=self.auth_source)
            try:
                self.repo = self.client.get_repo(self.name)
            except Exception as exc:
                owner, repository_name = self.name.split("/", 1)
                if getattr(exc, "status", None) != 404 or not create_missing or owner.casefold() != self.login.casefold():
                    raise
                # Creation cannot replace an inaccessible existing repository:
                # GitHub rejects the duplicate name. Only the selected owner's
                # namespace is eligible for automatic private preparation.
                self.repo = self.client.get_user().create_repo(repository_name, private=True, auto_init=False)
            if not self.repo.private:
                raise ValueError("Backups require a PRIVATE repository; nothing was uploaded")
            if self.repo.archived:
                raise ValueError("The backup repository is archived")
        except Exception as exc:
            self.client.close()
            status = getattr(exc, "status", None)
            if status == 401:
                if _credential_retry and backup_token(root, cfg) not in {"", token}:
                    self.__init__(root, name, cfg=cfg, create_missing=create_missing, _credential_retry=False)
                    return
                raise rejected_backup_credential(self.auth_source) from None
            if status == 404:
                raise ValueError("The selected GitHub account cannot see the configured private backup repository. "
                                 "Check the repository name and grant that credential access; do not recreate the repository.") from None
            if status == 403:
                raise ValueError("GitHub denied backup access (HTTP 403). Check repository permissions, "
                                 "organization authorization and rate limits. No alternate credential was tried.") from None
            raise

    def require_write_access(self):
        permissions = self.repo.permissions
        push = permissions.get("push") if isinstance(permissions, dict) else getattr(permissions, "push", None)
        if push is False:
            raise ValueError("The selected GitHub account has no write access to the private backup repository. "
                             "Grant repository Contents read/write access or choose the authorized CLI account.")
        # Missing metadata is not proof of write access. Only a completed Sync
        # (including restic integrity verification) verifies a new recovery point.

    def close(self):
        self.client.close()

    def storage_usage(self, prefix: str) -> dict:
        """Count stored encrypted assets without downloading their contents."""
        from applypilot.onboarding.storage import now
        result = {"bytes": 0, "assets": 0, "releases": 0, "complete": True,
                  "measured_at": now(), "scope": "Encrypted Release assets for this workspace, including retained storage namespaces"}
        deadline = time.monotonic() + 5
        for index, release in enumerate(self.repo.get_releases()):
            if index >= 200 or time.monotonic() > deadline:
                result["complete"] = False
                break
            if not str(release.tag_name).startswith(prefix + "-"):
                continue
            result["releases"] += 1
            for asset in release.get_assets():
                if result["assets"] >= 10000 or time.monotonic() > deadline:
                    result["complete"] = False
                    return result
                result["bytes"] += max(0, int(asset.size or 0))
                result["assets"] += 1
        return result

    def head(self):
        from github import GithubException
        try:
            return self.repo.get_git_ref("heads/" + BRANCH).object.sha
        except GithubException as exc:
            if exc.status in (404, 409): return None
            raise

    def files(self, head: str) -> dict:
        tree = self.repo.get_git_tree(head, recursive=True)
        if tree.truncated:
            raise ValueError("Remote Git tree is truncated; refusing an incomplete recovery")
        return {x.path: {"sha": x.sha, "size": x.size, "type": x.type, "mode": x.mode}
                for x in tree.tree if x.type != "tree"}

    def blob(self, sha: str, max_size=8 * 1024**2) -> bytes:
        blob = self.repo.get_git_blob(sha)
        if blob.size > max_size or blob.encoding != "base64":
            raise ValueError("Remote Git file exceeds the supported recovery size")
        raw = base64.b64decode(blob.content)
        if len(raw) != blob.size:
            raise ValueError("Incomplete Git blob")
        return raw

    def metadata(self, head=None):
        head = head or self.head()
        if not head: return None
        paths = self.files(head)
        if MANIFEST not in paths:
            raise ValueError("Backup branch is not a recognized checkpoint; use a different private repository")
        return json.loads(self.blob(paths[MANIFEST]["sha"]))

    def ensure_repository(self):
        """Seed an empty repository before restic creates its storage shards."""
        from github import GithubException
        try:
            self.repo.get_git_ref("heads/" + self.repo.default_branch)
        except GithubException as exc:
            if exc.status not in (404, 409): raise
            # A completely empty private repository needs one initial commit before Git Data APIs work.
            self.repo.create_file("README.md", "Initialize private ApplyPilot archive",
                                  "# Private ApplyPilot archive\nRecovery data lives on " + BRANCH + ".\n")

    def commit(self, files: dict[str, bytes], manifest: dict, expected: str | None) -> str:
        from github import InputGitTreeElement
        if self.head() != expected:
            raise ValueError("Remote backup changed on another device; restore/reconcile before uploading")
        if sum(len(v) for v in files.values()) > 32 * 1024**2 or len(files) > 5000:
            raise ValueError("Plain Git checkpoint exceeds 32 MiB/5000 files; keep large evidence in encrypted snapshots")
        parent = self.repo.get_git_commit(expected) if expected else None
        complete = files | {MANIFEST: canonical(manifest), "README.md":
            b"# ApplyPilot personal archive\n\nfiles/: readable configuration and diary. journal/: impact views.\n"
            b"Encrypted databases/documents are in Releases. Restore through ApplyPilot Backups.\n"
            b"Repository read access permits recovery. Keep this repository private. Do not run two writers.\n"}
        tree = self.repo.create_git_tree([InputGitTreeElement(path=k, mode="100644", type="blob", content=v.decode("utf-8"))
                                          for k, v in sorted(complete.items())])
        commit = self.repo.create_git_commit("ApplyPilot checkpoint " + manifest["created_at"], tree, [parent] if parent else [])
        if expected:
            self.repo.get_git_ref("heads/" + BRANCH).edit(commit.sha, force=False)
        else:
            self.repo.create_git_ref("refs/heads/" + BRANCH, commit.sha)
        return commit.sha

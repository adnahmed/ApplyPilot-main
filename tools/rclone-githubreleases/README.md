# ApplyPilot GitHub Releases transport

This is the `applypilot-rclone` binary used by ApplyPilot's restic repository.
It pins rclone 1.75.1 and adds one out-of-tree `githubreleases` backend. Restic
owns encryption, snapshots, deduplication, retention and integrity checking;
this backend only stores objects.

## Build

Running `uv sync` from the ApplyPilot checkout builds and installs this binary
alongside restic in the Python environment's `applypilot_backup_bins` package.
The Hatch build hook in `scripts/build_backup_tools.py` uses the isolated
`go-bin` build dependency. The first sync can take longer while it downloads and
compiles tools; subsequent syncs reuse the build cache. Backup and recovery
operations use the installed binary without installing tools at runtime.

For development, install Go 1.26 or newer, then run from this directory:

```sh
go build -mod=readonly -trimpath -o applypilot-rclone .
```

On Windows use `-o applypilot-rclone.exe`. ApplyPilot's installation build
uses the same source. The binary includes
`serve restic`, `config`, `version`, `ls`, `lsd` and `lsjson`; it does not import
rclone's complete collection of providers. Rclone's operation package also
registers its `crypt` wrapper.

## Configuration

ApplyPilot supplies these environment variables without writing a token file:

```text
RCLONE_CONFIG_APPLYPILOT_GH_TYPE=githubreleases
RCLONE_CONFIG_APPLYPILOT_GH_REPOSITORY=owner/private-repository
RCLONE_CONFIG_APPLYPILOT_GH_TOKEN=<raw GitHub token>
RCLONE_CONFIG_APPLYPILOT_GH_RELEASE_PREFIX=applypilot-store-<workspace-id>-account
```

The repository must be private and unarchived. The token needs Contents read
and write access, including visibility of draft releases. The backend targets
GitHub.com; there is no configurable API host. Tokens supplied through the
environment are raw, not rclone-obscured strings.

Restic uses repository `rclone:applypilot-gh:backup` with `rclone.program`
pointing to the binary and `rclone.args` set to:

```text
serve restic --stdio --cache-objects=false --transfers 2 --checkers 2
```

Disabling rclone's additional object cache is necessary for fresh lock reads.

ApplyPilot uses restic's native `--insecure-no-password` mode. Private GitHub
repository access controls recovery; repository readers can decrypt backups.
The `-account` namespace is separate from earlier password-protected storage.
There is no separate ApplyPilot recovery key or mailbox-based recovery record.

## Storage contract

- Objects are assets named `ap1-` followed by the unpadded base64url encoding of
  their complete UTF-8 path, including the remote root. Listings reconstruct
  the index; no manifest or local cache is required for recovery.
- Shards use `<prefix>-000001`, `<prefix>-000002`, and so on. New uploads roll
  over at 900 assets, leaving headroom below GitHub's 1,000-asset limit.
  `config` and `locks/` have dedicated `<prefix>-control` and `<prefix>-locks`
  shards. Each asset must be smaller than 2 GiB.
- Shards remain draft releases. An exact body marker proves namespace
  ownership. Published, immutable, foreign-marker or duplicate-tag shards
  cause a closed failure; the backend never publishes or deletes releases.
- Restic objects are immutable. Repeated uploads with identical bytes succeed;
  different content at an existing path is rejected without deleting it.
  GitHub's SHA-256 digest, or a full download when unavailable, verifies uploads.
  Duplicate objects discovered across shards must contain identical bytes.
- Normal inventory is cached within a process. Every lock lookup/list refreshes
  releases and lock assets, and invalidates the other inventory so the first
  operation after acquiring a lock sees the latest committed repository.
  Successful writes and deletions update the index locally.
- Mutation requests are serialized and paced at nine-second intervals per
  process, approximately 400/hour. Rate-limit reset and retry headers delay
  subsequent requests. Independent clients still share GitHub account limits;
  normal restic repository locks remain required.
- Failed or ambiguous uploads are reconciled before retries. An incomplete
  zero-byte `starter` asset can be removed after one hour; younger placeholders
  block the upload so another process's active request is preserved. HTTP
  requests have a 30-minute timeout. Namespace races fail closed if GitHub
  creates duplicate draft tags.
- Downloads support byte ranges. Redirects are restricted to HTTPS GitHub
  asset hosts and lose the Authorization header. Errors do not include API
  response bodies, token values or signed download URLs.

Do not publish, mark immutable, rename, or edit the body of live storage shards.
Do not run restic with `--no-lock` for mutation or maintenance operations.
Use restic to remove snapshots and prune unused objects; deleting assets by hand
can make the repository unrecoverable. This adapter intentionally does not offer
general-purpose rclone move, rename, purge or overwrite operations.

## Upstream references

- [Rclone 1.75.1 release](https://github.com/rclone/rclone/releases/tag/v1.75.1)
- [Rclone backend extension guide](https://github.com/rclone/rclone/blob/v1.75.1/CONTRIBUTING.md)
- [Restic rclone repositories](https://restic.readthedocs.io/en/latest/030_preparing_a_new_repo.html#other-services-via-rclone)
- [GitHub release assets API](https://docs.github.com/en/rest/releases/assets)
- [GitHub release limits](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)
- [GitHub API rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

## Validation boundary

The source has been compiled for Windows and Linux. Local binary version,
provider registration and command availability were inspected. No remote
repository was created, uploaded to, restored from or pruned during development.

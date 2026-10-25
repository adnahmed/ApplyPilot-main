# Gateway multi-repository workspace

ApplyPilot uses `gitkb/meta` to manage the private standalone gateway at
`gateway/`. The child repository is
`https://github.com/adnahmed/litellm-gateway`.

Run from the ApplyPilot repository root:

```powershell
./scripts/meta.ps1 project list --json
./scripts/meta.ps1 project check
./scripts/meta.ps1 git status --json
```

The wrapper installs checksum-verified meta 0.2.22 into the ignored `.tools/meta`
directory. Meta includes the workspace root automatically; `.meta` declares the
gateway child checkout. Use the child repository directly for gateway edits.

After cloning ApplyPilot, clone missing children with:

```powershell
./scripts/meta.ps1 git update
```

Authenticate to GitHub before cloning private repositories. This is a meta
relationship, not a Git submodule; the gateway has its own branch and history.

ApplyPilot's provider dashboard and maintenance APIs resolve
`gateway/`, with `APPLYPILOT_GATEWAY_ROOT` as an explicit override
(`APPLYPILOT_LITELLM_GUIDES` remains a legacy override).
Application-owned Temporal and research services remain under `docs/guides`.
The legacy gateway Compose entry point includes the child Compose stack.

The gateway's source, deployment files, provider inventories, and ignored local
runtime data live in the child checkout. `docs/` contains documentation only.
The old ApplyPilot provider/source snapshots are retired after the mount cutover.
Credentials remain local, ignored files; the original Docker project name is
preserved in the local gateway `.env` so existing named volumes remain intact.

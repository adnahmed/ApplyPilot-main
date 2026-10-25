# Launch from the project environment, not a stale globally installed entrypoint.
# The first network-connected sync resolves the new PostgreSQL dependencies.
# Downloads the verified CloakBrowser binary if needed; never uploads a backup itself.
[CmdletBinding()]
param(
    [ValidateSet('dashboard', 'autopilot')]
    [string]$Mode = 'dashboard'
)
$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path $PSScriptRoot -Parent
Push-Location $RepoRoot
try {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is required on PATH. Use your existing project uv installation; no packages were installed.'
    }
    if (Test-Path (Join-Path $RepoRoot 'uv.lock')) {
        & uv sync --python 3.12 --locked
    } else {
        & uv sync --python 3.12
    }
    if ($LASTEXITCODE -ne 0) { throw 'Dependency synchronization failed; dashboard was not started.' }
    & uv run --no-sync python scripts/check_desktop_runtime.py --prepare-browser
    if ($LASTEXITCODE -ne 0) { throw 'Desktop browser readiness failed. Read the diagnostic above; no dashboard was started.' }
    & uv run --no-sync applypilot $Mode
    if ($LASTEXITCODE -ne 0) { throw 'ApplyPilot exited with an error; inspect the application output.' }
} finally {
    Pop-Location
}

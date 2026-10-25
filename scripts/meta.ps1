param([Parameter(ValueFromRemainingArguments=$true)][string[]]$MetaArgs)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$install = Join-Path $root '.tools/meta'
$binary = Join-Path $install 'meta.exe'
$version = 'v0.2.22'
$digest = 'b8f2d670dcfde912495d5fc6e27efcb7eba32b3f124a1056616fc3c81d2c3706'

if (-not (Test-Path -LiteralPath $binary)) {
    if (-not (Test-Path -LiteralPath $root)) { throw 'Workspace root does not exist' }
    New-Item -ItemType Directory -Force -Path $install | Out-Null
    $archive = Join-Path $install 'meta-windows-x64.zip'
    & gh release download $version --repo gitkb/meta --pattern meta-windows-x64.zip --dir $install --clobber
    if ($LASTEXITCODE -ne 0) { throw 'Could not download gitkb/meta' }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $digest) {
        throw 'gitkb/meta release checksum mismatch'
    }
    Expand-Archive -LiteralPath $archive -DestinationPath $install -Force
    if (-not (Test-Path -LiteralPath $binary)) { throw 'Release archive does not contain meta.exe' }
}

$env:PATH = "$install;$env:PATH"
& $binary @MetaArgs
exit $LASTEXITCODE

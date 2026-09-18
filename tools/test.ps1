[CmdletBinding()]
param(
    [switch]$RequireDotnet,
    [switch]$SkipModBuild
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'preflight.ps1')

& uv sync --project (Join-Path $repoRoot 'runtime')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') ruff check (Join-Path $repoRoot 'runtime')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') pytest (Join-Path $repoRoot 'runtime\tests') (Join-Path $repoRoot 'tests')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') stardew-ai-runtime --check
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') python (Join-Path $repoRoot 'tools\check_release_boundaries.py')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$localDotnet = Join-Path $repoRoot '.local\dotnet\dotnet.exe'
if ((Get-Command dotnet -ErrorAction SilentlyContinue) -or (Test-Path -LiteralPath $localDotnet) -or ($env:STAR_DOTNET_PATH -and (Test-Path -LiteralPath $env:STAR_DOTNET_PATH))) {
    if (-not $SkipModBuild) {
        & (Join-Path $PSScriptRoot 'build-mod.ps1')
        exit $LASTEXITCODE
    }
    exit 0
}

if ($RequireDotnet) {
    Write-Error '.NET SDK is required but missing.'
    exit 1
}

Write-Warning 'C# Mod build skipped because the .NET SDK is not installed.'

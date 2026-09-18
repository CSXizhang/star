[CmdletBinding()]
param(
    [string]$Version = '6.0.428'
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$repoRoot = Split-Path -Parent $PSScriptRoot
$bootstrapDirectory = Join-Path $repoRoot '.local\bootstrap'
$installDirectory = Join-Path $repoRoot '.local\dotnet'
$installerPath = Join-Path $bootstrapDirectory 'dotnet-install.ps1'

New-Item -ItemType Directory -Force -Path $bootstrapDirectory | Out-Null
New-Item -ItemType Directory -Force -Path $installDirectory | Out-Null

Invoke-WebRequest -UseBasicParsing -Uri 'https://dot.net/v1/dotnet-install.ps1' -OutFile $installerPath
& $installerPath -Version $Version -InstallDir $installDirectory -NoPath
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$dotnetPath = Join-Path $installDirectory 'dotnet.exe'
if (-not (Test-Path -LiteralPath $dotnetPath)) {
    throw 'The local .NET SDK installer completed without producing dotnet.exe.'
}

& $dotnetPath --info

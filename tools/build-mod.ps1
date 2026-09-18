[CmdletBinding()]
param(
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Debug'
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$repoRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'preflight.ps1')

$dotnetCommand = Get-Command dotnet -ErrorAction SilentlyContinue
$localDotnet = Join-Path $repoRoot '.local\dotnet\dotnet.exe'
$dotnetPath = if ($env:STAR_DOTNET_PATH -and (Test-Path -LiteralPath $env:STAR_DOTNET_PATH)) {
    $env:STAR_DOTNET_PATH
} elseif ($dotnetCommand) {
    $dotnetCommand.Source
} elseif (Test-Path -LiteralPath $localDotnet) {
    $localDotnet
} else {
    $null
}
if (-not $dotnetPath) {
    throw '.NET SDK is not installed. Run tools/bootstrap-dotnet.ps1, then rerun tools/preflight.ps1 -Strict.'
}
if (-not $env:STARDEW_GAME_PATH) {
    throw 'STARDEW_GAME_PATH is not set in .env.local.'
}

& $dotnetPath build (Join-Path $repoRoot 'src\StardewAI.Companion.Mod\StardewAI.Companion.Mod.csproj') --configuration $Configuration
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

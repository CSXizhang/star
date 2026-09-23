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

# 同步纯净构建产物至分发源目录 artifacts/dist/StardewAI.Companion.Mod
$binDir = Join-Path $repoRoot "artifacts\bin\StardewAI.Companion.Mod\$Configuration\net6.0"
$distDir = Join-Path $repoRoot "artifacts\dist\StardewAI.Companion.Mod"
[void][System.IO.Directory]::CreateDirectory($distDir)
$prodFiles = @('manifest.json', 'StardewAI.Companion.Mod.dll', 'StardewAI.Companion.Mod.pdb', 'StardewAI.Companion.Mod.deps.json')
foreach ($file in $prodFiles) {
    $src = Join-Path $binDir $file
    if (Test-Path -LiteralPath $src) {
        Copy-Item -LiteralPath $src -Destination (Join-Path $distDir $file) -Force
    }
}
Write-Host "Mod 构建完成并同步至分发目录: $distDir"

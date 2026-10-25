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

# Load .env.local
$envFile = Join-Path $repoRoot '.env.local'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        $parts = $trimmed.Split('=', 2)
        if ($parts.Count -eq 2) {
            [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
        }
    }
}

$localDotnet = Join-Path $repoRoot '.local\dotnet\dotnet.exe'
$dotnetPath = if ($env:STAR_DOTNET_PATH -and (Test-Path -LiteralPath $env:STAR_DOTNET_PATH)) {
    $env:STAR_DOTNET_PATH
} elseif (Get-Command dotnet -ErrorAction SilentlyContinue) {
    (Get-Command dotnet).Source
} elseif (Test-Path -LiteralPath $localDotnet) {
    $localDotnet
} else {
    throw '.NET SDK not found.'
}

$proj = Join-Path $repoRoot 'src\StardewAI.Companion.TestDriver\StardewAI.Companion.TestDriver.csproj'
Write-Host "Building TestDriver project: $proj ($Configuration)..."
& $dotnetPath build $proj --configuration $Configuration
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Host "TestDriver build succeeded."

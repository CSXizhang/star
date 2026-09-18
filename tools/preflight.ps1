[CmdletBinding()]
param(
    [switch]$Strict
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $repoRoot '.env.local'

if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) {
            continue
        }
        $parts = $trimmed.Split('=', 2)
        if ($parts.Count -eq 2) {
            [Environment]::SetEnvironmentVariable($parts[0], $parts[1], 'Process')
        }
    }
}

$checks = @()
function Add-Check {
    param([string]$Name, [bool]$Required, [bool]$Ready, [string]$Details)
    $script:checks += [pscustomobject]@{
        Name = $Name
        Required = $Required
        Status = if ($Ready) { 'ready' } else { 'missing' }
        Details = $Details
    }
}

$git = Get-Command git -ErrorAction SilentlyContinue
Add-Check 'Git' $true ([bool]$git) $(if ($git) { & git --version } else { 'not found' })

$uv = Get-Command uv -ErrorAction SilentlyContinue
Add-Check 'uv' $true ([bool]$uv) $(if ($uv) { & uv --version } else { 'not found' })

$python = Get-Command python -ErrorAction SilentlyContinue
Add-Check 'Python' $true ([bool]$python) $(if ($python) { & python --version 2>&1 } else { 'not found' })

$dotnetCommand = Get-Command dotnet -ErrorAction SilentlyContinue
$dotnetPath = if ($env:STAR_DOTNET_PATH -and (Test-Path -LiteralPath $env:STAR_DOTNET_PATH)) {
    $env:STAR_DOTNET_PATH
} elseif ($dotnetCommand) {
    $dotnetCommand.Source
} else {
    $localDotnet = Join-Path $repoRoot '.local\dotnet\dotnet.exe'
    if (Test-Path -LiteralPath $localDotnet) { $localDotnet } else { $null }
}
$sdkList = if ($dotnetPath) { (& $dotnetPath --list-sdks) -join '; ' } else { 'run tools/bootstrap-dotnet.ps1' }
Add-Check '.NET SDK' $true ([bool]$dotnetPath) $sdkList

$gamePath = $env:STARDEW_GAME_PATH
$gameDll = if ($gamePath) { Join-Path $gamePath 'Stardew Valley.dll' } else { $null }
$gameReady = [bool]$gameDll -and (Test-Path -LiteralPath $gameDll)
$gameVersion = if ($gameReady) { [System.Diagnostics.FileVersionInfo]::GetVersionInfo($gameDll).FileVersion } else { 'set STARDEW_GAME_PATH' }
Add-Check 'Stardew Valley' $true $gameReady $gameVersion

$smapiPath = $env:STAR_SMAPI_PATH
if (-not $smapiPath -and $gamePath) {
    $smapiPath = Join-Path $gamePath 'StardewModdingAPI.exe'
}
$smapiReady = [bool]$smapiPath -and (Test-Path -LiteralPath $smapiPath)
$smapiVersion = if ($smapiReady) { [System.Diagnostics.FileVersionInfo]::GetVersionInfo($smapiPath).FileVersion } else { 'set STAR_SMAPI_PATH' }
Add-Check 'SMAPI' $true $smapiReady $smapiVersion

Add-Check 'Interactive desktop' $true ([Environment]::UserInteractive) ([Environment]::UserName)

$checks | Format-Table -AutoSize

$missing = @($checks | Where-Object { $_.Required -and $_.Status -ne 'ready' })
if ($missing.Count -gt 0) {
    Write-Warning ("Baseline has {0} missing required item(s): {1}" -f $missing.Count, (($missing | Select-Object -ExpandProperty Name) -join ', '))
    if ($Strict) {
        exit 1
    }
}

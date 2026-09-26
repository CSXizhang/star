[CmdletBinding()]
param([switch]$CheckOnly, [Parameter(ValueFromRemainingArguments=$true)][string[]]$ForwardArgs)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (Test-Path -LiteralPath (Join-Path $repoRoot 'release-manifest.json')) {
    . (Join-Path $PSScriptRoot 'release-package.ps1')
    try {
        Start-ReleaseCompanion $repoRoot -CheckOnly:$CheckOnly -ForwardArgs $ForwardArgs
        exit 0
    } catch {
        $logDir = Join-Path $repoRoot 'data'
        [void][IO.Directory]::CreateDirectory($logDir)
        Add-Content -LiteralPath (Join-Path $logDir 'release-start.log') -Value ("$(Get-Date -Format o) $_") -Encoding UTF8
        throw
    }
}
. (Join-Path $PSScriptRoot 'candidate-package.ps1')
$python = Join-Path $repoRoot 'runtime\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }
$metadata = Join-Path $repoRoot 'config\installed-candidate.json'
$serviceArgs = @('-m', 'stardew_ai_runtime.chat_bridge')
if (Test-Path -LiteralPath $metadata) {
    $installed = Get-Content -LiteralPath $metadata -Raw -Encoding UTF8 | ConvertFrom-Json
    $manifestPath = if ([IO.Path]::IsPathRooted($installed.manifest)) { $installed.manifest } else { Join-Path $repoRoot $installed.manifest }
    $candidate = Read-VerifiedCandidate $repoRoot $manifestPath -AllowUnaccepted:$CheckOnly
    Assert-FormalCandidateTarget $installed.gameDirectory $installed.modDirectory
    if ($candidate.version -ne $installed.version -or
        (Get-CandidateHash (Join-Path $installed.modDirectory 'StardewAI.Companion.Mod.dll')) -ne $candidate.modSha256) {
        if ($candidate.manifestType -eq 'local-dev' -or $candidate.version -eq 'local-dev') {
            throw 'Installed Mod and local-dev runtime differ. Run tools/build-mod.ps1 and tools/setup-companion.ps1.'
        }
        throw 'Installed Mod and candidate runtime differ. Close the game and use the candidate installer.'
    }
    if ($ForwardArgs | Where-Object { $_ -match '^--run-dir(=|$)' }) {
        throw 'Installed package is bound to its explicit normal-game instance.'
    }
    $serviceArgs += @('--run-dir', $installed.modDirectory)
    Write-Host "Candidate $($candidate.version): normal-game instance selected automatically."
}
if ($CheckOnly) {
    Write-Host 'Service entry checked. No service or game was started.'
    exit 0
}
Set-Location -LiteralPath $repoRoot
& $python @serviceArgs @ForwardArgs
exit $LASTEXITCODE

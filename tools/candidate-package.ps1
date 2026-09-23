function Get-CandidateHash([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return [BitConverter]::ToString($sha.ComputeHash($stream)).Replace('-', '') }
    finally { $stream.Dispose(); $sha.Dispose() }
}
# Shared read-only checks for the user-operated candidate installer and service.
function Read-VerifiedCandidate([string]$RepoRoot, [string]$ManifestPath, [switch]$AllowUnaccepted) {
    $root = [IO.Path]::GetFullPath($RepoRoot).TrimEnd('\') + '\'
    $manifestFile = [IO.Path]::GetFullPath($ManifestPath)
    if (-not (Test-Path -LiteralPath $manifestFile)) { throw 'Candidate is not ready. Ask for a completed candidate delivery first.' }
    $candidate = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $isLocalDev = ($candidate.manifestType -eq 'local-dev' -or $candidate.version -eq 'local-dev')
    if ((-not $isLocalDev -and -not $AllowUnaccepted -and $candidate.acceptancePassed -ne $true -and $candidate.offlineRepairInstallable -ne $true) -or -not $candidate.version -or @($candidate.files).Count -lt 4) {
        throw 'Candidate has no completed acceptance or version manifest.'
    }
    foreach ($entry in $candidate.files) {
        $file = [IO.Path]::GetFullPath((Join-Path $root $entry.path))
        if (-not $file.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Candidate file escapes workspace.' }
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Candidate file missing: $($entry.path)" }
        if ((Get-CandidateHash $file) -ne $entry.sha256) {
            if ($isLocalDev) {
                throw "Local-dev artifact changed: $($entry.path). Please run tools/setup-companion.ps1 to sync."
            }
            throw "Candidate version changed: $($entry.path). Please prepare the tested candidate again."
        }
    }
    $source = [IO.Path]::GetFullPath((Join-Path $root $candidate.modSource))
    if (-not $source.StartsWith((Join-Path $root 'artifacts') + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Candidate Mod source must be a frozen workspace artifact.'
    }
    $runtimePython = Join-Path $root 'runtime\.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $runtimePython)) {
        if ($isLocalDev -and (Get-Command python.exe -ErrorAction SilentlyContinue)) {
            $runtimePython = (Get-Command python.exe).Source
        } else {
            throw 'The matching workspace Python runtime is missing.'
        }
    }
    $runtimeOrigin = & $runtimePython -c "import importlib.util; print(importlib.util.find_spec('stardew_ai_runtime').origin)"
    $expectedRuntime = Join-Path $root 'runtime\src\stardew_ai_runtime'
    if ($LASTEXITCODE -ne 0 -or -not $runtimeOrigin -or -not ([IO.Path]::GetFullPath($runtimeOrigin.Trim())).StartsWith($expectedRuntime + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Python resolves a different runtime checkout. Candidate installation is blocked.'
    }
    if ($candidate.runtimeEnvironment) {
        $environmentJson = & $runtimePython -c "import json,sys,importlib.metadata as m; print(json.dumps({'python':sys.version.split()[0], 'packages':{d.metadata['Name']:d.version for d in m.distributions()}}))"
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect candidate runtime dependencies.' }
        $environment = $environmentJson | ConvertFrom-Json
        if ($environment.python -ne $candidate.runtimeEnvironment.python) { throw 'Candidate Python version changed.' }
        foreach ($package in $candidate.runtimeEnvironment.packages.PSObject.Properties) {
            if ($environment.packages.($package.Name) -ne $package.Value) { throw "Candidate dependency changed: $($package.Name)" }
        }
    }
    $candidate | Add-Member -NotePropertyName ResolvedModSource -NotePropertyValue $source -Force
    return $candidate
}

function Assert-FormalCandidateTarget([string]$GameDirectory, [string]$ModDirectory) {
    $game = [IO.Path]::GetFullPath($GameDirectory).TrimEnd('\')
    $mod = [IO.Path]::GetFullPath($ModDirectory).TrimEnd('\')
    $expected = Join-Path $game 'Mods\StardewAI.Companion.Mod'
    if ($mod -ne $expected -or $mod -match '(?i)[\\/]\.test-runs[\\/]') {
        throw 'Candidate requires the detected normal game Mods directory, never an isolated test run.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $game 'StardewModdingAPI.exe'))) { throw 'SMAPI was not found in the detected game.' }
    if (Test-Path -LiteralPath (Join-Path $game 'Mods\StardewAI.Companion.TestDriver')) {
        throw 'TestDriver is present in normal Mods. Candidate will not start or remove it automatically.'
    }
}

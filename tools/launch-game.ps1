[CmdletBinding()]
param(
    [ValidateSet('baseline-gate', 'arrange-smoke', 'scenario', 'init-golden-v1', 'interactive', 'test')]
    [string]$Mode = 'arrange-smoke',
    [string]$Scenario = 'normal-watering',
    [string]$RunId = '',
    [int]$Port = 37788,
    [int]$TimeoutSeconds = 120,
    [string]$FrozenProdDir = '',
    # Optional expected SHA256 of the deployed production Mod DLL. When set, the run
    # fails fast if the frozen package did not deploy the exact expected assembly
    # (catches the "Skipped Mod DLL doesn't exist" / wrong nesting class of defect).
    [string]$ExpectedProdDllSha256 = '',
    [string]$CustomSavePath = '',
    [switch]$PresetAcceptance,
    [switch]$NoOrchestrator,
    # Optional child-process environment overrides (name => value). Used by sandboxed
    # runs to redirect SMAPI's %APPDATA% error-log path into the workspace, because
    # the DSH file sandbox denies writes under the user's real AppData.
    [hashtable]$EnvironmentOverrides = @{}
)

$ErrorActionPreference = 'Stop'
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

$gamePath = $env:STARDEW_GAME_PATH
if (-not $gamePath) {
    $gamePath = (& (Join-Path $PSScriptRoot 'detect-game.ps1') | Out-String).Trim()
}
$smapiExe = $env:STAR_SMAPI_PATH
if (-not $smapiExe) {
    $smapiExe = Join-Path $gamePath "StardewModdingAPI.exe"
}

if (-not (Test-Path -LiteralPath $smapiExe)) {
    throw "SMAPI executable not found at: $smapiExe"
}

$testRunsDir = Join-Path $repoRoot ".test-runs"
$infraArtifactsDir = Join-Path $repoRoot "artifacts\infra"
$goldenV1Base = Join-Path $repoRoot "golden-saves\private\Stage0-Golden-v1"
$goldenV1Save = Join-Path $goldenV1Base "AstraFarm_448732481"
$goldenManifestFile = Join-Path $infraArtifactsDir "golden-save-v1.sha256"

foreach ($d in @($testRunsDir, $infraArtifactsDir)) {
    if (-not (Test-Path -LiteralPath $d)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
    }
}

# --- SAFETY HELPER FUNCTIONS ---

function Assert-NoExistingGameProcesses {
    $existingProcs = Get-Process -Name "Stardew Valley", "StardewModdingAPI" -ErrorAction SilentlyContinue
    if ($existingProcs) {
        $procSummary = ($existingProcs | ForEach-Object { "PID=$($_.Id) ($($_.ProcessName), StartTime=$($_.StartTime))" }) -join ", "
        throw "SAFETY PRE-CHECK FAILED: Unrelated game process(es) currently running: $procSummary. Will NOT kill existing processes. Terminate manually or wait for completion before starting a run."
    }
}

function Stop-LaunchedChildProcess([System.Diagnostics.Process]$p, [int]$expectedPid, [System.DateTime]$expectedStartTime) {
    if ($p -and -not $p.HasExited) {
        if ($p.Id -eq $expectedPid -and $p.StartTime -eq $expectedStartTime) {
            Write-Warning "Terminating verified child process PID $expectedPid launched by this run..."
            try {
                $p.Kill()
            } catch {
                Write-Warning "Note terminating child process PID $($expectedPid): $_"
            }
        } else {
            Write-Error "PROCESS IDENTITY MISMATCH: Not killing PID $($p.Id) because process identity did not match launched child!"
        }
    }
}

function Assert-PathContained([string]$targetPath, [string]$allowedParent) {
    if (-not (Test-Path -LiteralPath $allowedParent)) {
        throw "Allowed parent directory does not exist: $allowedParent"
    }
    $fullParent = [System.IO.Path]::GetFullPath($allowedParent).TrimEnd('\', '/')
    $fullParentWithSep = $fullParent + [System.IO.Path]::DirectorySeparatorChar
    $fullTarget = [System.IO.Path]::GetFullPath($targetPath).TrimEnd('\', '/')
    $fullTargetWithSep = $fullTarget + [System.IO.Path]::DirectorySeparatorChar

    # 1. Separator-aware prefix check to strictly reject sibling prefixes (e.g. .test-runs-evil)
    if (-not $fullTargetWithSep.StartsWith($fullParentWithSep, [System.StringComparison]::OrdinalIgnoreCase) -or $fullTarget -eq $fullParent) {
        throw "PATH CONTAINMENT VIOLATION: Target '$fullTarget' is not strictly contained within '$fullParent'!"
    }

    # 2. Reject reparse points / junctions / symlinks along path
    $curr = $fullTarget
    while ($curr -and $curr.Length -ge $fullParent.Length) {
        if (Test-Path -LiteralPath $curr) {
            $item = Get-Item -LiteralPath $curr -Force
            if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "PATH CONTAINMENT VIOLATION: Path '$curr' is a reparse point (symlink/junction)!"
            }
        }
        $parentDir = [System.IO.Path]::GetDirectoryName($curr)
        if ($parentDir -eq $curr) { break }
        $curr = $parentDir
    }
}

function Assert-ValidRunId([string]$candidateRunId, [string]$parentDir) {
    if ([string]::IsNullOrWhiteSpace($candidateRunId)) {
        throw "RUN-ID VIOLATION: RunId cannot be empty or whitespace."
    }
    if ($candidateRunId -notmatch '^[a-zA-Z0-9_-]+$') {
        throw "RUN-ID VIOLATION: RunId '$candidateRunId' contains illegal characters. Only alphanumeric, dash, and underscore allowed."
    }
    if ($candidateRunId -like "*..*" -or $candidateRunId -like "*/*" -or $candidateRunId -like "*\*") {
        throw "RUN-ID VIOLATION: RunId '$candidateRunId' contains traversal sequences."
    }
    $targetDir = Join-Path $parentDir $candidateRunId
    if (Test-Path -LiteralPath $targetDir) {
        throw "RUN-ID REUSE VIOLATION: Run directory already exists: '$targetDir'. Reusing RunId is forbidden to preserve evidence and fresh-state repeatability."
    }
}

function Assert-GoldenFixtureManifest([string]$goldenDir, [string]$manifestFile) {
    if (-not (Test-Path -LiteralPath $goldenDir)) {
        throw "GOLDEN FIXTURE VIOLATION: Golden save directory does not exist: $goldenDir"
    }
    if (-not (Test-Path -LiteralPath $manifestFile)) {
        throw "GOLDEN FIXTURE VIOLATION: Authoritative manifest does not exist: $manifestFile"
    }

    $manifestLines = (Get-Content -LiteralPath $manifestFile -Raw).Trim().Split("`n") | ForEach-Object { $_.Trim() } | Where-Object { $_ }
    $expectedHashes = @{}
    foreach ($line in $manifestLines) {
        $parts = $line -split '\s+', 2
        if ($parts.Count -eq 2) {
            $expectedHashes[$parts[1].Replace('/', '\')] = $parts[0]
        }
    }

    if ($expectedHashes.Count -ne 4) {
        throw "GOLDEN FIXTURE VIOLATION: Expected exactly 4 files in golden manifest, got: $($expectedHashes.Count)"
    }

    $actualFiles = Get-ChildItem -LiteralPath $goldenDir -File | Sort-Object Name
    if ($actualFiles.Count -ne 4) {
        throw "GOLDEN FIXTURE VIOLATION: Golden fixture directory contains $($actualFiles.Count) files, expected exactly 4!"
    }

    foreach ($f in $actualFiles) {
        $rel = "AstraFarm_448732481\" + $f.Name
        if (-not $expectedHashes.ContainsKey($rel)) {
            throw "GOLDEN FIXTURE VIOLATION: Unexpected file '$($f.Name)' in Golden fixture!"
        }
        $h = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash
        if ($h -ne $expectedHashes[$rel]) {
            throw "GOLDEN FIXTURE VIOLATION: SHA-256 mismatch for '$($f.Name)'! Expected $($expectedHashes[$rel]), got $h"
        }
    }
    Write-Host "Golden fixture AstraFarm_448732481 verified against authoritative manifest: $manifestFile"
}

function Get-FolderHashes([string]$folderPath) {
    $hashes = @{}
    if (Test-Path -LiteralPath $folderPath) {
        if ($PresetAcceptance -and $folderPath -eq $dailyModsDir) {
            # The isolated --mods-path never loads this tree. Check installed mod
            # entry files without traversing unrelated caches and bundled runtimes.
            $files = @(Get-ChildItem -LiteralPath $folderPath -File)
            foreach ($modDir in Get-ChildItem -LiteralPath $folderPath -Directory) {
                if ($modDir.Attributes -band [System.IO.FileAttributes]::ReparsePoint) { continue }
                $files += @(Get-ChildItem -LiteralPath $modDir.FullName -File)
            }
        } else {
            $files = Get-ChildItem -LiteralPath $folderPath -Recurse -File | Sort-Object FullName
        }
        foreach ($f in $files) {
            if ($folderPath -eq $dailyModsDir -and $f.FullName -match '[\\/]logs[\\/][^\\/]+\.log$') { continue }
            $h = Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256
            $rel = $f.FullName.Substring($folderPath.Length).TrimStart('\', '/')
            $hashes[$rel] = $h.Hash
        }
    }
    return $hashes
}

function Write-Utf8File([string]$path, [string]$content) {
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($path, $content, $utf8NoBom)
}

# --- Mode: init-golden-v1 ---
if ($Mode -eq 'init-golden-v1') {
    Write-Host "============================================================"
    Write-Host " INITIALIZING / VERIFYING IMMUTABLE STAGE0-GOLDEN-V1 FIXTURE"
    Write-Host "============================================================"
    if (Test-Path -LiteralPath $goldenV1Save) {
        Write-Host "Stage0-Golden-v1 directory already exists at: $goldenV1Save"
        Write-Host "Verifying existing fixture against authoritative manifest..."
        Assert-GoldenFixtureManifest $goldenV1Save $goldenManifestFile
        Write-Host ">>> Stage0-Golden-v1 is already initialized and verified. Will NOT overwrite existing immutable fixture. <<<"
        exit 0
    }

    $sourceDir = Join-Path $testRunsDir "isolated-saves\AstraFarm_448732481"
    if (-not (Test-Path -LiteralPath $sourceDir)) {
        throw "Source save directory not found: $sourceDir"
    }

    New-Item -ItemType Directory -Path $goldenV1Save -Force | Out-Null
    $sourceFiles = Get-ChildItem -LiteralPath $sourceDir -File
    foreach ($f in $sourceFiles) {
        $dest = Join-Path $goldenV1Save $f.Name
        Copy-Item -LiteralPath $f.FullName -Destination $dest
        Write-Host "  Copied $($f.Name) ($($f.Length) bytes) to $goldenV1Save"
    }

    $v1Files = Get-ChildItem -LiteralPath $goldenV1Save -File | Sort-Object Name
    if ($v1Files.Count -ne 4) {
        throw "Source save did not contain exactly 4 files! Found $($v1Files.Count)"
    }

    $hashLines = @()
    foreach ($f in $v1Files) {
        $h = Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256
        $rel = "AstraFarm_448732481\" + $f.Name
        $hashLines += "$($h.Hash)  $rel"
    }

    Write-Utf8File $goldenManifestFile (($hashLines -join "`n") + "`n")
    Write-Host "Recorded golden save v1 hashes to: $goldenManifestFile"
    Write-Host ">>> Golden Save v1 created successfully. Exiting. <<<"
    exit 0
}

Write-Host "============================================================"
Write-Host " STARDEW AI COMPANION - ISOLATED GAME LAUNCHER"
Write-Host " Mode:     $Mode"
Write-Host " Scenario: $Scenario"
Write-Host " Port:     $Port"
Write-Host "============================================================"

# State variables for try/finally lifecycle
$violations = [System.Collections.Generic.List[string]]::new()
$proc = $null
$launchedPid = 0
$launchedStartTime = [System.DateTime]::MinValue
$timedOut = $false
$preSavesHashes = $null
$preModsHashes = $null
$preGoldenHashes = $null
$orchestratorProc = $null

$dailyAppData = [Environment]::GetFolderPath('ApplicationData')
$dailySavesDir = Join-Path $dailyAppData "StardewValley\Saves"
$dailyModsDir = Join-Path $gamePath "Mods"

# Generate and validate RunId
if (-not $RunId) {
    $RunId = "run-" + (Get-Date -Format "yyyyMMdd-HHmmss") + "-" + [System.Guid]::NewGuid().ToString("N").Substring(0, 6)
}

Assert-ValidRunId $RunId $testRunsDir
$runDir = Join-Path $testRunsDir $RunId
Assert-PathContained $runDir $testRunsDir

$runSavesDir = Join-Path $runDir "saves"
$runModsDir = Join-Path $runDir "mods"
$runEvidenceDir = Join-Path $runDir "evidence"
$runConfigDir = Join-Path $runDir "config"

try {
    # 1. PROCESS SAFETY PRE-CHECK (Strict fail without touching unrelated processes)
    Write-Host "`n>>> STEP 1: Process Safety Pre-Check <<<"
    Assert-NoExistingGameProcesses

    # 2. RUN DIRECTORY ALLOCATION & CONTAINMENT
    Write-Host "`n>>> STEP 2: Allocating Run Directory <<<"
    foreach ($d in @($runDir, $runSavesDir, $runModsDir, $runEvidenceDir, $runConfigDir)) {
        New-Item -ItemType Directory -Path $d -Force | Out-Null
        Assert-PathContained $d $testRunsDir
    }
    Write-Host "Allocated run directory: $runDir"

    # 3. VERIFY AUTHORITATIVE GOLDEN FIXTURE MANIFEST
    if (-not $CustomSavePath) {
        Write-Host "`n>>> STEP 3: Verifying Golden Fixture Manifest <<<"
        Assert-GoldenFixtureManifest $goldenV1Save $goldenManifestFile
    } else {
        Write-Host "`n>>> STEP 3: Custom Save specified ($CustomSavePath) - Skipping Golden Fixture Manifest <<<"
    }

    # 4. PRE-LAUNCH INTEGRITY BASELINES (Daily Saves, Daily Mods, Golden/Custom Source)
    Write-Host "`n>>> STEP 4: Capturing Pre-Launch Hashes <<<"
    $preSavesHashes = Get-FolderHashes $dailySavesDir
    if ($preSavesHashes.Count -eq 0) {
        throw "PRE-CHECK VIOLATION: Daily saves directory has 0 files or does not exist: $dailySavesDir"
    }
    Write-Host "Daily saves files count before launch: $($preSavesHashes.Count)"
    $savesBaselineFile = Join-Path $runEvidenceDir "daily-saves-pre.sha256"
    $preSavesLines = @()
    foreach ($k in $preSavesHashes.Keys) { $preSavesLines += "$($preSavesHashes[$k])  $k" }
    Write-Utf8File $savesBaselineFile (($preSavesLines -join "`n") + "`n")

    $preModsHashes = Get-FolderHashes $dailyModsDir
    if ($preModsHashes.Count -eq 0) {
        throw "PRE-CHECK VIOLATION: Daily mods directory has 0 files or does not exist: $dailyModsDir"
    }
    Write-Host "Daily mods files count before launch: $($preModsHashes.Count)"
    $modsBaselineFile = Join-Path $runEvidenceDir "daily-mods-pre.sha256"
    $preModsLines = @()
    foreach ($k in $preModsHashes.Keys) { $preModsLines += "$($preModsHashes[$k])  $k" }
    Write-Utf8File $modsBaselineFile (($preModsLines -join "`n") + "`n")

    $preCustomHashes = $null
    if (-not $CustomSavePath) {
        $preGoldenHashes = Get-FolderHashes $goldenV1Save
        if ($preGoldenHashes.Count -eq 0) {
            throw "PRE-CHECK VIOLATION: Golden save directory has 0 files or does not exist: $goldenV1Save"
        }
        Write-Host "Golden source files count before launch: $($preGoldenHashes.Count)"
        $goldenPreFile = Join-Path $runEvidenceDir "golden-source-pre.sha256"
        $preGoldenLines = @()
        foreach ($k in $preGoldenHashes.Keys) { $preGoldenLines += "$($preGoldenHashes[$k])  $k" }
        Write-Utf8File $goldenPreFile (($preGoldenLines -join "`n") + "`n")
    } else {
        if (-not (Test-Path -LiteralPath $CustomSavePath)) {
            throw "CUSTOM SAVE VIOLATION: Custom save directory does not exist: $CustomSavePath"
        }
        $preCustomHashes = Get-FolderHashes $CustomSavePath
        if ($preCustomHashes.Count -eq 0) {
            throw "CUSTOM SAVE VIOLATION: Custom save directory has 0 files: $CustomSavePath"
        }
        Write-Host "Custom save source files count before launch: $($preCustomHashes.Count)"
        $customPreFile = Join-Path $runEvidenceDir "custom-source-pre.sha256"
        $preCustomLines = @()
        foreach ($k in $preCustomHashes.Keys) { $preCustomLines += "$($preCustomHashes[$k])  $k" }
        Write-Utf8File $customPreFile (($preCustomLines -join "`n") + "`n")
    }

    # Only update artifacts/infra/ manifests if Mode is explicitly baseline-gate
    if ($Mode -eq 'baseline-gate') {
        Write-Utf8File (Join-Path $infraArtifactsDir "daily-saves-baseline.sha256") (($preSavesLines -join "`n") + "`n")
        Write-Utf8File (Join-Path $infraArtifactsDir "daily-mods-baseline.sha256") (($preModsLines -join "`n") + "`n")
    }

    # 5. DEPLOY SAVE INTO RUN
    if (-not $CustomSavePath) {
        Write-Host "`n>>> STEP 5: Deploying Golden Save into Run <<<"
        $runSaveDest = Join-Path $runSavesDir "AstraFarm_448732481"
        New-Item -ItemType Directory -Path $runSaveDest -Force | Out-Null
        Copy-Item -Path (Join-Path $goldenV1Save "*") -Destination $runSaveDest -Force
        Write-Host "Deployed Golden Save AstraFarm_448732481 to: $runSaveDest"
    } else {
        $customSaveLeaf = Split-Path $CustomSavePath -Leaf
        Write-Host "`n>>> STEP 5: Deploying Custom Save '$customSaveLeaf' into Run <<<"
        $runSaveDest = Join-Path $runSavesDir $customSaveLeaf
        New-Item -ItemType Directory -Path $runSaveDest -Force | Out-Null
        Copy-Item -Path (Join-Path $CustomSavePath "*") -Destination $runSaveDest -Force
        Write-Host "Deployed Custom Save '$customSaveLeaf' to: $runSaveDest"
    }

    # 6. DEPLOY ISOLATED TEST MODS INTO RUN
    Write-Host "`n>>> STEP 6: Deploying Isolated Test Mods <<<"
    # 6a. Production Mod
    $prodDest = Join-Path $runModsDir "StardewAI.Companion.Mod"
    New-Item -ItemType Directory -Path $prodDest -Force | Out-Null

    if ($Mode -eq 'baseline-gate' -or $Mode -eq 'arrange-smoke') {
        # Deploy readonly baseline
        $readonlyBaseline = Join-Path $repoRoot "artifacts\baselines\production-readonly\pre-isolated-output-20260910-r1"
        Copy-Item -Path (Join-Path $readonlyBaseline "StardewAI.Companion.Mod.dll") -Destination $prodDest -Force
        Copy-Item -Path (Join-Path $readonlyBaseline "StardewAI.Companion.Mod.pdb") -Destination $prodDest -Force
        Copy-Item -Path (Join-Path $readonlyBaseline "StardewAI.Companion.Mod.deps.json") -Destination $prodDest -Force
        $realProdManifest = Join-Path $repoRoot "src\StardewAI.Companion.Mod\manifest.json"
        Copy-Item -LiteralPath $realProdManifest -Destination (Join-Path $prodDest "manifest.json") -Force
        Write-Host "Deployed readonly production baseline Mod to: $prodDest"
    } else {
        # Scenario mode: deploy frozen production mod if specified, or default diagnostic snapshot, or compiled output
        $prodSource = $FrozenProdDir
        if ($prodSource -and (Test-Path -LiteralPath $prodSource)) {
            $prodSource = (Resolve-Path -LiteralPath $prodSource).Path
        } else {
            $prodSource = Join-Path $repoRoot "artifacts\infra\diagnostic-production-r12"
        }
        if (-not (Test-Path -LiteralPath $prodSource)) {
            $prodSource = Join-Path $repoRoot "artifacts\infra\diagnostic-production-r9"
        }
        if (-not (Test-Path -LiteralPath $prodSource)) {
            $prodSource = Join-Path $repoRoot "artifacts\bin\StardewAI.Companion.Mod\Debug\net6.0"
        }
        if (Test-Path -LiteralPath $prodSource) {
            # The frozen package is a distribution root that contains the actual SMAPI
            # mod folder (e.g. <root>\StardewAI.Companion.Mod\StardewAI.Companion.Mod.dll)
            # next to the launcher .bat/.cmd helpers. SMAPI only loads a DLL that sits
            # directly inside the mod folder, so copy the *inner* mod folder contents,
            # never the distribution root (which would nest the DLL one level too deep).
            $modFolderName = "StardewAI.Companion.Mod"
            $nestedModFolder = Join-Path $prodSource $modFolderName
            if (Test-Path -LiteralPath (Join-Path $nestedModFolder "$modFolderName.dll")) {
                $modContentSource = $nestedModFolder
            } elseif (Test-Path -LiteralPath (Join-Path $prodSource "$modFolderName.dll")) {
                $modContentSource = $prodSource
            } else {
                throw "Production Mod source '$prodSource' does not contain '$modFolderName.dll' (checked the root and the '$modFolderName' subfolder)."
            }

            Copy-Item -Path (Join-Path $modContentSource "*") -Destination $prodDest -Recurse -Force
            $realProdManifest = Join-Path $repoRoot "src\StardewAI.Companion.Mod\manifest.json"
            if (-not (Test-Path -LiteralPath (Join-Path $prodDest "manifest.json"))) {
                Copy-Item -LiteralPath $realProdManifest -Destination (Join-Path $prodDest "manifest.json") -Force
            }

            # Never trust the copy: the deployed DLL must exist exactly where SMAPI looks
            # for it, and its hash must match the source package (and the expected hash
            # when the caller pinned one).
            $deployedDll = Join-Path $prodDest "$modFolderName.dll"
            if (-not (Test-Path -LiteralPath $deployedDll)) {
                throw "Deployment verification failed: '$deployedDll' does not exist after copying from '$modContentSource'. SMAPI would skip the Mod."
            }
            $sourceDll = Join-Path $modContentSource "$modFolderName.dll"
            $deployedHash = (Get-FileHash -LiteralPath $deployedDll -Algorithm SHA256).Hash
            $sourceHash = (Get-FileHash -LiteralPath $sourceDll -Algorithm SHA256).Hash
            if ($deployedHash -ne $sourceHash) {
                throw "Deployment verification failed: deployed DLL hash $deployedHash does not match frozen source hash $sourceHash."
            }
            if ($ExpectedProdDllSha256 -and ($deployedHash -ne $ExpectedProdDllSha256.ToUpperInvariant())) {
                throw "Deployment verification failed: deployed DLL hash $deployedHash does not match the expected frozen hash $($ExpectedProdDllSha256.ToUpperInvariant())."
            }
            Write-Host "Deployed production Mod from '$modContentSource' to: $prodDest"
            Write-Host "Verified deployed Mod DLL SHA256: $deployedHash"
        } else {
            throw "Production mod source not found at: $prodSource."
        }
    }

    if ($PresetAcceptance) {
        if ($Scenario -ne 'free-mode-control' -or $Mode -ne 'scenario') {
            throw 'Preset acceptance requires the isolated free-mode-control scenario.'
        }
        # Fresh runtime state is essential: never copy a live installed mod here.
        if (Test-Path -LiteralPath (Join-Path $prodDest 'chat_sessions.json')) {
            throw 'Use a clean release stage, not an installed Mod with session history.'
        }
        $presetConfig = Join-Path $prodDest 'config'
        New-Item -ItemType Directory -Path $presetConfig -Force | Out-Null
        Write-Utf8File (Join-Path $presetConfig 'chat-backend.json') '{"backend":"mcode","effort":"default","model":""}'
        # This suite explicitly exercises the built-in entry. Keep the choice
        # inside its isolated Mod copy; never change the player's installation.
        $presetModConfigPath = Join-Path $prodDest 'config.json'
        $presetModConfig = if (Test-Path -LiteralPath $presetModConfigPath) {
            Get-Content -LiteralPath $presetModConfigPath -Raw | ConvertFrom-Json
        } else { [pscustomobject]@{} }
        $presetModConfig | Add-Member -NotePropertyName ConnectionSource -NotePropertyValue 'builtin' -Force
        $presetModConfig | Add-Member -NotePropertyName ConnectionSourceChosen -NotePropertyValue $true -Force
        Write-Utf8File $presetModConfigPath ($presetModConfig | ConvertTo-Json -Depth 20)
    }

    # 6b. TestDriver Mod
    $driverSource = Join-Path $repoRoot "artifacts\bin\StardewAI.Companion.TestDriver\Debug\net6.0"
    $driverDest = Join-Path $runModsDir "StardewAI.Companion.TestDriver"
    New-Item -ItemType Directory -Path $driverDest -Force | Out-Null
    Copy-Item -Path (Join-Path $driverSource "*") -Destination $driverDest -Recurse -Force

    $harmonySrc = Join-Path $gamePath "smapi-internal\0Harmony.dll"
    if (Test-Path -LiteralPath $harmonySrc) {
        Copy-Item -LiteralPath $harmonySrc -Destination (Join-Path $driverDest "0Harmony.dll") -Force
    }
    Write-Host "Deployed TestDriver Mod to: $driverDest"

    # 7. WRITE PER-RUN SESSION CONFIG & COORDINATION
    Write-Host "`n>>> STEP 7: Writing Per-Run Session Config <<<"
    $evidenceFile = Join-Path $runEvidenceDir "scenario-evidence.json"
    $coordFile = Join-Path $runEvidenceDir "harness-coordination.json"
    $sessionJsonPath = Join-Path $runConfigDir "test-session.json"
    $sessionConfig = [ordered]@{
        runId = $RunId
        testMode = $Mode
        scenario = $Scenario
        port = $Port
        isolatedSavesPath = $runSavesDir
        evidenceDir = $runEvidenceDir
        evidenceOutputPath = $evidenceFile
        goldenSavePath = $goldenV1Save
        coordinationPath = $coordFile
    }
    $sessionJson = $sessionConfig | ConvertTo-Json -Depth 5
    Write-Utf8File $sessionJsonPath ($sessionJson + "`n")
    Write-Host "Wrote session config to: $sessionJsonPath"

    $initialCoord = [ordered]@{
        state = "INITIALIZING"
        runId = $RunId
        scenario = $Scenario
        mode = $Mode
        port = $Port
        timestampUtc = [System.DateTime]::UtcNow.ToString("o")
    }
    Write-Utf8File $coordFile (($initialCoord | ConvertTo-Json -Depth 5) + "`n")

    # 8. LAUNCH GAME
    Write-Host "`n>>> STEP 8: Launching SMAPI with Per-Run Isolation <<<"
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $smapiExe
    $psi.Arguments = "--mods-path `"$runModsDir`""
    $psi.WorkingDirectory = $gamePath
    $psi.EnvironmentVariables['STARDEW_AI_ISOLATED_SAVES'] = $runSavesDir
    $psi.EnvironmentVariables['STARDEW_AI_SESSION_CONFIG'] = $sessionJsonPath
    $psi.EnvironmentVariables['STARDEW_AI_TEST_MODE'] = $Mode
    $psi.EnvironmentVariables['STARDEW_AI_SCENARIO'] = $Scenario
    $psi.EnvironmentVariables['STARDEW_AI_PORT'] = [string]$Port
    $psi.EnvironmentVariables['STARDEW_AI_EVIDENCE_DIR'] = $runEvidenceDir
    $psi.EnvironmentVariables['STARDEW_AI_EVIDENCE_OUTPUT'] = $evidenceFile
    $psi.EnvironmentVariables['STARDEW_AI_RUN_ID'] = $RunId
    foreach ($key in $EnvironmentOverrides.Keys) {
        $psi.EnvironmentVariables[$key] = [string]$EnvironmentOverrides[$key]
        Write-Host "Applying child environment override: $key=$($EnvironmentOverrides[$key])"
    }
    # Sandbox-friendly redirection without CLI quoting problems: any environment
    # variable named STARDEW_AI_ENV_<NAME> is forwarded to the child as <NAME>.
    foreach ($entry in [System.Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
        $name = [string]$entry.Key
        if ($name -like 'STARDEW_AI_ENV_*') {
            $target = $name.Substring('STARDEW_AI_ENV_'.Length)
            if ($target) {
                $psi.EnvironmentVariables[$target] = [string]$entry.Value
                Write-Host "Forwarding child environment override: $target=$($entry.Value)"
            }
        }
    }
    $psi.UseShellExecute = $false

    Write-Host "Launching process: $($psi.FileName) $($psi.Arguments)"
    $proc = [System.Diagnostics.Process]::Start($psi)
    $launchedPid = $proc.Id
    $launchedStartTime = $proc.StartTime
    Write-Host "Started game process PID: $launchedPid (StartTime: $launchedStartTime)"

    $launchTimeUtc = [System.DateTime]::UtcNow
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $orchestratorStarted = $false
    while (-not $proc.HasExited) {
        Start-Sleep -Milliseconds 500
        if ($Mode -eq 'scenario' -and -not $orchestratorStarted -and -not $NoOrchestrator) {
            if (Test-Path -LiteralPath $coordFile) {
                try {
                    $coordContent = Get-Content -LiteralPath $coordFile -Raw -ErrorAction SilentlyContinue
                    if ($coordContent -match '"READY_FOR_ACT"') {
                        Write-Host "`n>>> [Launcher] Detected READY_FOR_ACT! Launching external Skill Orchestrator... <<<"
                        $orchestratorStarted = $true
                        $orchScript = Join-Path $repoRoot "tools\run-skill-orchestrator.py"
                        $orchStdoutFile = Join-Path $runEvidenceDir "orchestrator-stdout.log"
                        $orchStderrFile = Join-Path $runEvidenceDir "orchestrator-stderr.log"
                        $orchTimeout = if ($Scenario -eq 'loop-demo') { [math]::Min($TimeoutSeconds, 60.0) } else { 30.0 }
                        $orchArgs = "run --project `"$repoRoot\runtime`" python `"$orchScript`" --run-dir `"$runDir`" --scenario `"$Scenario`" --timeout $orchTimeout"
                        $uvCmd = 'uv'
                        $uvObj = Get-Command uv -ErrorAction SilentlyContinue
                        if ($uvObj) { $uvCmd = $uvObj.Source }
                        $orchestratorProc = Start-Process -FilePath $uvCmd -ArgumentList $orchArgs -WorkingDirectory $repoRoot -RedirectStandardOutput $orchStdoutFile -RedirectStandardError $orchStderrFile -PassThru -NoNewWindow
                        Write-Host "[Launcher] Started orchestrator process PID: $($orchestratorProc.Id) (Executable: $uvCmd, Stdout: $orchStdoutFile, Stderr: $orchStderrFile)"
                    }
                } catch {
                    Write-Warning "Error launching orchestrator: $_"
                }
            }
        }
        if ($sw.Elapsed.TotalSeconds -gt $TimeoutSeconds) {
            Write-Warning "Process timed out after $TimeoutSeconds seconds! Terminating PID $launchedPid..."
            Stop-LaunchedChildProcess $proc $launchedPid $launchedStartTime
            $timedOut = $true
            break
        }
    }

    if (-not $timedOut) {
        Write-Host "Game process exited with code $($proc.ExitCode) after $([int]$sw.Elapsed.TotalSeconds)s."
    }
}
finally {
    # CLEANUP & POST-INTEGRITY GUARANTEED IN FINALLY
    Write-Host "`n>>> FINALLY: Post-Launch Integrity & Cleanup <<<"

    # 1. Cleanup child game process if still running
    try {
        if ($proc -and -not $proc.HasExited) {
            Stop-LaunchedChildProcess $proc $launchedPid $launchedStartTime
        }
    } catch {
        Write-Warning "Note during game process termination: $_"
    }

    # 2. Cleanup orchestrator process if still running & capture exit code
    try {
        if ($orchestratorProc) {
            if (-not $orchestratorProc.HasExited) {
                Write-Warning "Waiting for orchestrator process PID $($orchestratorProc.Id) to complete..."
                $null = $orchestratorProc.WaitForExit(5000)
                if (-not $orchestratorProc.HasExited) {
                    Write-Warning "Terminating orchestrator process PID $($orchestratorProc.Id)..."
                    try {
                        $orchestratorProc.Kill()
                    } catch {
                        Write-Warning "Note terminating orchestrator PID $($orchestratorProc.Id): $_"
                    }
                }
            }
            try {
                $orchestratorProc.Refresh()
            } catch {}
            $hasExited = $orchestratorProc.HasExited
            $orchExitCode = $null
            if ($hasExited) {
                try { $orchExitCode = $orchestratorProc.ExitCode } catch {}
            }
            $orchRecord = [ordered]@{
                pid = $orchestratorProc.Id
                exitCode = $orchExitCode
                stdoutFile = "orchestrator-stdout.log"
                stderrFile = "orchestrator-stderr.log"
                completed = [bool]$hasExited
            }
            Write-Utf8File (Join-Path $runEvidenceDir "orchestrator-result.json") (($orchRecord | ConvertTo-Json -Depth 3) + "`n")
            Write-Host "Recorded orchestrator outcome: ExitCode=$orchExitCode (HasExited=$hasExited)"
            if ($orchExitCode -ne 0) {
                $stderrPath = Join-Path $runEvidenceDir "orchestrator-stderr.log"
                if (Test-Path -LiteralPath $stderrPath) {
                    $errContent = Get-Content -LiteralPath $stderrPath -Raw -ErrorAction SilentlyContinue
                    if (-not [string]::IsNullOrWhiteSpace($errContent)) {
                        Write-Warning "[Launcher] Orchestrator stderr output:`n$errContent"
                    }
                }
            }
        }
    } catch {
        Write-Warning "Note during orchestrator cleanup: $_"
    }

    # 3. Capture SMAPI run log strictly to runEvidenceDir FIRST
    try {
        $smapiLogDir = Join-Path $dailyAppData "StardewValley\ErrorLogs"
        $smapiLogFile = Join-Path $smapiLogDir "SMAPI-latest.txt"
        $runSmapiLog = Join-Path $runEvidenceDir "smapi-log.txt"

        if (Test-Path -LiteralPath $smapiLogFile) {
            Copy-Item -LiteralPath $smapiLogFile -Destination $runSmapiLog -Force
            Write-Host "Captured SMAPI log to: $runSmapiLog"
            if ($Mode -eq 'baseline-gate') {
                Copy-Item -LiteralPath $smapiLogFile -Destination (Join-Path $infraArtifactsDir "baseline-smapi-log.txt") -Force
            }
        }
    } catch {
        Write-Warning "Note capturing SMAPI log: $_"
    }

    # 4. Verify and Write Daily Saves Post Hashes
    try {
        if ($preSavesHashes) {
            $postSavesHashes = Get-FolderHashes $dailySavesDir
            $postSavesLines = @()
            foreach ($k in $postSavesHashes.Keys) { $postSavesLines += "$($postSavesHashes[$k])  $k" }
            Write-Utf8File (Join-Path $runEvidenceDir "daily-saves-post.sha256") (($postSavesLines -join "`n") + "`n")

            if ($preSavesHashes.Count -ne $postSavesHashes.Count) {
                $violations.Add("Daily saves file count changed from $($preSavesHashes.Count) to $($postSavesHashes.Count)!")
            }
            foreach ($k in $preSavesHashes.Keys) {
                if (-not $postSavesHashes.ContainsKey($k) -or $preSavesHashes[$k] -ne $postSavesHashes[$k]) {
                    $violations.Add("Daily save file '$k' was modified or removed!")
                }
            }
            foreach ($k in $postSavesHashes.Keys) {
                if (-not $preSavesHashes.ContainsKey($k)) {
                    $violations.Add("Unexpected new file '$k' was created in daily saves!")
                }
            }
        }
    } catch {
        Write-Warning "Note checking daily saves integrity: $_"
    }

    # 5. Verify and Write Daily Mods Post Hashes
    try {
        if ($preModsHashes) {
            $postModsHashes = Get-FolderHashes $dailyModsDir
            $postModsLines = @()
            foreach ($k in $postModsHashes.Keys) { $postModsLines += "$($postModsHashes[$k])  $k" }
            Write-Utf8File (Join-Path $runEvidenceDir "daily-mods-post.sha256") (($postModsLines -join "`n") + "`n")

            if ($preModsHashes.Count -ne $postModsHashes.Count) {
                $violations.Add("Daily mods file count changed from $($preModsHashes.Count) to $($postModsHashes.Count)!")
            }
            foreach ($k in $preModsHashes.Keys) {
                if (-not $postModsHashes.ContainsKey($k) -or $preModsHashes[$k] -ne $postModsHashes[$k]) {
                    $violations.Add("Daily mod file '$k' was modified or removed!")
                }
            }
            foreach ($k in $postModsHashes.Keys) {
                if (-not $preModsHashes.ContainsKey($k)) {
                    $violations.Add("Unexpected new file '$k' was created in daily mods!")
                }
            }
        }
    } catch {
        Write-Warning "Note checking daily mods integrity: $_"
    }

    # 6. Verify and Write Golden / Custom Source Post Hashes
    try {
        if ($preGoldenHashes) {
            $postGoldenHashes = Get-FolderHashes $goldenV1Save
            $postGoldenLines = @()
            foreach ($k in $postGoldenHashes.Keys) { $postGoldenLines += "$($postGoldenHashes[$k])  $k" }
            Write-Utf8File (Join-Path $runEvidenceDir "golden-source-post.sha256") (($postGoldenLines -join "`n") + "`n")

            if ($preGoldenHashes.Count -ne $postGoldenHashes.Count) {
                $violations.Add("Golden fixture file count changed from $($preGoldenHashes.Count) to $($postGoldenHashes.Count)!")
            }
            foreach ($k in $preGoldenHashes.Keys) {
                if (-not $postGoldenHashes.ContainsKey($k) -or $preGoldenHashes[$k] -ne $postGoldenHashes[$k]) {
                    $violations.Add("Golden fixture file '$k' was modified during run!")
                }
            }
        }
        if ($preCustomHashes -and $CustomSavePath) {
            $postCustomHashes = Get-FolderHashes $CustomSavePath
            $postCustomLines = @()
            foreach ($k in $postCustomHashes.Keys) { $postCustomLines += "$($postCustomHashes[$k])  $k" }
            Write-Utf8File (Join-Path $runEvidenceDir "custom-source-post.sha256") (($postCustomLines -join "`n") + "`n")

            if ($preCustomHashes.Count -ne $postCustomHashes.Count) {
                $violations.Add("Custom save source file count changed from $($preCustomHashes.Count) to $($postCustomHashes.Count)!")
            }
            foreach ($k in $preCustomHashes.Keys) {
                if (-not $postCustomHashes.ContainsKey($k) -or $preCustomHashes[$k] -ne $postCustomHashes[$k]) {
                    $violations.Add("Custom save file '$k' was modified during run!")
                }
            }
        }
    } catch {
        Write-Warning "Note checking source save integrity: $_"
    }

    # 7. Record all accumulated violations without prematurely aborting finally block
    if ($violations.Count -gt 0) {
        $violationFile = Join-Path $runEvidenceDir "integrity-violations.txt"
        Write-Utf8File $violationFile (($violations -join "`n") + "`n")
        foreach ($v in $violations) {
            Write-Warning "CRITICAL INTEGRITY VIOLATION: $v"
        }
    } else {
        Write-Host "INTEGRITY VERIFIED: Daily saves, daily mods, and Golden source are 100% pristine and untouched."
    }
}

# --- OUTCOME EVALUATION ---
$evidenceJson = $null
if (Test-Path -LiteralPath $evidenceFile) {
    $rawEv = Get-Content -LiteralPath $evidenceFile -Raw
    $evidenceJson = ConvertFrom-Json $rawEv
}

$cleanExit = ($proc -and -not $timedOut -and $proc.ExitCode -eq 0)
$noViolations = ($violations.Count -eq 0)
$matchingRun = ($evidenceJson -and $evidenceJson.runId -eq $RunId -and $evidenceJson.scenario -eq $Scenario)

$runSuccess = $false
if ($Mode -eq 'baseline-gate') {
    $logContent = if (Test-Path -LiteralPath $runSmapiLog) { Get-Content -LiteralPath $runSmapiLog -Raw } else { "" }
    $hasBaselineLoaded = $logContent -match "Stardew AI Companion baseline loaded in read-only mode\."
    $hasSaveIsolation = $logContent -match "Save path isolation ACTIVE and VERIFIED on both Game and SMAPI roots"
    $hasSaved = $logContent -match "Save completed successfully in:"
    $hasReloaded = $logContent -match "Save RELOADED"

    $gatePassed = $hasBaselineLoaded -and $hasSaveIsolation -and $hasSaved -and $hasReloaded -and $noViolations -and ($evidenceJson -and $evidenceJson.gateSuccess) -and $cleanExit
    $runSuccess = $gatePassed

    $gateRecord = [ordered]@{
        gate = "stage0-baseline-gate"
        runId = $RunId
        passed = [bool]$gatePassed
        timestampUtc = [System.DateTime]::UtcNow.ToString("o")
        gameVersion = "1.6.15.24356"
        smapiVersion = "4.2.1.0"
        saveIsolationMethod = "Harmony prefix on StardewValley.Program.GetSavesFolder() and StardewModdingAPI.Constants.get_SavesPath()"
        runDirectory = $runDir
        evidence = [ordered]@{
            runEvidenceDir = $runEvidenceDir
            smapiLog = $runSmapiLog
            evidenceJson = $evidenceFile
            goldenSource = $goldenV1Save
            goldenSaveHashFile = $goldenManifestFile
        }
        checks = [ordered]@{
            readonlyBaselineModLoaded = [bool]$hasBaselineLoaded
            saveIsolationActive = [bool]$hasSaveIsolation
            saveCompletedInIsolatedDir = [bool]$hasSaved
            saveReloadVerified = [bool]$hasReloaded
            noDailySavesTouched = [bool]$noViolations
            noDailyModsTouched = [bool]$noViolations
            goldenSourceUntouched = [bool]$noViolations
        }
    }

    $gateJson = $gateRecord | ConvertTo-Json -Depth 5
    Write-Utf8File (Join-Path $runEvidenceDir "baseline-gate.json") ($gateJson + "`n")
    Write-Utf8File (Join-Path $infraArtifactsDir "baseline-gate.json") ($gateJson + "`n")
    Write-Utf8File (Join-Path $repoRoot ".local\agy-orchestration\baseline-gate.json") ($gateJson + "`n")
} elseif ($Mode -eq 'arrange-smoke') {
    # Arrange smoke succeeds if arrange completed, screenshot captured, and player invariant
    $runSuccess = ($cleanExit -and $noViolations -and $matchingRun -and $evidenceJson.gateSuccess -eq $true)
} elseif ($Mode -eq 'scenario') {
    # Functional scenario mode requires verified external Act execution
    $runSuccess = ($cleanExit -and $noViolations -and $matchingRun -and $evidenceJson.functionalSkillPassed -eq $true)
}

if ($runSuccess) {
    Write-Host "`n>>> RUN $RunId PASSED ($Mode / $Scenario) <<<"
    exit 0
} else {
    Write-Warning "`n>>> RUN $RunId FAILED ($Mode / $Scenario) <<<"
    exit 1
}

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$RunDir,
    [Parameter(Mandatory=$true)][string]$TaskSpecPath,
    [string]$PreviousReport = '',
    [ValidateRange(0,7200)][int]$WallBudgetSeconds = 0
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$run = (Resolve-Path -LiteralPath $RunDir).Path.TrimEnd('\')
$spec = (Resolve-Path -LiteralPath $TaskSpecPath).Path
$PresetAcceptance = $true
# Import the existing launcher helpers without executing its deployment workflow.
$parseErrors = $null
$tokens = $null
$launcherAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $PSScriptRoot 'launch-game.ps1'), [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Launcher helper source could not be parsed.' }
foreach ($name in @('Assert-NoExistingGameProcesses','Assert-PathContained','Get-FolderHashes','Get-AgyConfigDigest','Get-AgyAcceptanceServerName','Write-Utf8File')) {
    $definition = $launcherAst.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name}, $true)
    if (-not $definition) { throw "Missing launcher helper $name" }
    . ([scriptblock]::Create($definition.Extent.Text))
}
Assert-NoExistingGameProcesses
Assert-PathContained $run (Join-Path $repoRoot '.test-runs')
$envFile = Join-Path $repoRoot '.env.local'
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if (-not $line.Trim() -or $line.Trim().StartsWith('#')) { continue }
        $parts = $line.Split('=',2)
        if ($parts.Count -eq 2) { [Environment]::SetEnvironmentVariable($parts[0],$parts[1],'Process') }
    }
}
$gamePath = $env:STARDEW_GAME_PATH
if (-not $gamePath) { $gamePath = (& (Join-Path $PSScriptRoot 'detect-game.ps1') | Out-String).Trim() }
$sessionPath = Join-Path $run 'config/test-session.json'
$session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
if ($session.runId -ne (Split-Path $run -Leaf) -or $session.scenario -ne 'free-mode-control') { throw 'Unexpected isolated session.' }
foreach ($path in @($session.isolatedSavesPath,$session.evidenceDir,$session.evidenceOutputPath)) { Assert-PathContained $path $run }
if (-not $PreviousReport) { $PreviousReport = Join-Path $session.evidenceDir 'multi-day-report.json' }
$PreviousReport = (Resolve-Path -LiteralPath $PreviousReport).Path
Assert-PathContained $PreviousReport $session.evidenceDir
$previous = Get-Content -LiteralPath $PreviousReport -Raw | ConvertFrom-Json
$priorTaskSeconds = [double]$previous.priorTaskSeconds + [double]$previous.taskSeconds
if ($WallBudgetSeconds -eq 0) {
    $task = Get-Content -LiteralPath $spec -Raw | ConvertFrom-Json
    $WallBudgetSeconds = [math]::Floor([double]$task.wallBudgetSeconds - $priorTaskSeconds)
    if ($WallBudgetSeconds -lt 1 -or $WallBudgetSeconds -gt 7200) { throw 'No valid remaining task budget; specify the continuation budget explicitly.' }
}
$mod = Join-Path $run 'mods/StardewAI.Companion.Mod'
$backend = Get-Content -LiteralPath (Join-Path $mod 'config/chat-backend.json') -Raw | ConvertFrom-Json
if ($backend.backend -ne 'agy' -or $backend.model -ne 'gemini-3.8-flash' -or $backend.effort -ne 'low') { throw 'Original backend/model/effort differs.' }
$config = Get-Content -LiteralPath (Join-Path $mod 'config.json') -Raw | ConvertFrom-Json
if ($config.Mode -ne 'player') { throw 'Saved isolated mode is not player.' }
$tag = 'resume-' + (Get-Date -Format 'MMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0,4))
$segment = Join-Path $session.evidenceDir $tag
$frozen = Join-Path $repoRoot ('.test-runs/' + $session.runId + '-' + $tag + '-launch')
New-Item -ItemType Directory -Path $segment,$frozen | Out-Null
Copy-Item -LiteralPath $PreviousReport -Destination (Join-Path $segment 'previous-multi-day-report.json')
Write-Utf8File (Join-Path $segment 'resume-lineage.json') (@{previousReportPath=$PreviousReport;previousReportSha256=(Get-FileHash -LiteralPath $PreviousReport -Algorithm SHA256).Hash;priorTaskSeconds=$priorTaskSeconds;remainingWallBudgetSeconds=$WallBudgetSeconds} | ConvertTo-Json)
$savedPause = @{}
foreach ($stateName in @('work-state','autonomy-state')) {
    $state = Get-Content -LiteralPath (Join-Path $mod ('data/' + $stateName + '.json')) -Raw | ConvertFrom-Json
    $savedPause[$stateName] = $state.($previous.fixture.saveName).paused
}
Write-Utf8File (Join-Path $segment 'saved-pause-flags.json') ($savedPause | ConvertTo-Json)
foreach ($file in @('reviewer-stop.json','free-mode-control-done.json','agy-binding-verified.json','agy-game-binding.json','harness-coordination.json','scenario-evidence.json','free-mode-native-snapshot-request.txt')) {
    $old = Join-Path $session.evidenceDir $file
    if (Test-Path -LiteralPath $old) { Move-Item -LiteralPath $old -Destination (Join-Path $segment ('previous-' + $file)) }
}
Copy-Item -LiteralPath (Join-Path $mod 'data/agy-turn-context.json') -Destination (Join-Path $segment 'previous-agy-turn-context.json')
$fixture = Get-Content -LiteralPath (Join-Path $session.evidenceDir 'harness-fixture-startup.json') -Raw | ConvertFrom-Json
$observer = @{runId=$session.runId;saveName=$fixture.saveName;mode='multi-day';targets=$fixture.plots;chest=$fixture.chest}
Write-Utf8File (Join-Path $session.evidenceDir 'continuity-observer.json') ($observer | ConvertTo-Json -Depth 10)
foreach ($file in @('runner.py','multi_day_runner.py','resume_multi_day_runner.py')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot ('companion_acceptance/' + $file)) -Destination (Join-Path $frozen $file)
}
Copy-Item -LiteralPath $spec -Destination (Join-Path $frozen 'task-spec.json')
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $frozen 'resume-companion-multi-day.ps1')
$driver = Join-Path $run 'mods/StardewAI.Companion.TestDriver'
Copy-Item -LiteralPath (Join-Path $driver 'StardewAI.Companion.TestDriver.dll') -Destination (Join-Path $segment 'previous-TestDriver.dll')
Copy-Item -Path (Join-Path $repoRoot 'artifacts/bin/StardewAI.Companion.TestDriver/Debug/net6.0/*') -Destination $driver -Force
$dailySavesDir = Join-Path $env:APPDATA 'StardewValley/Saves'
$dailyModsDir = Join-Path $gamePath 'Mods'
$baselineSaves = Get-FolderHashes $dailySavesDir
$baselineMods = Get-FolderHashes $dailyModsDir
$baselineSource = Get-FolderHashes $session.goldenSavePath
Write-Utf8File (Join-Path $segment 'integrity-before.json') (@{saves=$baselineSaves;mods=$baselineMods;source=$baselineSource} | ConvertTo-Json -Depth 10)
$agyConfig = Join-Path $env:USERPROFILE '.gemini/config/mcp_config.json'
$beforeDigest = Get-AgyConfigDigest $agyConfig
$serverName = Get-AgyAcceptanceServerName $session.runId
$previousBinding = Get-Content -LiteralPath (Join-Path $segment 'previous-agy-game-binding.json') -Raw | ConvertFrom-Json
$providerSessions = Get-Content -LiteralPath (Join-Path $mod 'chat_sessions.json') -Raw | ConvertFrom-Json
Write-Utf8File (Join-Path $segment 'agy-alias-migration.json') (@{previousServerName=$previousBinding.serverName;serverName=$serverName;aliasChanged=($previousBinding.serverName -ne $serverName);providerSessions=$providerSessions} | ConvertTo-Json)
$existing = Get-Content -LiteralPath $agyConfig -Raw | ConvertFrom-Json
if ($existing.mcpServers.PSObject.Properties.Name -contains $serverName) { throw 'Original acceptance service unexpectedly remains registered.' }
$python = Join-Path $mod 'runtime/python/python.exe'
$modulePath = Join-Path $mod 'runtime/src'
$context = Join-Path $mod 'data/agy-turn-context.json'
$registered = $false
$proc = $null
$monitorExit = $null
try {
    & agy mcp add --env "PYTHONPATH=$modulePath" --env "STARDEW_TURN_CONTEXT=$context" $serverName -- $python -B -m stardew_ai_runtime.mcp_server --run-dir $mod --surface light
    if ($LASTEXITCODE -ne 0) { throw 'Exact original game service registration failed.' }
    $registered = $true
    $binding = @{serverName=$serverName;previousServerName=$previousBinding.serverName;command=$python;args=@('-B','-m','stardew_ai_runtime.mcp_server','--run-dir',$mod,'--surface','light');env=@{PYTHONPATH=$modulePath;STARDEW_TURN_CONTEXT=$context};otherConfigDigestBefore=$beforeDigest}
    Write-Utf8File (Join-Path $session.evidenceDir 'agy-game-binding.json') ($binding | ConvertTo-Json -Depth 10)
    Write-Utf8File (Join-Path $segment 'agy-game-binding.json') ($binding | ConvertTo-Json -Depth 10)
    Write-Utf8File $context '{}'
    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = Join-Path $gamePath 'StardewModdingAPI.exe'
    $psi.Arguments = '--mods-path "' + (Join-Path $run 'mods') + '"'
    $psi.WorkingDirectory = $gamePath
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $values = @{STARDEW_AI_ISOLATED_SAVES=$session.isolatedSavesPath;STARDEW_AI_SESSION_CONFIG=$sessionPath;STARDEW_AI_TEST_MODE=$session.testMode;STARDEW_AI_SCENARIO=$session.scenario;STARDEW_AI_PORT=[string]$session.port;STARDEW_AI_EVIDENCE_DIR=$session.evidenceDir;STARDEW_AI_EVIDENCE_OUTPUT=$session.evidenceOutputPath;STARDEW_AI_RUN_ID=$session.runId;STARDEW_AI_CONTINUITY_RESUME='1';STARDEW_AI_PRESERVE_LOADED_WORLD='1';STARDEW_AI_OBSERVE_SECONDS=[string]($WallBudgetSeconds+600);STARDEW_AGY_GAME_SERVER=$serverName}
    foreach ($key in $values.Keys) { $psi.EnvironmentVariables[$key] = $values[$key] }
    $psi.EnvironmentVariables.Remove('STARDEW_AI_HARNESS_FIXTURE')
    $proc = [Diagnostics.Process]::Start($psi)
    Write-Utf8File (Join-Path $segment 'startup.json') (@{runId=$session.runId;gamePid=$proc.Id;segment=$tag;resumedExistingSave=$true;uninterruptedSample=$false;wallBudgetSeconds=$WallBudgetSeconds} | ConvertTo-Json)
    Write-Host "Resume directory: $segment"
    & (Join-Path $repoRoot 'runtime/.venv/Scripts/python.exe') (Join-Path $frozen 'resume_multi_day_runner.py') --run-dir $run --spec-file (Join-Path $frozen 'task-spec.json') --segment-dir $segment --timeout $WallBudgetSeconds
    $monitorExit = $LASTEXITCODE
} finally {
    if ($proc -and -not $proc.HasExited) {
        $done = Join-Path $session.evidenceDir 'free-mode-control-done.json'
        if (-not (Test-Path -LiteralPath $done)) { Write-Utf8File $done '{"success":false,"reason":"resume-launcher-cleanup"}' }
        $null = $proc.WaitForExit(60000)
    }
    $gameExited = $proc -and $proc.HasExited
    $cleanup = @{server=$serverName;removed=$false;otherConfigurationUnchanged=$null}
    if ($registered -and (-not $proc -or $gameExited)) {
        & agy mcp remove $serverName | Out-Null
        $cleanup.removed = $LASTEXITCODE -eq 0
        $cleanup.otherConfigurationUnchanged = (Get-AgyConfigDigest $agyConfig) -eq $beforeDigest
    }
    Write-Utf8File (Join-Path $segment 'agy-service-cleanup.json') ($cleanup | ConvertTo-Json)
    $afterSaves = Get-FolderHashes $dailySavesDir
    $afterMods = Get-FolderHashes $dailyModsDir
    $afterSource = Get-FolderHashes $session.goldenSavePath
    Write-Utf8File (Join-Path $segment 'integrity-after.json') (@{saves=$afterSaves;mods=$afterMods;source=$afterSource} | ConvertTo-Json -Depth 10)
    $checks = @{}
    foreach ($pair in @(@('dailySaves',$baselineSaves,$afterSaves),@('installedEntries',$baselineMods,$afterMods),@('sourceSave',$baselineSource,$afterSource))) {
        $unchanged = $pair[1].Count -eq $pair[2].Count
        foreach ($key in $pair[1].Keys) { if ($pair[1][$key] -ne $pair[2][$key]) { $unchanged = $false } }
        $checks[$pair[0]] = $unchanged
    }
    $gameExit = if ($gameExited) { $proc.ExitCode } else { $null }
    Write-Utf8File (Join-Path $segment 'exit-integrity.json') (@{monitorExit=$monitorExit;gameExited=$gameExited;gameExit=$gameExit;integrity=$checks;cleanup=$cleanup} | ConvertTo-Json -Depth 10)
}
if ($monitorExit -ne 0 -or -not $gameExited -or $gameExit -ne 0 -or $checks.Values -contains $false -or -not $cleanup.removed -or -not $cleanup.otherConfigurationUnchanged) { exit 1 }

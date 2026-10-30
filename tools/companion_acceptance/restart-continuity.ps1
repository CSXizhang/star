[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RunDir,
      [Parameter(Mandatory=$true)][string]$GamePath,
      [ValidateSet('companion','player')][string]$Mode = 'companion',
      [hashtable]$EnvironmentOverrides = @{})
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$run = (Resolve-Path -LiteralPath $RunDir).Path.TrimEnd('\')
$root = [IO.Path]::GetFullPath((Join-Path $repo '.test-runs')).TrimEnd('\') + '\'
if (-not $run.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw 'Only an existing isolated .test-runs directory is allowed.' }
if (Get-Process -Name 'Stardew Valley','StardewModdingAPI' -ErrorAction SilentlyContinue) { throw 'A game is already running; no processes were stopped.' }
$sessionPath = Join-Path $run 'config\test-session.json'
$session = Get-Content -LiteralPath $sessionPath -Raw | ConvertFrom-Json
if ($session.runId -ne (Split-Path $run -Leaf) -or $session.scenario -ne 'free-mode-control') { throw 'Unexpected isolated session.' }
foreach ($path in @($session.isolatedSavesPath,$session.evidenceDir,$session.evidenceOutputPath)) {
    if (-not [IO.Path]::GetFullPath($path).StartsWith($run + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Session path escaped isolated run.' }
}
if (-not (Test-Path -LiteralPath (Join-Path $session.evidenceDir 'continuity-observer.json'))) { throw 'Save-exit must record observer metadata before restart.' }
$mod = Join-Path $run 'mods\StardewAI.Companion.Mod'
$configPath = Join-Path $mod 'config.json'
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
# Mode takes effect only at process startup. Preserve source, memory, binding and backend files.
$config | Add-Member -NotePropertyName Mode -NotePropertyValue $Mode -Force
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 30), [Text.UTF8Encoding]::new($false))
$psi = [Diagnostics.ProcessStartInfo]::new()
$psi.FileName = Join-Path $GamePath 'StardewModdingAPI.exe'
$psi.Arguments = '--mods-path "' + (Join-Path $run 'mods') + '"'
$psi.WorkingDirectory = $GamePath
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true
$psi.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
$psi.EnvironmentVariables['STARDEW_AI_ISOLATED_SAVES'] = $session.isolatedSavesPath
$psi.EnvironmentVariables['STARDEW_AI_SESSION_CONFIG'] = $sessionPath
$psi.EnvironmentVariables['STARDEW_AI_TEST_MODE'] = $session.testMode
$psi.EnvironmentVariables['STARDEW_AI_SCENARIO'] = $session.scenario
$psi.EnvironmentVariables['STARDEW_AI_PORT'] = [string]$session.port
$psi.EnvironmentVariables['STARDEW_AI_EVIDENCE_DIR'] = $session.evidenceDir
$psi.EnvironmentVariables['STARDEW_AI_EVIDENCE_OUTPUT'] = $session.evidenceOutputPath
$psi.EnvironmentVariables['STARDEW_AI_RUN_ID'] = $session.runId
$psi.EnvironmentVariables['STARDEW_AI_CONTINUITY_RESUME'] = '1'
$psi.EnvironmentVariables['STARDEW_AI_PRESERVE_LOADED_WORLD'] = '1'
$psi.EnvironmentVariables['STARDEW_AI_OBSERVE_SECONDS'] = '7200'
foreach ($key in $EnvironmentOverrides.Keys) { $psi.EnvironmentVariables[$key] = [string]$EnvironmentOverrides[$key] }
$proc = [Diagnostics.Process]::Start($psi)
@{ gamePid=$proc.Id; runId=$session.runId; mode=$Mode; sessionConfig=$sessionPath; resumedExistingSave=$true } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $session.evidenceDir 'continuity-restart.json') -Encoding utf8
Write-Output "Restarted isolated run $($session.runId), owned game PID $($proc.Id)."

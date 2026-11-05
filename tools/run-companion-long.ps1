[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$ReleaseDir,[int]$TimeoutSeconds=1200)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if (Get-Process -Name 'Stardew Valley','StardewModdingAPI' -ErrorAction SilentlyContinue) {throw 'Existing game running; isolated evaluation refused.'}
$env:STAR_DOTNET_PATH=Join-Path $root '.local/dotnet/dotnet.exe'
& (Join-Path $PSScriptRoot 'build-testdriver.ps1') -Configuration Debug
if ($LASTEXITCODE -ne 0) {throw 'TestDriver build failed.'}
$runId='long-' + (Get-Date -Format 'MMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0,4))
$run=Join-Path $root ".test-runs/$runId"
$logs=Join-Path $root ".test-runs/$runId-launch"
New-Item -ItemType Directory -Path $logs | Out-Null
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE='mixed-long'
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_STAMINA='10'
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_WATER='1'
$env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS=[string]($TimeoutSeconds+240)
$release=(Resolve-Path -LiteralPath $ReleaseDir).Path
$shell=(Get-Process -Id $PID).Path
$args=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $PSScriptRoot 'launch-game.ps1')+'"'),'-Mode','scenario','-Scenario','free-mode-control','-RunId',$runId,'-FrozenProdDir',('"'+$release+'"'),'-NoOrchestrator','-PresetAcceptance','-TimeoutSeconds',($TimeoutSeconds+540))
try {
  $launcher=Start-Process -FilePath $shell -ArgumentList $args -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'launch.log') -RedirectStandardError (Join-Path $logs 'launch.err')
  Write-Host "Run directory: $run"
  & (Join-Path $root 'runtime/.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'companion_acceptance/long_runner.py') --run-dir $run --timeout $TimeoutSeconds
  $runnerExit=$LASTEXITCODE
  $launcher.WaitForExit()
  Write-Host "Launcher exit: $($launcher.ExitCode); evaluator exit: $runnerExit"
  if ($runnerExit -ne 0 -or $launcher.ExitCode -ne 0) {exit 1}
} finally {
  Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE -ErrorAction SilentlyContinue
  Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_STAMINA -ErrorAction SilentlyContinue
  Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_WATER -ErrorAction SilentlyContinue
  Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS -ErrorAction SilentlyContinue
}

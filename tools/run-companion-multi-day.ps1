[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$ReleaseDir,
 [Parameter(Mandatory=$true)][string]$TaskSpecPath,
 [ValidateRange(5400,7200)][int]$WallBudgetSeconds=7200,
 [ValidateSet('mcode','agy')][string]$AcceptanceBackend='mcode',
 [string]$AcceptanceModel='',
 [ValidateSet('default','low','medium','high')][string]$AcceptanceEffort='default',
 [string]$SavePath=''
)
$ErrorActionPreference='Stop'
$root=Split-Path -Parent $PSScriptRoot
if(Get-Process -Name 'Stardew Valley','StardewModdingAPI' -ErrorAction SilentlyContinue){throw 'An existing game is running; isolated multi-day evaluation refused.'}
$spec=(Resolve-Path -LiteralPath $TaskSpecPath).Path
$release=(Resolve-Path -LiteralPath $ReleaseDir).Path
$definition=Get-Content -LiteralPath $spec -Raw | ConvertFrom-Json
if(-not $definition.instruction -or -not $definition.criteria -or $definition.initial.money -ne 500){throw 'A frozen multi-day task definition with criteria and initial money is required.'}
$env:STAR_DOTNET_PATH=Join-Path $root '.local/dotnet/dotnet.exe'
& (Join-Path $PSScriptRoot 'build-testdriver.ps1') -Configuration Debug
if($LASTEXITCODE -ne 0){throw 'TestDriver build failed.'}
$runId='multi-day-'+(Get-Date -Format 'MMdd-HHmmss')+'-'+([guid]::NewGuid().ToString('N').Substring(0,4))
$run=Join-Path $root ".test-runs/$runId"
$logs=Join-Path $root ".test-runs/$runId-launch"
New-Item -ItemType Directory -Path $logs | Out-Null
$frozenSpec=Join-Path $logs 'task-spec.json'
Copy-Item -LiteralPath $spec -Destination $frozenSpec
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'companion_acceptance/multi_day_runner.py') -Destination (Join-Path $logs 'multi_day_runner.py')
Copy-Item -LiteralPath $PSCommandPath -Destination (Join-Path $logs 'run-companion-multi-day.ps1')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'companion_acceptance/runner.py') -Destination (Join-Path $logs 'runner.py')
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE='multi-day'
$env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS=[string]($WallBudgetSeconds+900)
$shell=(Get-Process -Id $PID).Path
$launchArgs=@('-NoProfile','-ExecutionPolicy','Bypass','-File',('"'+(Join-Path $PSScriptRoot 'launch-game.ps1')+'"'),'-Mode','scenario','-Scenario','free-mode-control','-RunId',$runId,'-FrozenProdDir',('"'+$release+'"'),'-NoOrchestrator','-PresetAcceptance','-ActorMode','player','-TimeoutSeconds',($WallBudgetSeconds+1200))
if($AcceptanceModel){$launchArgs+=@('-AcceptanceModel',$AcceptanceModel)}
$launchArgs+=@('-AcceptanceBackend',$AcceptanceBackend,'-AcceptanceEffort',$AcceptanceEffort)
if($SavePath){$launchArgs+=@('-CustomSavePath',('"'+(Resolve-Path -LiteralPath $SavePath).Path+'"'))}
try{
 $launcher=Start-Process -FilePath $shell -ArgumentList $launchArgs -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logs 'launch.log') -RedirectStandardError (Join-Path $logs 'launch.err')
 Write-Host "Run directory: $run"
 & (Join-Path $root 'runtime/.venv/Scripts/python.exe') (Join-Path $logs 'multi_day_runner.py') --run-dir $run --spec-file $frozenSpec --timeout $WallBudgetSeconds
 $monitorExit=$LASTEXITCODE
 $launcher.WaitForExit()
 Write-Host "Launcher exit: $($launcher.ExitCode); monitor exit: $monitorExit; final task judgment requires native ledger review."
 if($monitorExit -ne 0 -or $launcher.ExitCode -ne 0){exit 1}
}finally{
 Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE -ErrorAction SilentlyContinue
 Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS -ErrorAction SilentlyContinue
}

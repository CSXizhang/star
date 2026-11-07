[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$ReleaseDir,
    [string]$SavePath = '',
    [string]$DesignBriefPath = '',
    [string]$ReferenceImagesManifest = '',
    [switch]$NativeFarmServices,
    [string]$SuiteId = '',
    [ValidateSet('mcode','agy')][string]$AcceptanceBackend = 'mcode',
    [string]$AcceptanceModel = '',
    [ValidateSet('default','low','medium','high')][string]$AcceptanceEffort = 'default',
    [ValidateSet('water-refill','harvest-store','rest-water','mature-trees','walk-pickup','walk-pickup-full','care-guidance','harvest-logistics','rest-forestry','creative-farm')]
    [string[]]$Cases = @('care-guidance','harvest-logistics','rest-forestry'),
    # Applies to the finite regression cases; creative-farm runs until completion or takeover.
    [ValidateRange(60,3600)][int]$TimeoutSeconds = 600
)
$ErrorActionPreference = 'Stop'
$creative = $Cases -contains 'creative-farm'
if ($creative -and $Cases.Count -ne 1) { throw 'creative-farm 使用独立创作环境，请单独运行该场景。' }
if ($DesignBriefPath -and -not $creative) { throw '-DesignBriefPath 仅用于 creative-farm。' }
if ($ReferenceImagesManifest -and -not $creative) { throw '-ReferenceImagesManifest 仅用于 creative-farm。' }
if ($NativeFarmServices -and -not $creative) { throw '-NativeFarmServices 仅用于 creative-farm。' }
$designBrief = ''
if ($DesignBriefPath) {
    $designBrief = Get-Content -LiteralPath $DesignBriefPath -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($designBrief)) { throw '设计约定文件为空。' }
}
$root = Split-Path -Parent $PSScriptRoot
$release = (Resolve-Path -LiteralPath $ReleaseDir).Path
$python = Join-Path $root 'runtime/.venv/Scripts/python.exe'
$shell = (Get-Process -Id $PID).Path
if (Get-Process -Name 'Stardew Valley','StardewModdingAPI' -ErrorAction SilentlyContinue) {
    throw '请先退出正在运行的游戏；验收只启动和关闭自己的隔离实例。'
}
& (Join-Path $PSScriptRoot 'build-testdriver.ps1') -Configuration Debug
if ($LASTEXITCODE -ne 0) { throw 'TestDriver build failed.' }
if (-not $SuiteId) { $SuiteId = 'presets-' + (Get-Date -Format 'MMdd-HHmmss') + '-' + ([guid]::NewGuid().ToString('N').Substring(0,4)) }
$suite = Join-Path $root ".test-runs/$suiteId"
$runId = "$suiteId-game"
$run = Join-Path $root ".test-runs/$runId"
New-Item -ItemType Directory -Path $suite | Out-Null
$specPath = Join-Path $PSScriptRoot 'companion_acceptance/cases.json'
$specs = Get-Content -LiteralPath $specPath -Raw -Encoding UTF8 | ConvertFrom-Json
Copy-Item -LiteralPath $specPath -Destination (Join-Path $suite 'cases.json')
if ($designBrief) {
    $specs.'creative-farm'.designBrief = $designBrief.Trim()
}
if ($NativeFarmServices) {
    $specs.'creative-farm'.instruction = $specs.'creative-farm'.nativeServicesInstruction
    $specs.'creative-farm'.criteria | Add-Member -NotePropertyName nativeFarmServices -NotePropertyValue $true
    $specs.'creative-farm'.criteria | Add-Member -NotePropertyName allowedBuildingTypes -NotePropertyValue @('Coop','Barn','Silo')
}
$specs | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $suite 'cases.json') -Encoding UTF8
$first = $specs.($Cases[0])
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE = if ($first.fixture) { $first.fixture } else { $Cases[0] }
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_STAMINA = [string]$first.stamina
$env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_WATER = [string]$first.water
if ($creative) {
    $launchTimeout = 0
    $env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS = '0'
    # Keep before/final native farm images; avoid an overnight stream of window PNGs.
    $env:STARDEW_AI_ENV_STARDEW_AI_CAPTURE_PERIODIC_FRAMES = '0'
} else {
    $budget = $TimeoutSeconds * $Cases.Count
    $launchTimeout = $budget + 420
    $env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS = [string]($budget + 120)
}
$launchArgs = @('-NoProfile','-ExecutionPolicy','Bypass','-File',
    ('"' + (Join-Path $PSScriptRoot 'launch-game.ps1') + '"'),
    '-Mode','scenario','-Scenario','free-mode-control','-RunId',$runId,
    '-FrozenProdDir',('"' + $release + '"'),'-NoOrchestrator','-PresetAcceptance',
    '-AcceptanceBackend',$AcceptanceBackend,'-AcceptanceEffort',$AcceptanceEffort,'-TimeoutSeconds',$launchTimeout)
if ($creative) { $launchArgs += @('-ActorMode','player') }
if ($AcceptanceModel) { $launchArgs += @('-AcceptanceModel',$AcceptanceModel) }
if ($SavePath) { $launchArgs += @('-CustomSavePath',('"' + (Resolve-Path -LiteralPath $SavePath).Path + '"')) }
$launcher = $null
$runnerExit = 1
$previousReferenceManifest = $env:STARDEW_AI_ENV_STARDEW_AI_REFERENCE_IMAGES_MANIFEST
$previousNativeDays = $env:STARDEW_AI_ENV_STARDEW_AI_CREATIVE_NATIVE_DAYS
$env:STARDEW_AI_ENV_STARDEW_AI_CREATIVE_NATIVE_DAYS = if ($NativeFarmServices) { '1' } else { '0' }
if ($ReferenceImagesManifest) {
    $env:STARDEW_AI_ENV_STARDEW_AI_REFERENCE_IMAGES_MANIFEST = (Resolve-Path -LiteralPath $ReferenceImagesManifest).Path
}
try {
    $launcher = Start-Process -FilePath $shell -ArgumentList $launchArgs -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $suite 'launch.log') -RedirectStandardError (Join-Path $suite 'launch.err')
    if ($creative) {
        & $python (Join-Path $PSScriptRoot 'companion_acceptance/creative_runner.py') --run-dir $run --case creative-farm --cases-file (Join-Path $suite 'cases.json')
    } else {
        & $python (Join-Path $PSScriptRoot 'companion_acceptance/runner.py') --run-dir $run --suite @Cases --timeout $TimeoutSeconds --cases-file (Join-Path $suite 'cases.json')
    }
    $runnerExit = $LASTEXITCODE
} finally {
    $env:STARDEW_AI_ENV_STARDEW_AI_REFERENCE_IMAGES_MANIFEST = $previousReferenceManifest
    $env:STARDEW_AI_ENV_STARDEW_AI_CREATIVE_NATIVE_DAYS = $previousNativeDays
    if ($creative -and $launcher) {
        # Even a monitor import/startup failure must release this owned game.
        # The launcher has a separate shutdown timeout after this done signal.
        $evidence = Join-Path $run 'evidence'
        $done = Join-Path $evidence 'free-mode-control-done.json'
        if (-not (Test-Path -LiteralPath $done)) {
            New-Item -ItemType Directory -Path $evidence -Force | Out-Null
            @{success=$false; error='Creative monitor exited without a completion record.'} |
                ConvertTo-Json -Compress | Set-Content -LiteralPath "$done.tmp" -Encoding UTF8
            Move-Item -LiteralPath "$done.tmp" -Destination $done -Force
        }
    }
    Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_FIXTURE -ErrorAction SilentlyContinue
    Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_STAMINA -ErrorAction SilentlyContinue
    Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_HARNESS_WATER -ErrorAction SilentlyContinue
    Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_OBSERVE_SECONDS -ErrorAction SilentlyContinue
    if ($creative) { Remove-Item Env:STARDEW_AI_ENV_STARDEW_AI_CAPTURE_PERIODIC_FRAMES -ErrorAction SilentlyContinue }
}
$launcher.WaitForExit()
if ($creative) { Write-Host "自主装修实验报告：$run/evidence/creative-farm-report.md" }
else { Write-Host "伙伴实机回归报告：$run/summary.md" }
if ($creative -and $runnerExit -eq 2) {
    Write-Host "自主装修已按人工接管/停止请求结束；保存状态见报告，启动器退出码：$($launcher.ExitCode)。"
    exit 2
}
if ($runnerExit -ne 0 -or $launcher.ExitCode -ne 0) { exit 1 }

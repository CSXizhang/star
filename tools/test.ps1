[CmdletBinding()]
param(
    [switch]$RequireDotnet,
    [switch]$RequireCSharpTests,
    [switch]$SkipModBuild
)

$ErrorActionPreference = 'Stop'
$env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
$env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'
$env:DOTNET_NOLOGO = '1'
$env:DOTNET_SKIP_FIRST_TIME_EXPERIENCE = '1'
$repoRoot = Split-Path -Parent $PSScriptRoot
& (Join-Path $PSScriptRoot 'preflight.ps1')

& uv sync --project (Join-Path $repoRoot 'runtime')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') ruff check (Join-Path $repoRoot 'runtime')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') pytest (Join-Path $repoRoot 'runtime\tests') (Join-Path $repoRoot 'tests')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') stardew-ai-runtime --check
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

& uv run --project (Join-Path $repoRoot 'runtime') python (Join-Path $repoRoot 'tools\check_release_boundaries.py')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$localDotnet = Join-Path $repoRoot '.local\dotnet\dotnet.exe'
$dotnetCommand = Get-Command dotnet -ErrorAction SilentlyContinue
$dotnetPath = if ($env:STAR_DOTNET_PATH -and (Test-Path -LiteralPath $env:STAR_DOTNET_PATH)) {
    $env:STAR_DOTNET_PATH
} elseif ($dotnetCommand) {
    $dotnetCommand.Source
} elseif (Test-Path -LiteralPath $localDotnet) {
    $localDotnet
} else {
    $null
}

# The C# checks below need the real game assemblies: both test projects resolve
# StardewModdingAPI.dll / Stardew Valley.dll from STARDEW_GAME_PATH. When the
# dependency is missing the C# tests are NOT executed; that skip is reported
# loudly and never counted as a pass (-RequireCSharpTests turns it into a hard
# error for gates that must run the C# matrix).
$gamePath = $env:STARDEW_GAME_PATH
$gameAssembly = if ($gamePath) { Join-Path $gamePath 'StardewModdingAPI.dll' } else { $null }
$gameAssembliesReady = [bool]$gameAssembly -and (Test-Path -LiteralPath $gameAssembly)

if ($dotnetPath -and $gameAssembliesReady) {
    if (-not $SkipModBuild) {
        & (Join-Path $PSScriptRoot 'build-mod.ps1')
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }
    }

    # Full C# matrix: companion mod unit tests + transport contract tests.
    # Any failing suite fails the whole script with its non-zero exit code.
    $testProjects = @(
        (Join-Path $repoRoot 'src\StardewAI.Companion.Mod.Tests\StardewAI.Companion.Mod.Tests.csproj'),
        (Join-Path $repoRoot 'src\StardewAI.Companion.Transport.Tests\StardewAI.Companion.Transport.Tests.csproj')
    )
    foreach ($testProject in $testProjects) {
        Write-Host "运行 C# 测试: $testProject"
        & $dotnetPath test $testProject --nologo
        if ($LASTEXITCODE -ne 0) {
            Write-Error "C# 测试失败: $testProject"
            exit $LASTEXITCODE
        }
    }
    Write-Host 'C# 测试全部通过 (StardewAI.Companion.Mod.Tests + StardewAI.Companion.Transport.Tests)。'
    exit 0
}

if (-not $dotnetPath) {
    if ($RequireDotnet) {
        Write-Error '.NET SDK is required but missing.'
        exit 1
    }
    Write-Warning '.NET SDK 未安装: C# Mod 构建与 C# 测试未执行 (跳过, 不计入通过)。'
}

if (-not $gameAssembliesReady) {
    if ($RequireCSharpTests) {
        Write-Error "C# 测试需要游戏程序集, 但 STARDEW_GAME_PATH 未指向包含 StardewModdingAPI.dll 的安装目录 (当前值: '$gamePath')。"
        exit 1
    }
    Write-Warning "C# 测试未执行: STARDEW_GAME_PATH 未指向包含 StardewModdingAPI.dll 的游戏安装目录 (当前值: '$gamePath')。这是环境缺失导致的跳过, 不是通过。"
}
